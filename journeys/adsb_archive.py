"""Real flight routes from adsb.lol's open archive.

adsb.lol (https://adsb.lol) publishes every aircraft position its volunteer receivers picked up
each day as one archive on GitHub (about 3.7 GB, under the Open Database Licence). The background
job reads the newest day straight through once for all the flights waiting for a route, keeps
just their positions and saves nothing else. A route varies a little day to day, so the map shows
how the same flight number flew most recently. Flights beyond receiver coverage (much of inland
China, for one) aren't in the archive and stay as great-circle arcs.
"""

import gzip
import http.client
import io
import json
import logging
import re
import tarfile
import urllib.error
import urllib.request
import zlib
from collections import defaultdict
from datetime import date

from django.conf import settings
from django.utils import timezone

from .flight_tracks import TrackError, icao_ident
from .geo import distance_km, fill_gaps, simplify
from .http import USER_AGENT, get_json

log = logging.getLogger(__name__)

RELEASES = "https://api.github.com/repos/adsblol/globe_history_{year}/releases"
NEAR_KM = 30  # positions this close to an airport count as being at it
MAX_KMH = 1300  # faster than any airliner: a bad position
MIN_POINTS = 20  # fewer positions than this isn't a usable track


def enabled():
    return settings.ADSBLOL_TRACKS


def latest_day():
    """(date, part URLs) of the newest day in the archive."""
    this_year = timezone.now().year
    for year in (this_year, this_year - 1):  # early in January the new year may have nothing yet
        try:
            releases = get_json(RELEASES.format(year=year), {"per_page": 30})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        days = []
        for release in releases:
            found = re.match(r"v(\d{4})\.(\d\d)\.(\d\d)-planes-readsb-prod-\d+$", release.get("tag_name", ""))
            parts = sorted(a["browser_download_url"] for a in release.get("assets", []) if ".tar" in a["name"])
            if found and parts:
                days.append((date(*map(int, found.groups())), parts))
        if days:
            return max(days)
    raise TrackError("adsb.lol hasn't published a recent day.")


class _Parts(io.RawIOBase):
    """The day's archive parts (.tar.aa, .tar.ab, ...) read one after another as a single file."""

    def __init__(self, urls):
        self.urls, self.response = list(urls), None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.response is None:
                if not self.urls:
                    return 0
                request = urllib.request.Request(self.urls.pop(0), headers={"User-Agent": USER_AGENT})
                self.response = urllib.request.urlopen(request, timeout=60)
            count = self.response.readinto(buffer)
            if count:
                return count
            self.response.close()
            self.response = None

    def close(self):
        if self.response is not None:
            self.response.close()
        super().close()


def _open(urls):
    return io.BufferedReader(_Parts(urls), 1 << 20)


def find_tracks(legs):
    """(day, {leg pk: (path, message)}) for the legs found in the newest day of the archive. Legs
    missing from the result weren't in it; raises TrackError if the archive can't be read."""
    callsigns = {leg.pk: icao_ident(leg.number) for leg in legs}  # aircraft broadcast e.g. CPA156 for CX156
    try:
        day, urls = latest_day()
        with _open(urls) as stream:
            flown = _scan(stream, set(callsigns.values()))
    except (OSError, http.client.HTTPException, tarfile.TarError, zlib.error, ValueError, EOFError) as exc:
        raise TrackError(f"Couldn't read adsb.lol's archive ({exc}).")
    tracks = {}
    for leg in legs:
        callsign = callsigns[leg.pk]
        candidates = [_leg_part(positions, leg.origin.coords, leg.destination.coords) for positions in flown.get(callsign, [])]
        best = max(filter(None, candidates), key=len, default=None)
        if best:
            path = fill_gaps([list(leg.origin.coords)] + best + [list(leg.destination.coords)])
            tracks[leg.pk] = simplify(path, 0.003), f"Real track of {callsign} on {day:%-d %b %Y} (adsb.lol)."
    return day, tracks


def _scan(stream, callsigns):
    """{callsign: [positions of each aircraft that flew under it]} from one day's archive, where
    positions are (unix time, lat, lon). Only aircraft that used a wanted callsign are decoded."""
    needles = [f'"{callsign}'.encode() for callsign in callsigns]
    found = defaultdict(list)
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            if not member.isfile() or "trace_full_" not in member.name:
                continue
            raw = tar.extractfile(member).read()
            data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
            if not any(needle in data for needle in needles):
                continue
            for callsign, positions in _by_callsign(json.loads(data), callsigns).items():
                found[callsign].append(positions)
    return found


def _by_callsign(aircraft, callsigns):
    """Split one aircraft's day (a readsb trace) into the positions flown under each wanted callsign."""
    start, current, flown = aircraft.get("timestamp", 0), None, defaultdict(list)
    for point in aircraft.get("trace", []):
        details = point[8] if len(point) > 8 and isinstance(point[8], dict) else None
        if details and details.get("flight"):
            current = details["flight"].strip()
        if current in callsigns and point[1] is not None and point[2] is not None:
            flown[current].append((start + point[0], point[1], point[2]))
    return flown


def _leg_part(positions, origin, destination):
    """The positions flown from origin to destination, with bad positions dropped, or None. One
    aircraft may fly the same flight number onwards (Hong Kong-Taipei-Nagoya), so this stops on arrival."""
    clean = []
    for t, lat, lon in positions:
        if clean and distance_km(clean[-1][1:], (lat, lon)) / (max(t - clean[-1][0], 1) / 3600) > MAX_KMH:
            continue
        clean.append((t, lat, lon))
    near_origin = [i for i, p in enumerate(clean) if distance_km(p[1:], origin) < NEAR_KM]
    near_destination = [i for i, p in enumerate(clean) if distance_km(p[1:], destination) < NEAR_KM]
    arrival = next((i for i in near_destination if i >= (near_origin[0] if near_origin else 0)), None)
    if arrival is None:  # tracking stopped before the aircraft got there
        start, end = (near_origin[-1] if near_origin else 0), len(clean) - 1
    else:
        start, end = max((i for i in near_origin if i < arrival), default=0), arrival
        while end + 1 < len(clean) and distance_km(clean[end + 1][1:], destination) < NEAR_KM:
            end += 1
    flown = [[lat, lon] for _t, lat, lon in clean[start:end + 1]]
    if len(flown) < MIN_POINTS:
        return None
    first, last = flown[0], flown[-1]
    if distance_km(first, origin) > distance_km(first, destination) or distance_km(last, destination) > distance_km(last, origin):
        return None  # the same flight number in the other direction
    return flown
