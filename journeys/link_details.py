"""Reading a stay, activity or transport's details from its link (or pasted page text), and filling
in what a saved plan is missing from it.

The planner's "Fill from a link" box asks read_stay / read_activity / read_leg and fills the form.
A planned plan that has a link is also read when it's first saved (or its link changes), and again
when "Refresh details" is pressed (refresh): what the page gives fills only the plan's empty
details, so nothing typed by hand is overwritten. What it still lacks is flagged on the plan
(booking_costs.missing_details) until it's added.
"""

import hashlib
import re

from django.utils import timezone

from . import activity_import, booking_costs, booking_import, china_coords, geocode, leg_import, metro, nights, site_links, stay_import
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
    amount paid line); else nothing."""
    amount = booking_import._amount(booking_import.clean_text(text))
    return {"amount": amount[0], "currency": amount[1]} if amount else {}


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


def read_stay(url="", text=""):
    """{"fields", "city", "location", "missing"} from a hotel or booking page link, or pasted text.
    Raises PageError when the page can't be read or has nothing about a stay."""
    booked = site_links.read(url) if url else None
    if booked:
        found = stay_from_site_link(booked)
    else:
        page = stay_import.fetch_page(url) if url else text[:500_000]
        cost = paid(booking_import.html_to_text(page[:2_000_000]) if url else page)
        try:
            found = stay_import.extract(page, url)
        except PageError:
            if not cost:
                raise
            found = {}  # a booking page or confirmation with no hotel details: still its cost
        found.update(cost)

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


def read_activity(trip, url="", text="", day=None, title=""):
    """{"fields", "place", "missing", "note"} for a sight or tour from its link (or pasted page text).
    `title` is the activity's name as typed: it stands in when the site won't show its page to the
    server and the link doesn't name the place, and for pasted text, which seldom names it."""
    if url:
        found = activity_import.read_link(url, name_hint=title)
    else:
        found = activity_import.extract(text[:500_000], name_hint=title)
        found.update(paid(text[:500_000]))

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


def read_leg(trip, url="", text=""):
    """{"fields", "origin", "destination", "missing"} for a bus, coach, shuttle or car booking page."""
    found = leg_import.read_link(url) if url else leg_import.extract(text[:500_000])
    money = booking_costs.parse_money(found.get("price"))
    if money:
        found["amount"], found["currency"] = str(money[0]), money[1]
    elif text.strip():
        found.update(paid(text[:500_000]))
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
            amount, currency = booking_costs.total_from_price(plan, f["amount"]), f.get("currency") or "AUD"
            booking_costs.set_estimate(plan, amount, currency, site=f.get("site", ""))
            filled.append("estimated cost")
    return filled, booking_costs.missing_details(plan), ""
