"""Place search over the local airport/station/city tables, and auto-fill parsing for transport."""

import re
from datetime import date, timedelta
from difflib import get_close_matches
from functools import lru_cache

from django.db.models.functions import Length

from .models import Airline, Leg, Place, normalise

KIND_RANK = {
    Place.Kind.CITY: 0, Place.Kind.AIRPORT: 1, Place.Kind.STATION: 2, Place.Kind.METRO: 3,
    Place.Kind.ATTRACTION: 4, Place.Kind.HOTEL: 5, Place.Kind.PIER: 6, Place.Kind.OTHER: 7,
}
MODE_KINDS = {
    Leg.Mode.FLIGHT: [Place.Kind.AIRPORT],
    Leg.Mode.TRAIN: [Place.Kind.STATION],
    Leg.Mode.METRO: [Place.Kind.METRO, Place.Kind.TRAM, Place.Kind.STATION],
}


def find_places(query, kinds=None, limit=12):
    raw = query.strip()
    q = normalise(raw)
    if not q:
        return []
    places = Place.objects.all()
    if kinds:
        places = places.filter(kind__in=kinds)
    exact_code = list(places.filter(code__iexact=raw)[:3]) if len(raw) == 3 and raw.isalpha() else []
    matches = places
    for term in q.split():
        matches = matches.filter(search__contains=term)
    candidates = exact_code + [p for p in matches.order_by(Length("name"))[: limit * 4] if p not in exact_code]

    def rank(place):
        starts = place.search.startswith(q) or (place.name_local or "").startswith(raw)
        return (place not in exact_code, not starts, KIND_RANK.get(place.kind, 9), len(place.name))

    return sorted(candidates, key=rank)[:limit]


def place_json(place):
    return {
        "id": place.pk,
        "label": str(place),
        "name": place.name,
        "local": place.name_local,
        "kind": place.kind,
        "kind_label": place.get_kind_display(),
        "code": place.code,
        "country": place.country,
    }


@lru_cache(maxsize=1)
def _station_names():
    """Compact names (no spaces) of every railway station, for fuzzy matching typos."""
    names = {}
    for pk, search in Place.objects.filter(kind=Place.Kind.STATION).values_list("pk", "search"):
        for token in search.split():
            if token.isascii() and len(token) > 3:
                names.setdefault(token, pk)
    return names


def match_station(text):
    """Best railway station for free text: Chinese, English, pinyin ('Shenzhenbei') or a near-miss typo."""
    cleaned = re.sub(r"\((north|south|east|west)\)|\b(railway|train|high[- ]speed)?\s*station\b", " ", text, flags=re.I).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None
    found = find_places(cleaned, kinds=[Place.Kind.STATION], limit=1)
    if found:
        return found[0]
    compact = normalise(cleaned).replace(" ", "")
    found = list(Place.objects.filter(kind=Place.Kind.STATION, search__contains=compact).order_by(Length("name"))[:1])
    if found:
        return found[0]
    close = get_close_matches(compact, list(_station_names()), n=1, cutoff=0.72)
    return Place.objects.filter(pk=_station_names()[close[0]]).first() if close else None


def match_airport(text):
    raw = text.strip()
    if re.fullmatch(r"[A-Za-z]{3}", raw):
        found = Place.objects.filter(kind=Place.Kind.AIRPORT, code=raw.upper()).first()
        if found:
            return found
    found = find_places(raw, kinds=[Place.Kind.AIRPORT], limit=1)
    return found[0] if found else None


# ---- Auto-fill parsing ----

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
YEAR = r"(?:\s+((?:19|20)\d{2}))?(?!\d)"  # an optional 4-digit year, so "27 Dec 0050" keeps 0050 as a time
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"), "dmy"),
    (re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])"), "dm"),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MON}{YEAR}", re.I), "d_mon"),
    (re.compile(rf"\b{MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?{YEAR}", re.I), "mon_d"),
]
TIME_RE = re.compile(r"(?<![\w:/.])(\d{1,2})[:.h]?(\d{2})\s*(am|pm|hrs|hr|h)?\s*(\+1)?(?![\w:/])", re.I)
CLASS_RE = re.compile(r"\(?\b(business|first|premium economy|economy|second|1st|2nd)\s*class\b\)?", re.I)
FLIGHT_RE = re.compile(r"\b([A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(\d{1,4})\b", re.I)
TRAIN_RE = re.compile(r"\b([GDCZTKYSL])(\d{1,5})\b", re.I)
SEPARATORS = re.compile(r"\s*(?:->|→|=>|–|—|>|\bto\b|,)\s*|\s+-\s+", re.I)
MODE_WORDS = {
    "flight": Leg.Mode.FLIGHT, "fly": Leg.Mode.FLIGHT, "train": Leg.Mode.TRAIN, "rail": Leg.Mode.TRAIN,
    "bus": Leg.Mode.BUS, "coach": Leg.Mode.BUS, "ferry": Leg.Mode.FERRY, "boat": Leg.Mode.BOAT, "cruise": Leg.Mode.BOAT,
    "metro": Leg.Mode.METRO, "subway": Leg.Mode.METRO, "mtr": Leg.Mode.METRO, "taxi": Leg.Mode.CAR,
    "didi": Leg.Mode.CAR, "car": Leg.Mode.CAR, "uber": Leg.Mode.CAR, "shuttle": Leg.Mode.SHUTTLE, "walk": Leg.Mode.WALK,
}
MODE_WORD_RE = re.compile(r"\b(" + "|".join(MODE_WORDS) + r")\b\s*[:\-]?", re.I)


