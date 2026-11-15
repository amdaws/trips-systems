"""Reading a stay, activity or transport's details from its link (or pasted page text), and filling
in what a saved plan is missing from it.

The planner's "Fill from a link" box asks read_stay / read_activity / read_leg and fills the form.
A planned plan that has a link is also read when it's first saved (or its link changes), and again
when "Refresh details" is pressed (refresh): what the page gives fills only the plan's empty
details, so nothing typed by hand is overwritten. What it still lacks is flagged on the plan
(booking_costs.missing_details) until it's added.

With an Anthropic API key set, Claude reads the page too (claude_read), whether the server fetched
it from the link, the browser sent it (Send to Trips) or its text was pasted in.
"""

import hashlib
import re
import time
import urllib.parse

from django.utils import timezone

from . import (activity_import, booking_costs, booking_import, china_coords, claude_read, geocode, leg_import, metro, nights, site_links,
               stay_import)
from .models import BookingStatus, Item, Place, Stay, normalise
from .search import find_places, match_airport, match_station

PageError = stay_import.PageError

LINK_DETAILS = {  # what a link should give a planned plan, and what it's called when missing
    "stay": [(("amount",), "cost"), (("address",), "address"), (("phone", "email"), "phone or email"),
             (("check_in_time",), "check-in time"), (("check_out_time",), "check-out time")],
    "activity": [(("amount",), "cost"), (("address", "lat"), "address"), (("phone", "email"), "phone or email"),
                 (("opening_hours",), "opening hours")],
    "leg": [(("amount",), "cost"), (("operator",), "operator")],
}


def link_missing(kind, found):
    """What the page didn't give, for the form to say (and the plan to flag until it's added)."""
    return [label for keys, label in LINK_DETAILS[kind] if not any(found.get(key) for key in keys)]


def paid(text):
    """{"amount", "currency"} when a booking page or confirmation says what was paid (a total or
    amount paid line, above zero); else nothing."""
    amount = booking_import._amount(booking_import.clean_text(text))
    return {"amount": amount[0], "currency": amount[1]} if amount and booking_import._positive(amount[0]) else {}


def capture_page(capture):
    """A page as the browser showed it, sent by the Send to Trips bookmarklet (journeys/bookmarklet.js):
    (its address, the page rebuilt as HTML the pattern readers understand, its words for Claude)."""
    from html import escape

    url = str(capture.get("url") or "")[:500]
    url = url if url.startswith(("http://", "https://")) else ""
    title = " ".join(str(capture.get("title") or "").split())[:300]
    meta = capture.get("meta") if isinstance(capture.get("meta"), dict) else {}
    meta = {str(key)[:60]: str(value)[:2000] for key, value in list(meta.items())[:60]}
    parts = [f"<title>{escape(title)}</title>"]
    for key, value in meta.items():
        attribute = "property" if re.match(r"(?i)(og|product|place):", key) else "name"
        parts.append(f'<meta {attribute}="{escape(key)}" content="{escape(value)}">')
    if capture.get("ld"):
        parts.append(f'<script type="application/ld+json">{str(capture["ld"])[:200_000]}</script>')
    if capture.get("geo"):
        parts.append("<script>{" + str(capture["geo"])[:5000] + "}</script>")  # map positions from the page's own data
    words = str(capture.get("text") or "")[:200_000]
    parts.append(f"<body>{escape(words)}</body>")
    description = meta.get("og:description") or meta.get("description") or ""
    return url, "\n".join(parts), "\n".join(part for part in (title, description[:1000], words) if part)


def page_words(page):
    """A page the server fetched, as words for Claude: its title and description (html_to_text leaves
    out the page's head, where they are), then its text."""
    from html import unescape

    head = page[:300_000]
    title = stay_import._first(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', head) or stay_import._first(
        r"<title[^>]*>([^<]+)</title>", head)
    description = stay_import._first(r'<meta[^>]+(?:name="description"|property="og:description")[^>]+content="([^"]+)"', head)
    parts = [" ".join(unescape(title).split())[:300], " ".join(unescape(description).split())[:1000],
             booking_import.html_to_text(page[:2_000_000])]
    return "\n".join(part for part in parts if part)


