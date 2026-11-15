"""Direct flights between two airports on a day, for the Journey page's International routes (signed in only).

The first airport's departures board for the day comes from AeroDataBox (the same RapidAPI key
as transport auto-fill), in two 12-hour pieces, the most it gives at once, and is kept for a
day (an hour for today's, whose times change) beside the city timetables, so every worker and
every destination from that airport share it. The free plan allows 400 units a month and each
piece costs 2, so a new airport and day uses 4; the units left are shown with the results.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import flights
from .http import USER_AGENT
from .models import Place

log = logging.getLogger(__name__)

BOARD_URL = f"https://{flights.API_HOST}/flights/airports/iata/{{code}}/{{start}}/{{end}}"
KEEP_SECS = 24 * 3600
KEEP_TODAY_SECS = 3600
HALVES = (("00:00", "11:59"), ("12:00", "23:59"))
NUMBER = re.compile(r"^[A-Z0-9]{2}\d{1,4}[A-Z]?$")


class FlightError(Exception):
    """Why flights can't be shown, in words for the page."""


def enabled():
    return flights.enabled()


def _folder():
    return Path(settings.TRANSIT_DIR) / "flights"


def airport(code):
    """The airport Place for an IATA code, or None."""
    code = str(code or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        return None
    return Place.objects.filter(kind=Place.Kind.AIRPORT, code=code).order_by("id").first()


def airport_json(place):
    return {"code": place.code, "name": place.name, "local": place.name_local, "city": place.city_name, "country": place.country,
            "lat": place.lat, "lon": place.lon}


def find_airports(query, limit=8):
    from .search import find_places

    return [airport_json(p) for p in find_places(query, kinds=[Place.Kind.AIRPORT], limit=limit * 2) if re.fullmatch(r"[A-Z]{3}", p.code or "")][:limit]


def _trim(departure):
    """What the page needs from one of AeroDataBox's departures, or None for cargo, codeshares and the unknown."""
    arrival = departure.get("arrival") or {}
    to = arrival.get("airport") or {}
    if departure.get("isCargo") or departure.get("codeshareStatus") == "IsCodeshared" or not to.get("iata"):
        return None
    leaving = departure.get("departure") or {}
    times = [(part.get("scheduledTime") or {}) for part in (leaving, arrival)]
    if not all(t.get("local") and t.get("utc") for t in times):
        return None
    airline = departure.get("airline") or {}
    return {
        "number": re.sub(r"\s+", "", departure.get("number") or "").upper(), "airline": airline.get("name", ""), "airline_code": airline.get("iata", ""),
        "to": to["iata"].upper(), "to_name": to.get("name", ""), "depart_local": times[0]["local"], "depart_utc": times[0]["utc"],
        "arrive_local": times[1]["local"], "arrive_utc": times[1]["utc"], "terminal": leaving.get("terminal", ""),
        "arrive_terminal": arrival.get("terminal", ""), "aircraft": (departure.get("aircraft") or {}).get("model", ""),
        "status": departure.get("status", ""),
    }


def _fetch(code, day, half):
    start, end = HALVES[half]
    url = BOARD_URL.format(code=code, start=f"{day}T{start}", end=f"{day}T{end}") + "?" + urllib.parse.urlencode({
        "withLeg": "true", "direction": "Departure", "withCancelled": "false", "withCodeshared": "false",
        "withCargo": "false", "withPrivate": "false", "withLocation": "false"})
    request = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": settings.AERODATABOX_RAPIDAPI_KEY, "X-RapidAPI-Host": flights.API_HOST, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(5_000_000)
            headers = response.headers
            data = json.loads(body) if response.status != 204 and body else {}
    except urllib.error.HTTPError as exc:
        if exc.code in (204, 404):
            return [], {}
        if exc.code in (401, 403):
            raise FlightError("The flight data service rejected the API key.")
        if exc.code == 429:
            raise FlightError("This month's free flight searches are used up. They start again next month.")
        if exc.code == 400:
            raise FlightError("The flight data service has no timetable for that day yet.")
        raise FlightError(f"The flight data service returned an error ({exc.code}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning("Flight board for %s failed: %s", code, type(exc).__name__)
        raise FlightError("The flight data service isn't answering right now. Please try again in a minute.")
    allowance = {}
    try:
        allowance = {"left": int(headers["X-RateLimit-API-Units-Remaining"]), "of": int(headers["X-RateLimit-API-Units-Limit"]),
                     "resets": int(time.time()) + int(headers["X-RateLimit-API-Units-Reset"])}
    except (KeyError, TypeError, ValueError):
        pass
    return [found for d in (data or {}).get("departures", []) if (found := _trim(d))], allowance


def _allowance(update=None):
    path = _folder() / "allowance.json"
    if update:
        path.write_text(json.dumps(update))
        return update
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def board(code, day):
    """Every departure from an airport on a (local) day, from the kept copy while it's fresh."""
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    keep = KEEP_TODAY_SECS if day <= timezone.localdate() + timedelta(days=1) else KEEP_SECS
    found = []
    for half in range(len(HALVES)):
        path = folder / f"{code}-{day.isoformat()}-{half}.json"
        try:
            if time.time() - path.stat().st_mtime < keep:
                found += json.loads(path.read_text())
                continue
        except (OSError, ValueError):
            pass
        piece, allowance = _fetch(code, day.isoformat(), half)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(piece))
        temporary.replace(path)
        if allowance:
            _allowance(allowance)
        found += piece
    return found


def _moment(text):
    """'2026-12-27 00:50+10:00' or '2026-12-26 14:50Z' -> an aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))


def _flight(found):
    leave, land = _moment(found["depart_local"]), _moment(found["arrive_local"])
    minutes = round((_moment(found["arrive_utc"]) - _moment(found["depart_utc"])).total_seconds() / 60)
    return {
        "number": found["number"], "airline": found["airline"], "airline_code": found["airline_code"],
        "depart": leave.strftime("%H:%M"), "arrive": land.strftime("%H:%M"), "days": (land.date() - leave.date()).days,
        "depart_day": leave.date().isoformat(), "arrive_day": land.date().isoformat(),
        "minutes": minutes, "duration": f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min",
        "terminal": found["terminal"], "arrive_terminal": found["arrive_terminal"], "aircraft": found["aircraft"], "status": found["status"],
    }


def search(origin, destination, day):
    """Direct flights from one airport to another leaving on a day (the first airport's date)."""
    if not enabled():
        raise FlightError("Flight search needs the AeroDataBox key.")
    a, b = airport(origin), airport(destination)
    if not a or not b:
        raise FlightError("Choose both airports from the lists.")
    if a.code == b.code:
        raise FlightError("Choose two different airports.")
    today = timezone.localdate()
    if day < today - timedelta(days=1):
        raise FlightError("Choose a date from today on.")
    if day > today + timedelta(days=365):
        raise FlightError("Choose a date within the next year.")
    found = sorted((_flight(f) for f in board(a.code, day) if f["to"] == b.code), key=lambda f: (f["depart"], f["number"]))
    allowance = _allowance()
    slug = lambda place: re.sub(r"[^a-z0-9]+", "-", place.city_name.lower()).strip("-")  # noqa: E731
    return {
        "from": airport_json(a), "to": airport_json(b), "day": day.isoformat(), "day_label": f"{day:%A %-d %B %Y}", "flights": found,
        "allowance": {"left": allowance["left"], "of": allowance["of"], "resets": datetime.fromtimestamp(allowance["resets"]).strftime("%-d %B")}
        if allowance.get("of") else None,
        "book": f"https://au.trip.com/flights/{slug(a)}-to-{slug(b)}/airfares-{a.code.lower()}-{b.code.lower()}/",
    }


def find(origin, destination, day, number):
    """One flight from the (kept) board, as search() shows it, or None."""
    a = airport(origin)
    if not a or not isinstance(day, date):
        return None
    for found in board(a.code, day):
        if found["to"] == str(destination).upper() and found["number"] == number:
            return _flight(found)
    return None
