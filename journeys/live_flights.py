"""Live flight positions from adsb.lol, for the trip map while a flight is in the air.

adsb.lol's free API (https://api.adsb.lol, Open Database Licence) gives an aircraft's latest position
by callsign. From an hour before a flight's departure until three hours after its arrival, the
position is fetched at most every 30 seconds: by the trip page while someone's looking, and by the
background job every two minutes regardless. Each position is kept, so the map can draw the path
flown so far. After landing, the background job saves that recording as the leg's route (gaps over
the sea, beyond the receivers, filled with great-circle arcs).

Each flight's recording is one small file in transit/live-flights/. Only the aircraft's position
is fetched and kept; nothing about who's looking.
"""

import fcntl
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta

from django.utils import timezone

from . import timetable
from .flight_tracks import icao_ident
from .geo import distance_km, fill_gaps, great_circle, simplify
from .http import get_json
from .models import Leg

log = logging.getLogger(__name__)

API = "https://api.adsb.lol/v2/callsign/{callsign}"
BEFORE = timedelta(hours=1)  # start watching this long before departure (taxiing, a gate change)
AFTER = timedelta(hours=3)  # and keep going this long after arrival (delays)
CRUISE_KMH = 800  # for flights without an arrival time
FRESH_SECS = 30  # adsb.lol is asked at most this often per flight
RETRY_SECS = 60  # after a failed request
LOST_SECS = 300  # no position for this long: out of receiver range
NEAR_KM = 30  # this close to the destination, low and slow, counts as landed
MIN_POINTS = 8  # fewer airborne positions than this isn't worth keeping as the route
KEEP_DAYS = 3  # recordings are deleted this long after they were last touched


def _folder():
    path = timetable.folder() / "live-flights"
    path.mkdir(mode=0o750, exist_ok=True)
    return path


def _expected_arrival(leg):
    if leg.arrives_at and leg.arrives_at > leg.departs_at:
        return leg.arrives_at
    hours = distance_km(leg.origin.coords, leg.destination.coords) / CRUISE_KMH + 0.5
    return leg.departs_at + timedelta(hours=hours)


def window(leg):
    """(start, end) of the time this flight is watched."""
    return leg.departs_at - BEFORE, _expected_arrival(leg) + AFTER


def trackable(leg):
    return leg.mode == Leg.Mode.FLIGHT and bool(leg.number) and leg.depart_time is not None


def watched(legs, now=None):
    """The legs being watched now, from those given."""
    now = now or timezone.now()
    return [leg for leg in legs if trackable(leg) and window(leg)[0] <= now <= window(leg)[1]]


def next_start(legs, now=None):
    """Seconds until the next of these flights starts being watched, or None."""
    now = now or timezone.now()
    starts = [window(leg)[0] for leg in legs if trackable(leg) and window(leg)[0] > now]
    return int((min(starts) - now).total_seconds()) if starts else None


# ---- The recording: one file per leg ----

def _path(leg):
    return _folder() / f"leg-{leg.pk}.json"


def _blank(leg):
    return {"callsign": icao_ident(leg.number), "date": leg.depart_date.isoformat(), "fetched": 0, "failed": 0,
            "positions": [], "saved": False}


def _read(leg):
    try:
        record = json.loads(_path(leg).read_text())
    except (FileNotFoundError, ValueError):
        return _blank(leg)
    if record.get("date") != leg.depart_date.isoformat() or record.get("callsign") != icao_ident(leg.number):
        return _blank(leg)  # the leg was changed to another day or flight since
    return record


def _write(leg, record):
    path = _path(leg)
    handle, temp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(handle, "w") as out:
        json.dump(record, out, separators=(",", ":"))
    os.replace(temp, path)


