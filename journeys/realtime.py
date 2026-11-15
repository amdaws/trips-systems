"""Live times for the journey planner: GTFS-realtime trip updates from each city's agency.

Brisbane's (Translink) are open; Sydney's use the Transport for NSW key (TFNSW_API_KEY) and
Melbourne's a Transport Victoria open data key (TRANSPORT_VIC_API_KEY), without which Melbourne
shows timetabled times. Each feed is fetched at most once a minute, whichever page asks first,
and kept as a file so the site's workers share it; if a feed is down, a copy up to five minutes
old is used, then none (the planner says the times are timetabled).

Only service data is fetched and kept here; nothing about the person asking.
"""

import logging
import os
import tempfile
import time
import urllib.request

from django.conf import settings

from . import timetable
from .http import USER_AGENT

log = logging.getLogger(__name__)

FRESH_SECS = 60
STALE_SECS = 300
NSW = "https://api.transport.nsw.gov.au"
VIC = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1"

# For each city: (the timetable feed it updates, address, which key).
FEEDS = {
    "brisbane": [(0, "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates", None)],
    "sydney": [
        (0, f"{NSW}/v2/gtfs/realtime/sydneytrains", "nsw"),
        (1, f"{NSW}/v2/gtfs/realtime/metro", "nsw"),
        (2, f"{NSW}/v2/gtfs/realtime/lightrail/innerwest", "nsw"),
        (3, f"{NSW}/v1/gtfs/realtime/lightrail/cbdandsoutheast", "nsw"),
        (4, f"{NSW}/v1/gtfs/realtime/lightrail/parramatta", "nsw"),
        (5, f"{NSW}/v1/gtfs/realtime/ferries/sydneyferries", "nsw"),
        (6, f"{NSW}/v1/gtfs/realtime/ferries/MFF", "nsw"),
        (7, f"{NSW}/v1/gtfs/realtime/buses", "nsw"),
    ],
    "melbourne": [(0, f"{VIC}/metro/trip-updates", "vic"), (1, f"{VIC}/tram/trip-updates", "vic"), (3, f"{VIC}/bus/trip-updates", "vic")],
}


def _headers(key):
    headers = {"User-Agent": USER_AGENT}
    if key == "nsw":
        if not settings.TFNSW_API_KEY:
            return None
        headers["Authorization"] = f"apikey {settings.TFNSW_API_KEY}"
    elif key == "vic":
        if not settings.TRANSPORT_VIC_API_KEY:
            return None
        # The portal's page names KeyID; the feeds' API description names the subscription-key header. Send both.
        headers["KeyID"] = headers["Ocp-Apim-Subscription-Key"] = settings.TRANSPORT_VIC_API_KEY
    return headers


def _cache_path(slug, index):
    folder = timetable.folder() / "live"
    folder.mkdir(mode=0o750, exist_ok=True)
    return folder / f"{slug}-{index}.pb"


def _feed_bytes(slug, index, url, key):
    """The feed's latest copy: fresh from the agency at most once a minute, else the kept one
    (up to five minutes old). None if there's nothing usable."""
    path = _cache_path(slug, index)
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        age = None
    if age is not None and age < FRESH_SECS:
        return path.read_bytes(), age
    headers = _headers(key)
    if headers is None:
        return None, None
    failed = path.with_suffix(".failed")
    try:
        recently_failed = time.time() - failed.stat().st_mtime < STALE_SECS
    except FileNotFoundError:
        recently_failed = False
    if recently_failed:  # a feed that just failed (a bad key, an outage) waits five minutes before it's tried again
        return (path.read_bytes(), age) if age is not None and age < STALE_SECS else (None, None)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=8) as response:
            data = response.read(20_000_000)
        handle, temp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, path)
        failed.unlink(missing_ok=True)
        return data, 0
    except Exception as exc:
        log.warning("Live times from %s failed: %s", url, exc)
        failed.touch()
        if age is not None and age < STALE_SECS:
            return path.read_bytes(), age
        return None, None


def _feed(data):
    from google.transit import gtfs_realtime_pb2  # loaded only when live times are needed

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)
    positions = {entity.trip_update.trip.trip_id: n for n, entity in enumerate(feed.entity)
                 if entity.HasField("trip_update") and entity.trip_update.trip.trip_id}
    return feed, positions


def _update(entity):
    """{"cancelled", "start_date", "stops": [(seq or None, stop id, arrival (delay, time), departure (delay, time), skipped)]}."""
    from google.transit import gtfs_realtime_pb2

    update = entity.trip_update
    skipped = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED

    def event(part):
        return (part.delay if part.HasField("delay") else None, part.time if part.HasField("time") and part.time else None)

    stops = [(
        stop.stop_sequence if stop.HasField("stop_sequence") else None, stop.stop_id or "",
        event(stop.arrival) if stop.HasField("arrival") else (None, None),
        event(stop.departure) if stop.HasField("departure") else (None, None),
        stop.schedule_relationship == skipped,
    ) for stop in update.stop_time_update]
    return {"cancelled": update.trip.schedule_relationship == gtfs_realtime_pb2.TripDescriptor.CANCELED,
            "start_date": update.trip.start_date, "stops": stops}


def _feeds(slug):
    """[(feed index, parsed feed, {trip id: position})] for the city's feeds that answered, and their
    status. Parsed for each search and let go after it (a parsed copy in every worker would hold
    tens of megabytes), from the file the workers share."""
    found, answered, oldest, version = [], 0, 0, []
    feeds = FEEDS.get(slug, [])
    for index, url, key in feeds:
        data, age = _feed_bytes(slug, index, url, key)
        if data is None:
            continue
        try:
            feed, positions = _feed(data)
        except Exception as exc:
            log.warning("Live times from %s couldn't be read: %s", url, exc)
            continue
        answered += 1
        oldest = max(oldest, round(age or 0))
        found.append((index, feed, positions))
        version.append((index, len(data), hash(data[:256]), hash(data[-256:])))
    return found, {"feeds": answered, "of": len(feeds), "age": oldest, "version": tuple(version)}


def updated(slug):
    """({(feed index, trip id): fetch the update} for every trip with live news, and the feeds' status).
    Each update is turned into Python only if it's asked for."""
    found = {}
    feeds, status = _feeds(slug)
    for index, feed, positions in feeds:
        for trip_id, position in positions.items():
            found[(index, trip_id)] = (feed, position)
    return found, status


def read(handle):
    feed, position = handle
    return _update(feed.entity[position])


def updates(slug, wanted=None):
    """({(timetable feed, trip id): update} for the wanted trips (all if None), and the feeds' status)."""
    handles, status = updated(slug)
    keys = handles.keys() if wanted is None else [key for key in wanted if key in handles]
    return {key: read(handles[key]) for key in keys}, status