def _with_claude(kind, read, words, hint="", extra=None, started=None):
    """A pattern reader's fields (`read()`), with `extra` (a cost read from the text) and what Claude
    reads in `words` laid over them. Raises the reader's PageError only when nothing was found at all.

    `started` (time.monotonic()) is when the server began fetching the page from its link, in this
    request: Claude then gets only what's left of its TIMEOUT, and a robot check (BlockedError) is
    passed straight on, as there's nothing on it for Claude to read."""
    try:
        found, error = read(), None
    except PageError as exc:
        if started is not None and isinstance(exc, stay_import.BlockedError):
            raise
        found, error = {}, exc
    found.update(extra or {})
    timeout = claude_read.TIMEOUT if started is None else claude_read.TIMEOUT - (time.monotonic() - started)
    ai = claude_read.for_page(kind, claude_read.read(kind, words, hint=hint, timeout=timeout))
    if ai:
        found.update(ai)
        found["read_by"] = "claude"
    elif error and not extra:
        raise error
    return found


def stay_from_site_link(booked):
    """A Booking.com, TripAdvisor or Klook hotel link: its name, city and dates from the address,
    then the property itself from OpenStreetMap search (its address and map pin)."""
    found = {key: booked[key] for key in ("name", "url", "site", "city", "check_in_date", "check_out_date") if booked.get(key)}
    results = []
    for query in site_links.search_texts(booked):
        try:
            results = geocode.search(query, limit=1, country=booked.get("country", ""))
        except Exception:
            results = []
        if results:
            break
    if results:
        result = results[0]
        found.update(lat=result["lat"], lon=result["lon"], address=", ".join(result["label"].split(", ")[:5]))
        found.setdefault("city", result.get("city", ""))
    return found


def find_place(trip, name):
    """The place a booking page names: one already in this trip (e.g. the station the train arrives
    at), then a station, airport or other known place, then a map search."""
    if not name:
        return None
    wanted = normalise(name)
    known = {leg.origin for leg in trip.legs.select_related("origin")} | {leg.destination for leg in trip.legs.select_related("destination")}
    known |= {stay.city for stay in trip.stays.select_related("city") if stay.city}
    place = next((p for p in sorted(known, key=lambda p: -len(p.name)) if len(normalise(p.name)) > 3
                  and (normalise(p.name) in wanted or wanted in normalise(p.name))), None)
    if place is None and re.search(r"(?i)station|railway|站", name):
        place = match_station(name)
    if place is None and re.search(r"(?i)airport|机场", name):
        place = match_airport(name)
    if place is None:
        place = next(iter(find_places(name, limit=1)), None)
    if place is None:
        try:
            results = geocode.search(name, limit=1)
        except Exception:
            results = []
        if results:
            place = geocode.save_result(results[0])
    return place


def read_stay(url="", text="", capture=None):
    """{"fields", "city", "location", "missing"} from a hotel or booking page link, pasted text, or a
    page sent from the browser (`capture`). Raises PageError when there's nothing about a stay."""
    if capture:
        url, page, words = capture_page(capture)
        found = _with_claude("stay", lambda: stay_import.extract(page, url), words, hint=url, extra=paid(words))
        booked = site_links.read(url) if url else None  # Booking.com's dates are in its address
        for key in ("check_in_date", "check_out_date", "city"):
            if booked and booked.get(key) and not found.get(key):
                found[key] = booked[key]
    elif url and (booked := site_links.read(url)):
        found = stay_from_site_link(booked)
    elif url:
        started = time.monotonic()
        page = stay_import.fetch_page(url)
        words = page_words(page)
        # A booking page or confirmation with no hotel details still gives its cost (extra).
        found = _with_claude("stay", lambda: stay_import.extract(page, url), words, hint=url, extra=paid(words), started=started)
    else:
        page = text[:500_000]
        found = _with_claude("stay", lambda: stay_import.extract(page), page, extra=paid(page))

    city = None
    if found.get("city"):
        city = Place.objects.filter(kind=Place.Kind.CITY, name__iexact=found["city"]).first() or next(
            iter(find_places(found["city"], kinds=[Place.Kind.CITY], limit=1)), None)
    location = None
    if found.get("lat") and found.get("lon") and found.get("name"):
        tripcom_hotel = re.search(r"(?:hotel-detail-|[?&]hotelId=)(\d+)", url or "")
        key = f"trip.com/{tripcom_hotel[1]}" if tripcom_hotel else hashlib.sha1(f"{found['name']}|{found.get('address', '')}".encode(), usedforsecurity=False).hexdigest()[:20]
        lat, lon = china_coords.to_wgs84(found["lat"], found["lon"], found.get("datum", "wgs84"))
        location, _ = Place.objects.get_or_create(source="link", source_id=key, defaults={
            "kind": Place.Kind.HOTEL, "name": found["name"][:200], "lat": lat, "lon": lon,
            "city": city.name if city else found.get("city", "")[:120], "country": city.country if city else "",
        })
    return {"fields": found, "city": city, "location": location, "missing": link_missing("stay", found)}


