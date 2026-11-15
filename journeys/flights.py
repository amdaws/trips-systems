"""Live flight schedules from AeroDataBox (via RapidAPI), used by transport auto-fill.

Needs AERODATABOX_RAPIDAPI_KEY in /etc/trips/trips.env (free "Basic" plan,
non-commercial). Lookups are cached so the monthly allowance goes a long way:
flights found are kept for 7 days, "not found" for a day.
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .http import USER_AGENT
from .models import FlightLookup, Place

log = logging.getLogger(__name__)

API_HOST = "aerodatabox.p.rapidapi.com"
API_URL = f"https://{API_HOST}/flights/number/{{number}}/{{date}}"
FOUND_FOR = timedelta(days=7)
NOT_FOUND_FOR = timedelta(days=1)


class LookupError(Exception):
    """Shown to you in the planner (bad key, allowance used up, service down)."""


def enabled():
    return bool(settings.AERODATABOX_RAPIDAPI_KEY)


def lookup(number, on_date):
    """Scheduled airports and times for a flight on a date, as auto-fill fields; None if not found."""
    if not enabled() or not number or not on_date:
        return None
    number = re.sub(r"\s+", "", number.upper())
    cached = FlightLookup.objects.filter(number=number, date=on_date).first()
    if cached and timezone.now() - cached.fetched_at < (FOUND_FOR if cached.data else NOT_FOUND_FOR):
        flights = cached.data
    else:
        flights = _fetch(number, on_date)
        FlightLookup.objects.update_or_create(number=number, date=on_date, defaults={"data": flights})
    return _to_fields(flights, on_date)


def _fetch(number, on_date):
    url = API_URL.format(number=urllib.parse.quote(number), date=on_date.isoformat())
    url += "?" + urllib.parse.urlencode({"dateLocalRole": "Departure", "withAircraftImage": "false", "withLocation": "false"})
    request = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": settings.AERODATABOX_RAPIDAPI_KEY, "X-RapidAPI-Host": API_HOST, "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            return json.loads(body) if response.status != 204 and body else []
    except urllib.error.HTTPError as exc:
        if exc.code in (204, 404):
            return []
        if exc.code in (401, 403):
            raise LookupError("The flight data service rejected the API key (check it's subscribed to the Basic plan).")
        if exc.code == 429:
            raise LookupError("This month's free flight lookups are used up; fill this one in by hand.")
        raise LookupError(f"The flight data service returned an error ({exc.code}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LookupError(f"The flight data service didn't respond ({exc}).")


def _to_fields(flights, on_date):
    usable = [f for f in flights if (f.get("departure") or {}).get("airport", {}).get("iata")
              and (f.get("arrival") or {}).get("airport", {}).get("iata")]
    if not usable:
        return None
    flight = usable[0]
    dep, arr = flight["departure"], flight["arrival"]
    dep_date, dep_time = _local(dep)
    arr_date, arr_time = _local(arr)
    return {
        "origin": _airport(dep["airport"]),
        "destination": _airport(arr["airport"]),
        "depart_date": dep_date or on_date,
        "depart_time": dep_time,
        "arrive_date": arr_date if arr_date and dep_date and arr_date > dep_date else None,
        "arrive_time": arr_time,
        "operator": (flight.get("airline") or {}).get("name", ""),
        "aircraft": (flight.get("aircraft") or {}).get("model", ""),
    }


def _local(movement):
    """'2026-12-27 00:50+10:00' -> (date, '00:50')."""
    value = ((movement.get("scheduledTime") or {}).get("local") or "")[:16]
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        return None, ""
    return moment.date(), moment.strftime("%H:%M")


def _airport(data):
    code = data["iata"].upper()
    place = Place.objects.filter(kind=Place.Kind.AIRPORT, code=code).first()
    if place:
        return place
    location = data.get("location") or {}
    if "lat" not in location or "lon" not in location:
        return None
    return Place.objects.create(
        kind=Place.Kind.AIRPORT, name=data.get("name") or code, code=code, country=(data.get("countryCode") or "")[:2],
        lat=location["lat"], lon=location["lon"], city=(data.get("municipalityName") or "")[:120],
        source="aerodatabox", source_id=code,
    )