def _resolve_year(month, day, year, trip, fallback):
    if year:
        year = int(year)
        return date(year + 2000 if year < 100 else year, month, day)
    years = [trip.start_date.year, trip.end_date.year] if trip else [(fallback or date.today()).year]
    for y in sorted(set(years)):
        candidate = date(y, month, day)
        if not trip or trip.start_date <= candidate <= trip.end_date:
            return candidate
    return date(years[0], month, day)


def _take_date(text, trip, fallback):
    for pattern, kind in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            if kind == "ymd":
                found = date(int(match[1]), int(match[2]), int(match[3]))
            elif kind == "dmy":
                found = _resolve_year(int(match[2]), int(match[1]), match[3], trip, fallback)
            elif kind == "dm":
                found = _resolve_year(int(match[2]), int(match[1]), None, trip, fallback)
            elif kind == "d_mon":
                found = _resolve_year(MONTHS[match[2][:3].lower()], int(match[1]), match[3], trip, fallback)
            else:
                found = _resolve_year(MONTHS[match[1][:3].lower()], int(match[2]), match[3], trip, fallback)
        except ValueError:
            continue
        return found, text[: match.start()] + " " + text[match.end():]
    return None, text


def _take_times(text):
    times = []
    for match in TIME_RE.finditer(text):
        hour, minute, suffix = int(match[1]), int(match[2]), (match[3] or "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            continue
        times.append((f"{hour:02d}:{minute:02d}", bool(match[4]), match))
        if len(times) == 2:
            break
    for _value, _plus, match in reversed(times):
        text = text[: match.start()] + " " + text[match.end():]
    return [(value, plus) for value, plus, _m in times], text


def parse_leg(text, trip=None, depart_date=None):
    """Free text -> suggested transport fields. Works with codes, names, the spreadsheet format and dates."""
    result = {"mode": "", "number": "", "operator": "", "origin": None, "destination": None, "depart_date": "",
              "arrive_date": "", "depart_time": "", "arrive_time": "", "next_day": False, "seat_class": "",
              "messages": [], "filled": []}
    work = f" {text.strip()} "

    found_date, work = _take_date(work, trip, depart_date)
    if found_date:
        result["depart_date"] = found_date.isoformat()

    seat = CLASS_RE.search(work)
    if seat:
        result["seat_class"] = seat[1].title().replace("1St", "First").replace("2Nd", "Second")
        work = work[: seat.start()] + " " + work[seat.end():]

    times, work = _take_times(work)
    if times:
        result["depart_time"] = times[0][0]
    if len(times) > 1:
        result["arrive_time"], result["next_day"] = times[1]

    mode_word = MODE_WORD_RE.search(work)
    mode = MODE_WORDS[mode_word[1].lower()] if mode_word else ""
    if mode_word:
        work = work[: mode_word.start()] + " " + work[mode_word.end():]

    codes = [c for c in re.findall(r"\b[A-Z]{3}\b", work) if Place.objects.filter(kind=Place.Kind.AIRPORT, code=c).exists()]
    train, flight = TRAIN_RE.search(work), FLIGHT_RE.search(work)
    known = None
    if not mode:
        if len(codes) >= 2:
            mode = Leg.Mode.FLIGHT
        elif train and not (flight and len(flight[1]) == 2 and flight[1][0].isalpha() and flight[1][1].isalpha()):
            mode = Leg.Mode.TRAIN
        elif flight:
            mode = Leg.Mode.FLIGHT

    number_match = train if mode == Leg.Mode.TRAIN else flight if mode == Leg.Mode.FLIGHT else (train or flight)
    if number_match:
        result["number"] = (number_match[1] + number_match[2]).upper()
        work = work[: number_match.start()] + " " + work[number_match.end():]
        known = Leg.objects.filter(number__iexact=result["number"]).select_related("origin", "destination").order_by("-id").first()
        if not mode and known:
            mode = known.mode
    result["mode"] = mode or ""

    if mode == Leg.Mode.FLIGHT and result["number"]:
        airline = Airline.objects.filter(code=result["number"][:2]).first()
        result["operator"] = airline.name if airline else ""
    elif mode == Leg.Mode.TRAIN:
        result["operator"] = "China Railway"

    # Places: whatever text is left, split on arrows, "to", commas or dashes.
    work = re.sub(r"^\s*from\s+", " ", work, flags=re.I)
    parts = [p.strip(" :-") for p in SEPARATORS.split(work) if p and p.strip(" :-")]
    if len(parts) == 1 and len(codes) >= 2:
        parts = codes[:2]
    if len(parts) >= 2:
        matcher = match_airport if mode == Leg.Mode.FLIGHT else match_station if mode == Leg.Mode.TRAIN else (
            lambda t: (find_places(t, limit=1) or [None])[0])
        for key, part in (("origin", parts[0]), ("destination", parts[-1])):
            place = matcher(part)
            if place:
                result[key] = place_json(place)
            else:
                result["messages"].append(f"Couldn't find '{part}'. Pick it in the From/To box instead.")

    # Live schedule for that date (AeroDataBox), when a key is set and something's still missing.
    lookup_date = found_date or depart_date
    missing = not (result["origin"] and result["destination"] and result["depart_time"] and result["arrive_time"])
    if mode == Leg.Mode.FLIGHT and result["number"] and lookup_date and missing:
        from . import flights

        if flights.enabled():
            try:
                live = flights.lookup(result["number"], lookup_date)
            except flights.LookupError as exc:
                live = None
                result["messages"].append(str(exc))
            if live:
                for key in ("origin", "destination"):
                    if not result[key] and live[key]:
                        result[key] = place_json(live[key])
                if not result["depart_time"]:
                    result["depart_time"] = live["depart_time"]
                if not result["arrive_time"]:
                    result["arrive_time"] = live["arrive_time"]
                    result["next_day"] = bool(live["arrive_date"])
                result["operator"] = live["operator"] or result["operator"]
                result["depart_date"] = result["depart_date"] or live["depart_date"].isoformat()
                result["messages"] = [m for m in result["messages"] if not m.startswith("Couldn't find")]
                aircraft = f", {live['aircraft']}" if live["aircraft"] else ""
                result["messages"].append(f"Schedule from AeroDataBox for {lookup_date:%a %-d %b}{aircraft}.")
            else:
                result["messages"].append(f"No published schedule found for {result['number']} on {lookup_date:%a %-d %b} (it may not be on sale yet).")

    # Fill gaps from the same service already in any trip (e.g. CX156 typed on its own).
    if known:
        before = dict(result)
        for key, value in (("origin", known.origin), ("destination", known.destination)):
            if not result[key]:
                result[key] = place_json(value)
        if not result["depart_time"] and known.depart_time:
            result["depart_time"] = known.depart_time.strftime("%H:%M")
        if not result["arrive_time"] and known.arrive_time:
            result["arrive_time"] = known.arrive_time.strftime("%H:%M")
            result["next_day"] = known.arrives_next_day
        result["operator"] = result["operator"] or known.operator
        result["seat_class"] = result["seat_class"] or known.seat_class
        if result != before:
            # The typed places win; a "couldn't find" message is moot once the known service filled it.
            result["messages"] = [m for m in result["messages"] if not m.startswith("Couldn't find")]
            result["messages"].append(f"Filled in from {known.number} in '{known.trip}'.")

    if result["arrive_time"] and result["depart_time"] and result["arrive_time"] < result["depart_time"]:
        result["next_day"] = True
    base = date.fromisoformat(result["depart_date"]) if result["depart_date"] else depart_date
    if result["next_day"] and base:
        result["arrive_date"] = (base + timedelta(days=1)).isoformat()

    if not result["number"] and not mode:
        result["messages"].append("Couldn't recognise a flight or train number. You can still fill the form in below.")
    elif not (result["origin"] and result["destination"]):
        hint = "the two airports, e.g. BNE HKG or Brisbane to Hong Kong" if mode == Leg.Mode.FLIGHT else "the two stations or places"
        result["messages"].append(f"Add {hint}.")
    result["filled"] = [k for k in ("mode", "number", "operator", "origin", "destination", "depart_date", "depart_time",
                                    "arrive_time", "arrive_date", "seat_class") if result[k]]
    return result