def read_activity(trip, url="", text="", day=None, title="", capture=None):
    """{"fields", "place", "missing", "note"} for a sight or tour from its link, pasted page text, or a
    page sent from the browser (`capture`). `title` is the activity's name as typed: it stands in when
    the site won't show its page to the server and the link doesn't name the place, and for pasted
    text, which seldom names it."""
    if capture:
        url, page, words = capture_page(capture)
        found = _with_claude("activity", lambda: activity_import.extract(page, url, name_hint=title), words, hint=url,
                             extra=paid(words))
        if not found.get("name"):
            found.update({key: value for key, value in site_links.tripcom_activity(url).items() if key in ("name", "city")})
        found["url"] = url
        found["site"] = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
    elif url:
        started = time.monotonic()
        found = activity_import.read_link(url, name_hint=title, read_page=lambda page: _with_claude(
            "activity", lambda: activity_import.extract(page, url), page_words(page), hint=url, started=started))
    else:
        page = text[:500_000]
        found = _with_claude("activity", lambda: activity_import.extract(page, name_hint=title), page, extra=paid(page))

    place = None
    if found.get("lat") is not None and found.get("name"):
        key = hashlib.sha1(f"{found['name']}|{float(found['lat']):.5f}|{float(found['lon']):.5f}".encode(), usedforsecurity=False).hexdigest()[:20]
        place, _ = Place.objects.get_or_create(source="link", source_id=key, defaults={
            "kind": Place.Kind.ATTRACTION, "name": found["name"][:200], "lat": found["lat"], "lon": found["lon"],
        })
    elif found.get("name") or found.get("address"):
        # No position on the page: look the place up by its address or name, near that night's stay.
        near = (nights.night_names(trip).get(day.date) or {}).get("name", "") if day else ""
        if found.get("address"):
            queries = [found["address"]]
        elif found.get("site") in ("booking.com", "klook.com", "tripadvisor", "viator.com") or found.get("blocked"):
            queries = site_links.search_texts(found, near)
        else:
            queries = [found["name"]]
        if near:
            queries = [q if near.lower() in q.lower() else f"{q}, {near}" for q in queries]
        results = []
        for query in queries:
            try:
                results = geocode.search(query, limit=1, country=found.get("country", ""))
            except Exception:
                results = []
            if results:
                break
        if results:
            place = geocode.save_result(results[0], kind=Place.Kind.ATTRACTION, name=(found.get("name") or results[0]["name"])[:200])
    note = activity_import.blocked_note(found, pinned=place is not None) if found.get("blocked") else ""
    return {"fields": found, "place": place, "missing": link_missing("activity", found), "note": note}


def read_leg(trip, url="", text="", capture=None):
    """{"fields", "origin", "destination", "missing"} for a bus, coach, shuttle or car booking page: its
    link, pasted text, or the page sent from the browser (`capture`)."""
    words = text[:500_000]
    if capture:
        url, page, words = capture_page(capture)
        found = _with_claude("leg", lambda: leg_import.extract(page, url), words, hint=url)
    elif url:
        started = time.monotonic()
        found, words = leg_import.read_link(url, read_page=lambda page: _with_claude(
            "leg", lambda: leg_import.extract(page, url), page_words(page), hint=url, started=started)), ""
    else:
        found = _with_claude("leg", lambda: leg_import.extract(words), words)
    money = booking_costs.parse_money(found.get("price"))
    if money and not found.get("amount"):
        found["amount"], found["currency"] = str(money[0]), money[1]
    if not found.get("amount") and words.strip():
        found.update(paid(words))
    return {"fields": found, "missing": link_missing("leg", found),
            "origin": find_place(trip, found.get("origin")), "destination": find_place(trip, found.get("destination"))}