class _Locked:
    """Only one process at a time updates a flight's recording (the site's workers and the job)."""

    def __init__(self, leg):
        self.path = _path(leg).with_suffix(".lock")

    def __enter__(self):
        self.file = open(self.path, "w")
        fcntl.flock(self.file, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.file.close()


def _on_route(leg, lat, lon):
    """Whether a position is plausibly on the way from the leg's origin to its destination (not the
    same flight number on another day's sector, or a bad position)."""
    a, b, p = leg.origin.coords, leg.destination.coords, (lat, lon)
    return distance_km(a, p) + distance_km(p, b) <= distance_km(a, b) * 1.25 + 300


def _fetch(leg, record, now_secs):
    """Ask adsb.lol where the flight is and add the position to the recording."""
    try:
        data = get_json(API.format(callsign=urllib.parse.quote(record["callsign"])), timeout=8)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("Live position for %s failed: %s", record["callsign"], exc)
        record["failed"] = now_secs
        return
    record["fetched"], record["failed"] = now_secs, 0
    reported = (data.get("now") or now_secs * 1000) / 1000
    found = []
    for aircraft in data.get("ac") or []:
        lat, lon = aircraft.get("lat"), aircraft.get("lon")
        if lat is None or lon is None or (aircraft.get("flight") or "").strip() != record["callsign"]:
            continue
        if not _on_route(leg, lat, lon):
            continue
        altitude = aircraft.get("alt_baro")
        altitude = 0 if altitude == "ground" else altitude if isinstance(altitude, (int, float)) else None
        speed = aircraft.get("gs")
        found.append([
            round(reported - (aircraft.get("seen_pos") or 0)), round(lat, 5), round(lon, 5), altitude,
            round(speed * 1.852) if isinstance(speed, (int, float)) else None, aircraft.get("track"),
        ])
    if not found:
        return
    positions = record["positions"]
    if positions:  # more than one aircraft on the callsign: the one that carries on from the last position
        found.sort(key=lambda p: distance_km(positions[-1][1:3], p[1:3]))
    best = found[0]
    if not positions or best[0] > positions[-1][0]:
        positions.append(best)


def refresh(leg, now=None):
    """The flight's recording, brought up to date if it's older than FRESH_SECS."""
    now_secs = (now or timezone.now()).timestamp()
    record = _read(leg)
    if record["saved"] or now_secs - record["fetched"] < FRESH_SECS or now_secs - record["failed"] < RETRY_SECS:
        return record
    with _Locked(leg):
        record = _read(leg)  # another process may have just fetched it
        if now_secs - record["fetched"] < FRESH_SECS or now_secs - record["failed"] < RETRY_SECS:
            return record
        _fetch(leg, record, now_secs)
        _write(leg, record)
    return record


# ---- What the map shows ----

def _airborne(leg, positions):
    """The positions from take-off on: off the ground and away from the origin airport."""
    for i, p in enumerate(positions):
        if (p[3] is None or p[3] > 0) and distance_km(leg.origin.coords, p[1:3]) > 3:
            return positions[i:]
    return []


def status(leg, record, now=None):
    """waiting, flying, out_of_range or landed."""
    now = now or timezone.now()
    flown = _airborne(leg, record["positions"])
    if not flown:
        return "waiting"
    last = flown[-1]
    age = now.timestamp() - last[0]
    near = distance_km(last[1:3], leg.destination.coords)
    low = last[3] is not None and last[3] <= 1500
    if (near < NEAR_KM and (low or age > LOST_SECS)) or (near < 2 * NEAR_KM and last[3] == 0):
        return "landed"
    if now > window(leg)[1]:
        return "landed"
    return "flying" if age <= LOST_SECS else "out_of_range"


def _along(path, fraction):
    """The point a fraction of the way along a path."""
    lengths = [0.0]
    for a, b in zip(path, path[1:]):
        lengths.append(lengths[-1] + distance_km(a, b))
    target = lengths[-1] * fraction
    for i in range(1, len(path)):
        if lengths[i] >= target:
            f = (target - lengths[i - 1]) / ((lengths[i] - lengths[i - 1]) or 1)
            a, b = path[i - 1], path[i]
            return [round(a[0] + (b[0] - a[0]) * f, 5), round(a[1] + (b[1] - a[1]) * f, 5)]
    return list(path[-1])


def estimate(leg, flown, now):
    """Where the plane probably is while it's out of receiver range: carried on along the planned
    route from where it was last seen, arriving when the timetable says (allowing for a late start)."""
    planned = leg.path or great_circle(leg.origin.coords, leg.destination.coords)
    last = flown[-1]
    nearest = min(range(len(planned)), key=lambda i: distance_km(planned[i], last[1:3]))
    ahead = [last[1:3]] + [list(p) for p in planned[nearest + 1:]] + [list(leg.destination.coords)]
    late = max(0, flown[0][0] - (leg.departs_at.timestamp() + 20 * 60))  # taxiing takes about 20 minutes
    due = _expected_arrival(leg).timestamp() + late
    fraction = 0.97 if due <= last[0] else min(0.97, (now.timestamp() - last[0]) / (due - last[0]))
    return _along(ahead, fraction)


def show(leg, record, now=None):
    """The live entry for the map: no flight number or callsign, which visitors never see."""
    now = now or timezone.now()
    state = status(leg, record, now)
    flown = _airborne(leg, record["positions"])
    entry = {"id": leg.pk, "status": state, "position": None, "estimate": None, "trail": []}
    if flown:
        last = flown[-1]
        entry["position"] = {"lat": last[1], "lon": last[2], "altitude_ft": last[3], "kmh": last[4], "heading": last[5],
                             "seen_secs": max(0, round(now.timestamp() - last[0]))}
        entry["trail"] = simplify([list(leg.origin.coords)] + [p[1:3] for p in flown], 0.01)
        if state == "out_of_range":
            entry["estimate"] = estimate(leg, flown, now)
    return entry


def for_trip(legs, now=None):
    """{"flights": [...], "poll": seconds until it's worth asking again (None: no flights coming)}."""
    now = now or timezone.now()
    flights = [show(leg, refresh(leg, now), now) for leg in watched(legs, now)]
    if any(f["status"] != "landed" for f in flights):
        poll = FRESH_SECS
    else:
        later = next_start(legs, now)
        poll = min(later, 6 * 3600) if later is not None else (300 if flights else None)
        poll = max(poll, 60) if poll is not None else None
    return {"flights": flights, "poll": poll}


# ---- The background job ----

def record_all(now=None):
    """Bring every watched flight's recording up to date; once a flight has landed, save what was
    recorded as its route. Returns lines for the job's log."""
    now = now or timezone.now()
    lines = []
    legs = Leg.objects.filter(mode=Leg.Mode.FLIGHT, depart_date__gte=(now - timedelta(days=2)).date(),
                              depart_date__lte=(now + timedelta(days=1)).date()).exclude(number="")
    for leg in watched(legs.select_related("origin", "destination"), now):
        record = refresh(leg, now)
        state = status(leg, record, now)
        if state == "landed" and not record["saved"]:
            lines.append(save_route(leg, record))
    _tidy()
    return lines


def save_route(leg, record):
    """Save a landed flight's recording as the leg's drawn route."""
    from . import routing

    flown = _airborne(leg, record["positions"])
    with _Locked(leg):
        record = _read(leg)
        record["saved"] = True
        _write(leg, record)
    if len(flown) < MIN_POINTS:
        return f"{leg}: landed, but only {len(flown)} positions were recorded; the route is left as it was."
    path = fill_gaps([list(leg.origin.coords)] + [p[1:3] for p in flown] + [list(leg.destination.coords)])
    day = datetime.fromtimestamp(flown[0][0], tz=UTC).astimezone(leg.origin.zone)
    message = f"Real track of {record['callsign']} on {day:%-d %b %Y}, recorded live (adsb.lol)."
    routing._save(leg, simplify(path, 0.003), Leg.PathSource.LIVE, Leg.PathStatus.OK, message)
    return f"{leg}: {message}"


def _tidy():
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in _folder().iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            pass
