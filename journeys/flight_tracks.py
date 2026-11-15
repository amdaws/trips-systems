"""Real flight routes from the Flightradar24 API.

A flight's route varies a little day to day, so the planner draws the track of
the most recent completed flight with the same number between the same
airports (the Explorer plan reaches back 30 days). Gaps in coverage (oceans,
parts of China) are filled with great-circle arcs.

Flightradar24 data may be kept for 30 days at most, so the background job
fetches each track afresh after KEEP_DAYS, or redraws it as an arc if it can't
(see run_jobs). Needs FLIGHTRADAR24_API_TOKEN; a track costs about 80 credits
(Explorer plan: 30,000 a month).
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from django.conf import settings

from .geo import fill_gaps, simplify
from .http import USER_AGENT
from .models import Airline

log = logging.getLogger(__name__)

API = "https://fr24api.flightradar24.com/api"
GAP_KM = 300  # positions further apart than this are joined with a great-circle arc
KEEP_DAYS = 25  # stored tracks are fetched again before Flightradar24's 30-day storage limit
WINDOW_DAYS = 14  # the longest date range one flight-summary query may cover
SEARCH_DAYS = 28  # how far back to look for a recent flight (the Explorer plan keeps 30 days)
_last_call = [0.0]


class TrackError(Exception):
    """A reason the real track couldn't be used (shown on the leg)."""


def enabled():
    return bool(settings.FLIGHTRADAR24_API_TOKEN)


def _get(path, params=None):
    # Explorer plan: at most 10 requests a minute.
    wait = 6.5 - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {settings.FLIGHTRADAR24_API_TOKEN}", "Accept": "application/json",
        "Accept-Version": "v1", "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise TrackError("Flightradar24 rejected the API token.")
        if exc.code == 402:
            raise TrackError("The Flightradar24 credits for this month have run out.")
        if exc.code == 403:
            raise TrackError("The Flightradar24 plan doesn't include flight tracks.")
        if exc.code == 429:
            raise TrackError("Flightradar24's request limit was reached; use Retrace in a minute.")
        if exc.code == 400:
            raise TrackError(f"Flightradar24 couldn't read the request ({_details(exc)}).")
        raise TrackError(f"Flightradar24 returned an error ({exc.code}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise TrackError(f"Flightradar24 didn't respond ({exc}).")


def _details(exc):
    try:
        body = json.load(exc)
        return str(body.get("details") or body.get("message") or exc.reason)[:120]
    except (ValueError, AttributeError, OSError):
        return exc.reason


def icao_ident(number):
    """CX156 -> CPA156 (the ICAO callsign most airlines fly under)."""
    number = number.replace(" ", "").upper()
    airline = Airline.objects.filter(code=number[:2]).first()
    return (airline.icao + number[2:]) if airline and airline.icao else number


def _when(value):
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _latest_flight(leg, number):
    """The most recent completed flight with this number on the leg's route, or None."""
    searches = [{"flights": number}]
    callsign = icao_ident(number)
    if callsign != number:
        searches.append({"callsigns": callsign})  # some flights are only listed under their callsign
    now = datetime.now(UTC).replace(microsecond=0)
    for days_back in range(0, SEARCH_DAYS, WINDOW_DAYS):
        end = now - timedelta(days=days_back)
        window = {
            "flight_datetime_from": f"{end - timedelta(days=WINDOW_DAYS):%Y-%m-%d %H:%M:%S}",
            "flight_datetime_to": f"{end:%Y-%m-%d %H:%M:%S}",
            "sort": "desc", "limit": 20,
        }
        for search in searches:
            flights = _get("/flight-summary/full", {**search, **window}).get("data") or []
            if flights and not any(f.get("flight") == number or f.get("callsign") == callsign for f in flights):
                # The sandbox ignores the search and returns the same sample flight every time.
                raise TrackError("Flightradar24 returned unrelated flights: this is a sandbox token. "
                                 "Save the production token from Key management.")
            done = [
                f for f in flights
                if f.get("flight_ended") and f.get("datetime_landed")
                and f.get("orig_iata") == leg.origin.code
                and (f.get("dest_iata_actual") or f.get("dest_iata")) == leg.destination.code
            ]
            if done:
                return max(done, key=lambda f: _when(f["datetime_landed"]))
    return None


def flown_path(leg):
    """(path, message) for the leg's most recent real flight, or raise TrackError."""
    if not (leg.number and leg.origin.code and leg.destination.code):
        raise TrackError("Needs a flight number and both airport codes.")
    number = leg.number.replace(" ", "").upper()
    flight = _latest_flight(leg, number)
    if not flight:
        raise TrackError(f"No completed {leg.number} {leg.origin.code}→{leg.destination.code} flight in Flightradar24's last {SEARCH_DAYS} days.")
    tracks = _get("/flight-tracks", {"flight_id": flight["fr24_id"]})
    if isinstance(tracks, dict):
        tracks = tracks.get("data") or []
    positions = sorted((p for t in tracks for p in t.get("tracks") or []), key=lambda p: p.get("timestamp") or "")
    points = [[p["lat"], p["lon"]] for p in positions if p.get("lat") is not None and p.get("lon") is not None]
    if len(points) < 2:
        raise TrackError("Flightradar24 has no recorded positions for the latest flight.")
    path = fill_gaps([leg.origin.coords] + points + [leg.destination.coords], GAP_KM)
    flown_on = _when(flight["datetime_landed"]).astimezone(leg.destination.zone)
    return simplify(path, 0.003), f"Real track of {flight.get('flight') or number} on {flown_on:%-d %b %Y} (Flightradar24)."