# ---------- a saved plan: reading its link and filling its gaps ----------


def _fill(plan, attr, value, filled, label):
    if value and not getattr(plan, attr):
        setattr(plan, attr, value)
        filled.append(label)


def refresh(plan):
    """Read a planned plan's link and fill its empty details from it (never overwriting anything).
    Returns (what was filled, what's still missing, or an error message). The result is kept on the
    plan (details_checked_at, details_note) for its edit page."""
    from datetime import time as clock

    url = booking_costs.plan_link(plan)
    if not url:
        return [], [], "There's no link to read the details from."
    filled = []
    try:
        if isinstance(plan, Stay):
            result = read_stay(url)
        elif isinstance(plan, Item):
            result = read_activity(plan.day.trip, url, day=plan.day, title=plan.title)
        else:
            result = read_leg(plan.trip, url)
    except PageError as exc:
        plan.details_checked_at, plan.details_note = timezone.now(), str(exc)[:300]
        plan.save(update_fields=["details_checked_at", "details_note"])
        return [], booking_costs.missing_details(plan), str(exc)
    except Exception as exc:  # a site timing out or answering oddly: say so, keep the plan as it is
        plan.details_checked_at, plan.details_note = timezone.now(), f"Couldn't read the page ({exc.__class__.__name__})."
        plan.save(update_fields=["details_checked_at", "details_note"])
        return [], booking_costs.missing_details(plan), plan.details_note

    f = result["fields"]

    def as_time(value):
        match = re.match(r"(\d{1,2}):(\d{2})", str(value or ""))
        return clock(int(match[1]), int(match[2])) if match and int(match[1]) < 24 else None

    if isinstance(plan, Stay):
        before = (plan.address, plan.location_id)
        _fill(plan, "name", (f.get("name") or "")[:200], filled, "property name")
        _fill(plan, "address", f.get("address", ""), filled, "address")
        _fill(plan, "phone", (f.get("phone") or "")[:60], filled, "phone")
        _fill(plan, "email", (f.get("email") or "")[:254], filled, "email")
        _fill(plan, "check_in_time", as_time(f.get("check_in_time")), filled, "check-in time")
        _fill(plan, "check_out_time", as_time(f.get("check_out_time")), filled, "check-out time")
        _fill(plan, "city", result["city"], filled, "city")
        _fill(plan, "location", result["location"], filled, "map location")
        if plan.label_names_hotel():
            plan.label = (plan.city.name if plan.city else "Stay")[:100]
        if (plan.address, plan.location_id) != before:  # look again for the nearest metro station
            metro.reset(plan, address_changed=plan.address != before[0], location_changed=plan.location_id != before[1])
    elif isinstance(plan, Item):
        had_place = plan.place_id
        _fill(plan, "address", (f.get("address") or "")[:300], filled, "address")
        _fill(plan, "place", result["place"], filled, "map location")
        _fill(plan, "opening_hours", (f.get("opening_hours") or "")[:300], filled, "opening hours")
        _fill(plan, "phone", (f.get("phone") or "")[:60], filled, "phone")
        _fill(plan, "email", (f.get("email") or "")[:254], filled, "email")
        _fill(plan, "description", (f.get("description") or "")[:500], filled, "description")
        if plan.place_id and not had_place:
            plan.directions = {"pending": True}  # the background job works out how to get there
    else:
        _fill(plan, "operator", (f.get("operator") or "")[:100], filled, "operator")
        _fill(plan, "seat_class", (f.get("seat_class") or "")[:50], filled, "class")

    # A site that wouldn't show its page: say so (the edit page shows it beside Refresh details).
    plan.details_checked_at, plan.details_note = timezone.now(), (result.get("note") or "")[:300]
    plan.save()
    if f.get("amount") and plan.status == BookingStatus.PLANNED:
        line = booking_costs.budget_line(plan)
        if not line or (line.estimate is None and line.actual is None):
            amount = booking_costs.total_from_price(plan, f["amount"], per_person=not f.get("price_is_total"))
            currency = f.get("currency") or "AUD"
            booking_costs.set_estimate(plan, amount, currency, site=f.get("site", ""))
            filled.append("estimated cost")
    return filled, booking_costs.missing_details(plan), ""
