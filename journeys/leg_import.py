"""Read a bus, coach, shuttle or private-car booking page for the transport form.

Works with product pages such as Trip.com's things-to-do transfers, Klook or 12Go,
from the link or (when the site shows a robot check, as Trip.com does) from the
page text pasted in. schema.org trip data is used when a page has it; otherwise the
route comes from the title, e.g. "Huanglong Jiuzhai station - Jiuzhaigou National
Park shuttle bus [VIP 2+1 luxury bus]" or "Private transfer from Zhangjiajie
Airport to Wulingyuan".
"""

import json
import re

from .models import Leg
from .stay_import import PageError, _enclosing_object, _first, _unescape_layers, fetch_page

PRIVATE = r"private (?:car|transfer|vehicle|driver|charter)|car charter|chartered car|chauffeur|exclusive car|with driver|taxi|didi"
SHARED = r"shuttle|shared (?:transfer|ride|van)|join-in|seat in coach"
COACH = r"\b(?:bus|coach|minibus)\b"
VEHICLE = rf"(?:{PRIVATE}|{SHARED}|{COACH}|\btransfers?\b|\bpick-?up\b|\bdrop-?off\b|\bcharter\b|\bvan\b|\bcar\b)"
SEPARATOR = r"\s+(?:to|-|–|—|→|->|⇌|⇄|<->|↔|~)\s+"
MONEY = r"((?:A\$|AU\$|US\$|HK\$|NT\$|S\$|CN¥|¥|￥|€|£|\$|RMB\s?)\s?\d[\d,]*(?:\.\d{1,2})?)"


def read_link(url, read_page=None):
    """A transfer's details from its link. `read_page(page)`, when given, reads the fetched page instead of
    extract (link_details passes one that has Claude read it too)."""
    from . import site_links

    booked = site_links.read(url)
    if booked:  # Klook, Booking.com: read from the address (their pages can't be fetched)
        return {"mode": booked.get("mode", ""), "operator": booked.get("operator", ""), "origin": booked.get("origin", ""),
                "destination": booked.get("destination", ""), "url": url, "notes": booked.get("name", ""), "site": booked["site"]}
    try:
        page = fetch_page(url)
    except PageError:
        raise
    if re.search(r"verification|captcha|verify you are human", page[:20000], re.I) and len(page) < 40000:
        raise PageError("The site showed a robot check instead of the page. Use Send to Trips on the page in your browser, "
                        "or select all (Ctrl/⌘ + A), copy, and paste the text below.")
    return read_page(page) if read_page else extract(page, url)


def mode_for(text):
    """The transport type a description sounds like (a private car, a shuttle, or a bus or coach)."""
    lowered = text.lower()
    if re.search(PRIVATE, lowered) and not re.search(r"\bshared\b", lowered):
        return Leg.Mode.CAR
    if re.search(SHARED, lowered):
        return Leg.Mode.SHUTTLE
    if re.search(COACH, lowered):
        return Leg.Mode.BUS
    if re.search(r"\btransfers?\b|\bpick-?up\b", lowered):
        return Leg.Mode.CAR
    return ""


def route_from_title(title):
    """(from, to, extra) from a product title; extra is any bracketed detail such as the vehicle."""
    extra = "; ".join(part.strip() for part in re.findall(r"[\[(（【]([^\])）】]+)[\])）】]", title) if part.strip())
    plain = " ".join(re.sub(r"[\[(（【][^\])）】]*[\])）】]", " ", title).split())
    found = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+(?:by|in|with|via|for)\b|[,|:]|$)", plain, re.I)
    if found:
        return _clean(found.group(1)), _clean(re.split(VEHICLE, found.group(2), flags=re.I)[0]), extra
    route = re.split(r"[:：|]", plain)
    candidates = [part for part in route if re.search(SEPARATOR, part)] or route
    part = candidates[0]
    head = re.split(rf"\s+{VEHICLE}", part, maxsplit=1, flags=re.I)[0]  # "A - B shuttle bus …" -> "A - B"
    head = re.sub(rf"^(?:{VEHICLE}\s*)+", "", head, flags=re.I)  # "Private transfer A to B" -> "A to B"
    ends = re.split(SEPARATOR, head, maxsplit=1)
    if len(ends) == 2:
        return _clean(ends[0]), _clean(ends[1]), extra
    return "", "", extra


def _clean(name):
    name = re.sub(r"(?i)\b(one[- ]way|round[- ]trip|return|optional|pick-?up(?:/drop-?off)?|drop-?off)\b", " ", name)
    name = re.sub(r"(?i)\s+(cross[- ]border|hotels?|downtown|city cent(?:re|er)|area|urban area)\s*$", "", name.strip())
    return " ".join(name.strip(" -–—,.").split())[:120]


def _trip_objects(text):
    pattern = re.compile(r'"@type"\s*:\s*\[?\s*"(BusTrip|Trip|TouristTrip|Product|Service|TaxiService)"')
    for match in pattern.finditer(text):
        raw = _enclosing_object(text, match.start())
        if raw:
            try:
                yield match.group(1), json.loads(raw)
            except ValueError:
                continue


def _name(value):
    if isinstance(value, dict):
        return (value.get("name") or "").strip()
    return value.strip() if isinstance(value, str) else ""


def extract(page, url=""):
    """Trip details from a booking page's HTML or pasted text. Keys are only present when found."""
    text = _unescape_layers(page)
    found = {}

    def add(key, value):
        if value and not found.get(key):
            found[key] = value.strip() if isinstance(value, str) else value

    for _kind, obj in _trip_objects(text):
        add("title", _name(obj.get("name")))
        add("origin", _name(obj.get("departureBusStop")) or _name(obj.get("departureStation")))
        add("destination", _name(obj.get("arrivalBusStop")) or _name(obj.get("arrivalStation")))
        add("operator", _name(obj.get("provider")) or _name(obj.get("brand")))
        add("number", _name(obj.get("busNumber")))
        offers = obj.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict) and offers.get("price"):
            add("price", f"{offers.get('priceCurrency', '')} {offers['price']}".strip())

    plain = re.sub(r"[ \t]+", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", "\n", text, flags=re.S | re.I))
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    title = _first(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page) or _first(r"<title>([^<]+)</title>", page)
    title = re.split(r"\s+[|]\s+", title)[0] if title else ""
    if not re.search(VEHICLE, title, re.I):
        # Pasted text: the title is the first line near the top naming both ends and a vehicle.
        title = next((line for line in lines[:80] if 12 < len(line) < 200 and re.search(VEHICLE, line, re.I)
                      and re.search(SEPARATOR + r"|\bfrom\b.+\bto\b", line, re.I)), title)
    add("title", title)

    origin, destination, extra = route_from_title(found.get("title", ""))
    add("origin", origin)
    add("destination", destination)
    add("seat_class", extra[:50])
    add("mode", mode_for(" ".join([found.get("title", ""), plain[:20000]])))
    add("price", _first(rf"(?:price|from|total)\s*[:：]?\s*{MONEY}", plain) or _first(MONEY, plain))
    add("duration", _first(r"(?:duration|journey time|travel time|approx\.?|about)\s*[:：]?\s*(\d+(?:\.\d+)?\s*(?:-\s*\d+\s*)?(?:h|hrs?|hours?|mins?|minutes)\b(?:\s*\d+\s*(?:mins?|minutes))?)", plain))
    add("seats", _first(r"(\d+\s*-\s*\d+\s*seat(?:er|s)?|\d+-seat(?:er)?)", plain))
    add("cancellation", _first(r"((?:free )?cancell?ation (?:by|before|up to)[^\n.]{3,80})", plain))

    notes = [found.get("title", "")]
    notes += [f"{label}: {found[key]}" for key, label in (("price", "Price"), ("duration", "Duration"), ("seats", "Vehicle"))
              if found.get(key)]
    if found.get("cancellation"):
        notes.append(found["cancellation"][0].upper() + found["cancellation"][1:])
    found["notes"] = "\n".join(line for line in notes if line)
    if url:
        found["url"] = url
    found = {k: (v[:500] if isinstance(v, str) else v) for k, v in found.items() if v}
    if not (found.get("origin") or found.get("destination")):
        raise PageError("Couldn't find where the trip goes from and to. Paste the page text below, or fill in the form.")
    return found
