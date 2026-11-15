"""Trains, metro, trams and ferries in Brisbane, Melbourne and Sydney, from the official timetables.

Each city's public transport agency publishes its timetable as open data in GTFS form: Translink
(South East Queensland), Transport Victoria (PTV) and Transport for NSW, all under CC BY 4.0.
Once a week the background job downloads each city's feed and keeps what the planner needs as a
MetroNetwork (source "timetable"):

- stations: train stations, Brisbane Metro and tram stops, ferry wharves, with map positions;
- lines: each line's name, official colour and operator;
- graph: the travel time between neighbouring stations on each line (from real trips), plus
  short walks between nearby stations to change between them;
- shapes: each line's real path, for drawing rides on the map.

The stations also become Places, so they can be picked as the From and To of a transport leg.
Journeys are then worked out here (fewest minutes, with a few minutes added for each change),
giving the same kind of route MetroMan gives for Chinese cities, so stays and activities get
the same directions, and transport legs get their line, journey time and real route.

Buses aren't included (except the Brisbane Metro), and Sydney needs a free Transport for NSW
API key (TFNSW_API_KEY).
"""

import csv
import functools
import heapq
import io
import json
import logging
import re
import shutil
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import timetable
from .geo import distance_km, simplify
from .http import USER_AGENT
from .models import MetroNetwork, Place

log = logging.getLogger(__name__)

REFRESH_DAYS = 7
RETRY_HOURS = 6
MAX_DOWNLOAD = 800_000_000  # bytes; Melbourne's full feed is about 260 MB
TRIPS_PER_PATTERN = 2  # timetabled trips read per line, direction and route shape
CHANGE_MIN = 4  # added for each change of line: walking between platforms and waiting
WALK_KM = 0.3  # stations this close are linked by a short walk
WALK_SPEED_KMH = 4.5
SHAPE_MATCH_KM = 1.0  # a ride is drawn along its line's real path when its stations lie this close to it

SEQ_URL = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"
# PTV's GTFS file gets a new address (resource id) when it's republished, so each build looks up the
# current one in the data catalogue (_ptv_url), with the last one seen (Nov 2026) as the fallback.
PTV_URL = ("https://opendata.transport.vic.gov.au/dataset/3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
           "8af7737c-5c9e-4022-aa98-911aa04370a7/download/gtfs.zip")
PTV_CATALOGUE = "https://opendata.transport.vic.gov.au/api/3/action/package_show?id=gtfs-schedule"
PTV_HOST = "https://opendata.transport.vic.gov.au/"


@functools.cache
def _ptv_url():
    """The current address of PTV's GTFS file, from the data catalogue (CKAN), or None if it can't be
    found (the last known address is used then). Looked up once per run, not once per feed."""
    try:
        request = urllib.request.Request(PTV_CATALOGUE, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            resources = json.loads(response.read(1 << 20))["result"]["resources"]
        for resource in resources:
            url = str(resource.get("url") or "")
            # Only a file on PTV's own site: the catalogue's answer never sends the server anywhere else.
            if url.startswith(PTV_HOST) and url.endswith("/gtfs.zip") and "GTFS" in str(resource.get("name") or "").upper():
                return url
        log.warning("PTV catalogue lists no GTFS file; using the last known address")
    except Exception as exc:
        log.warning("PTV catalogue lookup failed (%s); using the last known address", exc)
    return None


NSW_API = "https://api.transport.nsw.gov.au"

MODE_KIND = {"train": Place.Kind.STATION, "metro": Place.Kind.METRO, "tram": Place.Kind.TRAM, "ferry": Place.Kind.PIER}
MODE_ORDER = ["train", "metro", "tram", "ferry"]  # a station's main kind, when it serves several
MODE_COLOUR = {"train": "#0f6cbd", "metro": "#00a3a3", "tram": "#7a4fb3", "ferry": "#0086c0", "bus": "#d4541b"}
MODE_WORD = {"train": "train", "metro": "Metro", "tram": "tram", "ferry": "ferry", "bus": "bus"}
ANY_LINE = 3  # when this many lines make the same ride, it reads "Any train" rather than one of them


def _hex(value, fallback):
    value = (value or "").strip().lstrip("#")
    return f"#{value.upper()}" if re.fullmatch(r"[0-9A-Fa-f]{6}", value) else fallback


def _line(key, mode, short, long, route, operator, planner_only=False):
    """A line from a feed's route. planner_only lines (buses) go in the journey planner's timetable
    but not the network the trip pages use, which would otherwise gain tens of thousands of stops."""
    return {
        "key": key, "mode": mode, "planner_only": planner_only, "name": short.strip() or long.strip(), "long": long.strip() or short.strip(),
        "colour": _hex(route.get("route_color"), MODE_COLOUR[mode]),
        "text": _hex(route.get("route_text_color"), "#FFFFFF"), "operator": operator,
    }


# ---------- what each city's feeds contain ----------


def _seq(route, mode=None):
    """Translink: Queensland Rail City network trains, Brisbane Metro, ferries and the Gold Coast's G:link."""
    kind, short, long = route["route_type"], route["route_short_name"], route["route_long_name"]
    if kind == "2":
        ends = sorted(part.strip() for part in long.split(" - "))
        name = f"{ends[0]} loop" if len(set(ends)) == 1 else " – ".join(part.strip() for part in long.split(" - "))
        return _line("train:" + "|".join(ends), "train", name, name, route, "Queensland Rail")
    if kind == "4":
        return _line(f"ferry:{short}", "ferry", short, long, route, "Brisbane ferries" if short.startswith("F") and short != "F50" else "Moreton Bay ferries")
    if kind == "0":
        return _line(f"tram:{short}", "tram", "G:link", long, route, "G:link")
    if kind == "3" and short in ("M1", "M2"):
        return _line(f"metro:{short}", "metro", short, long, route, "Brisbane Metro")
    if kind == "3":
        return _line(f"bus:{short}", "bus", short, long, route, "Translink bus", planner_only=True)
    return None


def _ptv(route, mode):
    """Transport Victoria: metropolitan trains, trams, V/Line regional trains, metropolitan buses and
    SkyBus (replacement buses left out)."""
    short, long = route["route_short_name"], route["route_long_name"]
    if short == "Replacement Bus" or route["route_id"].rstrip(":").endswith("-R"):
        return None
    if mode in ("bus", "skybus"):
        operator = "SkyBus" if mode == "skybus" else ""
        return _line(f"{mode}:{route['route_id']}", "bus", short or "SkyBus", long, route, operator, planner_only=True)
    if mode == "tram":
        return _line(f"tram:{short}", "tram", short, f"Route {short} {long}", route, "Yarra Trams")
    if mode == "vline":
        return _line(f"vline:{short or long}", "train", short or long, long, route, "V/Line")
    return _line(f"train:{long}", "train", re.sub(r"\s+Line$", "", long), long, route, "Metro Trains Melbourne")


NSW_OPERATORS = {"train": "Sydney Trains", "metro": "Sydney Metro", "tram": "Sydney Light Rail", "ferry": "Sydney Ferries", "bus": ""}


def _nsw(route, mode, operator=None):
    """Transport for NSW: Sydney Trains, Sydney Metro, light rail and ferries (replacement buses left out)."""
    kind = int(route["route_type"] or 0)
    short, long = route["route_short_name"], route["route_long_name"]
    if re.search(r"(?i)non revenue|out of service|not in service|empty", f"{short} {long}") or kind in (712, 714):
        return None  # trains running empty, school and replacement buses
    if mode == "bus" or kind == 3 or 700 <= kind <= 799:
        if mode != "bus":
            return None  # buses come from the bus feed
        return _line(f"bus:{route['route_id']}", "bus", short or long, long, route, operator or "", planner_only=True)
    return _line(f"{mode}:{route['route_id']}" if mode != "train" else f"train:{short or long}", mode, short or long, long, route,
                 operator or NSW_OPERATORS[mode])


def _nsw_feed(path, mode, optional=True, fallbacks=(), operator=None):
    """A Transport for NSW feed. Only the trains are required: a metro, light rail or ferry feed
    that can't be fetched is left out (and noted) rather than stopping Sydney's build."""
    return {"url": f"{NSW_API}{path}", "fallbacks": [f"{NSW_API}{other}" for other in fallbacks], "mode": mode,
            "select": (lambda route, feed_mode: _nsw(route, feed_mode, operator)) if operator else _nsw,
            "key": "TFNSW_API_KEY", "optional": optional}


CITIES = {
    "brisbane": {
        "name": "Brisbane", "centre": (-27.4698, 153.0251), "radius_km": 110, "tz": "Australia/Brisbane",
        "planner": "https://jp.translink.com.au/plan-your-journey/journey-planner", "planner_name": "Translink journey planner",
        "credit": "Translink (Queensland Government)",
        "areas": [("Gold Coast", (-28.0167, 153.4000), 30), ("Sunshine Coast", (-26.6500, 153.0667), 35), ("Brisbane", (-27.4698, 153.0251), 35)],
        "feeds": [{"url": SEQ_URL, "mode": None, "select": _seq}],
    },
    "melbourne": {
        "name": "Melbourne", "centre": (-37.8136, 144.9631), "radius_km": 70, "tz": "Australia/Melbourne",
        "planner": "https://www.ptv.vic.gov.au/journey", "planner_name": "PTV journey planner",
        "credit": "Transport Victoria",
        "areas": [("Melbourne", (-37.8136, 144.9631), 45)],
        "feeds": [
            {"url": PTV_URL, "resolve": _ptv_url, "inner": "2/google_transit.zip", "mode": "train", "select": _ptv},
            {"url": PTV_URL, "resolve": _ptv_url, "inner": "3/google_transit.zip", "mode": "tram", "select": _ptv},
            {"url": PTV_URL, "resolve": _ptv_url, "inner": "1/google_transit.zip", "mode": "vline", "select": _ptv},
            {"url": PTV_URL, "resolve": _ptv_url, "inner": "4/google_transit.zip", "mode": "bus", "select": _ptv, "optional": True},
            {"url": PTV_URL, "resolve": _ptv_url, "inner": "11/google_transit.zip", "mode": "skybus", "select": _ptv, "optional": True},
        ],
    },
    "sydney": {
        "name": "Sydney", "centre": (-33.8688, 151.2093), "radius_km": 90, "tz": "Australia/Sydney",
        "planner": "https://transportnsw.info/trip", "planner_name": "Transport NSW trip planner",
        "credit": "Transport for NSW",
        "areas": [("Sydney", (-33.8688, 151.2093), 45)],
        "feeds": [
            _nsw_feed("/v1/gtfs/schedule/sydneytrains", "train", optional=False),
            _nsw_feed("/v2/gtfs/schedule/metro", "metro", fallbacks=["/v1/gtfs/schedule/metro"]),
            _nsw_feed("/v1/gtfs/schedule/lightrail/innerwest", "tram"),
            _nsw_feed("/v1/gtfs/schedule/lightrail/cbdandsoutheast", "tram"),
            _nsw_feed("/v1/gtfs/schedule/lightrail/parramatta", "tram"),
            _nsw_feed("/v1/gtfs/schedule/ferries/sydneyferries", "ferry"),
            _nsw_feed("/v1/gtfs/schedule/ferries/MFF", "ferry", operator="Manly Fast Ferry"),
            _nsw_feed("/v1/gtfs/schedule/buses", "bus"),
        ],
    },
}


def city_for(point):
    """The timetable city whose area takes in this point (slug), or None."""
    if point is None:
        return None
    for slug, city in CITIES.items():
        if distance_km(point, city["centre"]) <= city["radius_km"]:
            return slug
    return None


def needs_key(slug):
    """The missing setting this city's timetables need, if any."""
    keys = {feed.get("key") for feed in CITIES[slug]["feeds"]} - {None}
    return next((key for key in sorted(keys) if not getattr(settings, key, "")), None)


def network(slug):
    """The stored timetable network for a city, if it has been built."""
    return MetroNetwork.objects.filter(slug=slug, source=MetroNetwork.Source.TIMETABLE).exclude(stations=[]).first()


def is_timetable(network_obj):
    return bool(network_obj) and network_obj.source == MetroNetwork.Source.TIMETABLE


def planner_link(network_obj):
    city = CITIES.get(network_obj.slug, {})
    return city.get("planner", ""), city.get("planner_name", "")


# ---------- downloading and reading the feeds (background job only) ----------


def _scratch():
    """Where big downloads go: on disk beside the database (/tmp is memory on the server)."""
    folder = Path(settings.DATABASES["default"]["NAME"]).with_name("downloads")
    folder.mkdir(mode=0o750, exist_ok=True)
    return folder


def _download(url, key_setting=None):
    """The feed as a temporary file (deleted when closed)."""
    headers = {"User-Agent": USER_AGENT}
    if key_setting:
        headers["Authorization"] = f"apikey {getattr(settings, key_setting)}"
    request = urllib.request.Request(url, headers=headers)
    out = tempfile.TemporaryFile(dir=_scratch())
    with urllib.request.urlopen(request, timeout=120) as response:
        total = 0
        while chunk := response.read(1 << 20):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                out.close()
                raise RuntimeError(f"{url} is larger than expected")
            out.write(chunk)
    out.seek(0)
    return out


def _rows(archive, name):
    return csv.DictReader(io.TextIOWrapper(archive.open(name), "utf-8-sig"))


def _seconds(text):
    hours, minutes, secs = (int(part) for part in text.split(":"))
    return hours * 3600 + minutes * 60 + secs


def _clean_name(name, mode):
    """'Central station, platform 3' -> ('Central', ''); 'Glenferrie Rd/Wattletree Rd #45' -> (…, '45')."""
    name = re.sub(r"(?i),?\s*(platform|stop|bay|wharf|side)\s+\w+$", "", name.strip())
    number = ""
    found = re.match(r"^(.*?)\s*#(\w+)$", name)
    if found:
        name, number = found.group(1).strip(), found.group(2)
    if mode in ("train", "metro"):
        name = re.sub(r"(?i)\s+(railway\s+|metro\s+|busway\s+|bus\s+)?station$", "", name)
    return name.strip(), number


class _Builder:
    """Collects one city's stations, lines, travel times and shapes from its feeds."""

    def __init__(self, slug, writer=None):
        self.slug = slug
        self.writer = writer  # the full timetable, for the journey planner (timetable.py)
        self.lines, self.edges = {}, {}
        self.stations = {}  # slug -> station
        self.shapes = defaultdict(list)  # line key -> [path, …]

    def read(self, archive, feed, prefix):
        agencies = {}
        if "agency.txt" in archive.namelist():
            agencies = {row.get("agency_id", ""): row.get("agency_name", "") for row in _rows(archive, "agency.txt")}
        chosen, everything = {}, {}  # route id -> line key: for the network, and for the planner's timetable
        for route in _rows(archive, "routes.txt"):
            line = feed["select"](route, feed["mode"])
            if not line:
                continue
            line["key"] = f"{prefix}:{line['key']}"
            line["operator"] = line["operator"] or agencies.get(route.get("agency_id", ""), "") or (next(iter(agencies.values()), "") if len(agencies) == 1 else "")
            meta = {k: v for k, v in line.items() if k not in ("key", "planner_only")}
            everything[route["route_id"]] = line["key"]
            if self.writer:
                self.writer.add_line(line["key"], meta)
            if not line["planner_only"]:
                chosen[route["route_id"]] = line["key"]
                self.lines.setdefault(line["key"], meta)
        trips, per_pattern = {}, defaultdict(int)
        for trip in _rows(archive, "trips.txt"):
            line_key = everything.get(trip["route_id"])
            if not line_key:
                continue
            if self.writer:
                self.writer.add_trip(trip["trip_id"], line_key, trip.get("service_id", ""), trip.get("trip_headsign") or "",
                                     trip.get("wheelchair_accessible") or "")
            if trip["route_id"] not in chosen:
                continue
            pattern = (trip["route_id"], trip.get("direction_id", ""), trip.get("shape_id", ""))
            if per_pattern[pattern] < TRIPS_PER_PATTERN:
                per_pattern[pattern] += 1
                trips[trip["trip_id"]] = (line_key, trip.get("direction_id", ""), trip.get("shape_id", ""))
        times = defaultdict(list)
        for row in _rows(archive, "stop_times.txt"):
            if not (row["arrival_time"] and row["departure_time"]):
                continue
            if row["trip_id"] in trips:
                times[row["trip_id"]].append((int(row["stop_sequence"]), row["stop_id"], row["arrival_time"], row["departure_time"]))
            if self.writer and self.writer.wants(row["trip_id"]):
                self.writer.add_stop_time(row["trip_id"], int(row["stop_sequence"]), row["stop_id"], row["arrival_time"], row["departure_time"])
        used = {stop_id for stop_list in times.values() for _, stop_id, _, _ in stop_list}
        stops = {stop["stop_id"]: stop for stop in _rows(archive, "stops.txt")}
        station_of = self._group(stops, used, trips, times, prefix)
        if self.writer:
            self.writer.add_stops(stops, station_of)
            self.writer.add_calendar(archive, _rows)

        best_shape = {}  # (line, direction) -> (stop count, shape id): the fullest pattern's path
        for trip_id, stop_list in times.items():
            line_key, direction, shape_id = trips[trip_id]
            stop_list.sort()
            mode = self.lines[line_key]["mode"]
            previous = None
            for _, stop_id, arrive, depart in stop_list:
                station = self.stations[station_of[stop_id]]
                station["lines"].add(line_key)
                station["modes"].add(mode)
                if previous and previous[0] != station["slug"]:
                    minutes = max(0.5, (_seconds(arrive) - _seconds(previous[1])) / 60)
                    edge = (previous[0], station["slug"], line_key)
                    self.edges[edge] = min(self.edges.get(edge, minutes), minutes)
                previous = (station["slug"], arrive if previous else depart)  # later hops include the wait at each stop
            if shape_id and len(stop_list) > best_shape.get((line_key, direction), (0, ""))[0]:
                best_shape[(line_key, direction)] = (len(stop_list), shape_id)

        wanted = defaultdict(list)
        for (line_key, _), (_, shape_id) in best_shape.items():
            wanted[shape_id].append(line_key)
        points = defaultdict(list)
        if wanted and "shapes.txt" in archive.namelist():
            for point in _rows(archive, "shapes.txt"):
                if point["shape_id"] in wanted:
                    points[point["shape_id"]].append((int(point["shape_pt_sequence"]), float(point["shape_pt_lat"]), float(point["shape_pt_lon"])))
        for shape_id, line_keys in wanted.items():
            path = simplify([(lat, lon) for _, lat, lon in sorted(points.get(shape_id, []))], tolerance=0.0001)
            for line_key in line_keys:
                if len(path) > 1:
                    self.shapes[line_key].append(path)

    def _group(self, stops, used, trips, times, prefix):
        """Platforms become their station (shared by a city's feeds, so Melbourne's metro and V/Line
        trains meet at one Southern Cross); stops without one (trams) are joined by name with the
        stop across the road. Each station is named after its parent or lowest stop id, which
        stay put from one week's timetable to the next."""
        mode_of = {}
        for trip_id, stop_list in times.items():
            mode = self.lines[trips[trip_id][0]]["mode"]
            for _, stop_id, _, _ in stop_list:
                mode_of.setdefault(stop_id, mode)
        groups = {}  # group key -> [stop ids]
        for stop_id in sorted(used):
            stop = stops.get(stop_id)
            if not stop:
                continue
            parent = stop.get("parent_station") or ""
            if parent and parent in stops:
                key = ("parent", parent)
            else:
                name, number = _clean_name(stop["stop_name"], mode_of[stop_id])
                key = ("name", name.lower(), number)
                spot = (float(stop["stop_lat"]), float(stop["stop_lon"]))
                first = stops[groups[key][0]] if key in groups else None
                if first and distance_km(spot, (float(first["stop_lat"]), float(first["stop_lon"]))) > 0.4:
                    key = ("stop", stop_id)  # same name, somewhere else
            groups.setdefault(key, []).append(stop_id)
        station_of = {}
        for key, members in groups.items():
            base = stops[key[1]] if key[0] == "parent" else stops[members[0]]
            modes = {mode_of[stop_id] for stop_id in members}
            mode = next(m for m in MODE_ORDER if m in modes)
            name, number = _clean_name(base["stop_name"], mode)
            spots = [(float(stops[s]["stop_lat"]), float(stops[s]["stop_lon"])) for s in members]
            lat, lon = (float(base["stop_lat"]), float(base["stop_lon"])) if key[0] == "parent" and base["stop_lat"] else (
                sum(p[0] for p in spots) / len(spots), sum(p[1] for p in spots) / len(spots))
            slug = f"{self.slug[:3]}:{key[1]}" if key[0] == "parent" else f"{prefix}:{min(members)}"
            for stop_id in members:
                station_of[stop_id] = slug
            if slug in self.stations:
                continue
            self.stations[slug] = {
                "slug": slug, "name": f"{name} (stop {number})" if number else name, "local": "", "url": "",
                "lat": round(lat, 6), "lon": round(lon, 6), "lines": set(), "modes": set(), "mode": mode,
            }
        return station_of

    def walks(self):
        """Short walks between nearby stations that share no line (changing from a tram to a train)."""
        cells = defaultdict(list)
        for station in self.stations.values():
            cells[(round(station["lat"] * 100), round(station["lon"] * 100))].append(station)
        found = []
        for (row, col), here in cells.items():
            near = [s for dr in (-1, 0, 1) for dc in (-1, 0, 1) for s in cells.get((row + dr, col + dc), [])]
            for a in here:
                for b in near:
                    if a["slug"] < b["slug"] and not (a["lines"] & b["lines"]):
                        km = distance_km((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                        if km <= WALK_KM:
                            found.append([a["slug"], b["slug"], round(2 + km / WALK_SPEED_KMH * 60, 1)])
        return found

    def result(self):
        stations = []
        for station in self.stations.values():
            if station["lines"]:
                stations.append(dict(station, lines=sorted(station["lines"]), modes=sorted(station["modes"])))
        graph = [[a, b, line, round(minutes, 1)] for (a, b, line), minutes in self.edges.items()]
        graph += [[a, b, "", minutes] for a, b, minutes in self.walks()]
        overview = []
        for line_key, paths in self.shapes.items():
            longest = max(paths, key=len)
            overview.append({"line": line_key, "path": simplify(longest, tolerance=0.0004)})
        return {"stations": stations, "lines": self.lines, "graph": graph, "shapes": dict(self.shapes), "overview": overview}


def _fetch_feed(feed, downloads):
    """The feed's archive (each address downloaded once per build), trying its other addresses in turn."""
    error = None
    urls = [feed["url"], *feed.get("fallbacks", [])]
    current = feed["resolve"]() if feed.get("resolve") else None
    if current and current not in urls:
        urls.insert(0, current)
    for url in urls:
        try:
            if url not in downloads:
                downloads[url] = _download(url, feed.get("key"))
            return zipfile.ZipFile(downloads[url])
        except Exception as exc:
            error = exc
    raise error


def build(slug):
    """Download and read a city's feeds into network data (see the module notes)."""
    city = CITIES[slug]
    writer = timetable.Writer(slug)
    builder = _Builder(slug, writer)
    downloads, skipped = {}, []
    try:
        for index, feed in enumerate(city["feeds"]):
            writer.start_feed(index)
            try:
                archive = _fetch_feed(feed, downloads)
            except Exception as exc:
                if not feed.get("optional"):
                    raise
                log.warning("Timetable feed %s left out: %s", feed["url"], exc)
                skipped.append(f"{feed['url'].removeprefix(NSW_API)}: {exc}"[:200])
                continue
            inner_file = None
            if feed.get("inner"):
                inner_file = tempfile.TemporaryFile(dir=_scratch())
                with archive.open(feed["inner"]) as source:
                    shutil.copyfileobj(source, inner_file)
                inner_file.seek(0)
                archive = zipfile.ZipFile(inner_file)
            try:
                builder.read(archive, feed, f"{slug[:3]}{index}")
            finally:
                if inner_file:
                    inner_file.close()
        writer.finish()
    except BaseException:
        writer.abandon()
        raise
    finally:
        for handle in downloads.values():
            handle.close()
    return dict(builder.result(), skipped=skipped)


def refresh(slug):
    """Rebuild a city's network from this week's timetables, and its stations as Places."""
    data = build(slug)
    if not data["stations"]:
        raise RuntimeError("the timetable had no stations")
    city = CITIES[slug]
    network_obj, _ = MetroNetwork.objects.update_or_create(slug=slug, defaults={
        "name": city["name"], "source": MetroNetwork.Source.TIMETABLE, "stations": data["stations"], "lines": data["lines"],
        "graph": data["graph"], "shapes": data["shapes"], "overview": data["overview"],
        "status": {"built": timezone.now().isoformat(), "stations": len(data["stations"]), "lines": len(data["lines"]), "skipped": data["skipped"]},
    })
    _save_places(slug, data["stations"])
    return network_obj


def _area(slug, point):
    for name, centre, km in CITIES[slug]["areas"]:
        if distance_km(point, centre) <= km:
            return name
    return ""


def _save_places(slug, stations):
    """Each station as a Place (source 'timetable'), so the planner can pick it for a leg."""
    tz = CITIES[slug]["tz"]
    existing = {p.source_id: p for p in Place.objects.filter(source="timetable", source_id__startswith=f"{slug[:3]}")}
    for station in stations:
        point = (station["lat"], station["lon"])
        values = {"kind": MODE_KIND[station["mode"]], "name": station["name"][:200], "lat": station["lat"], "lon": station["lon"],
                  "tz": tz, "country": "AU", "city": _area(slug, point)}
        place = existing.get(station["slug"])
        if place is None:
            Place.objects.create(source="timetable", source_id=station["slug"], **values)
        elif any(getattr(place, key) != value for key, value in values.items()):
            for key, value in values.items():
                setattr(place, key, value)
            place.save()


def due():
    """The city whose timetables should be refreshed next, if any (missing, a week old, or not
    retried for a while after failing), skipping a city whose API key isn't set."""
    now = timezone.now()
    stored = {n.slug: n for n in MetroNetwork.objects.filter(slug__in=CITIES)}
    for slug in CITIES:
        if needs_key(slug):
            continue
        current = stored.get(slug)
        status = (current.status or {}) if current else {}
        failed = status.get("failed")
        if failed and failed > (now - timedelta(hours=RETRY_HOURS)).isoformat():
            continue
        built = status.get("built") if current and current.source == MetroNetwork.Source.TIMETABLE else None
        if not built or built < (now - timedelta(days=REFRESH_DAYS)).isoformat():
            return slug
    return None


def mark_failed(slug, error):
    current = MetroNetwork.objects.filter(slug=slug).first()
    status = dict((current.status or {}) if current else {}, failed=timezone.now().isoformat(), error=str(error)[:300])
    if current:
        MetroNetwork.objects.filter(pk=current.pk).update(status=status)
    else:
        MetroNetwork.objects.create(slug=slug, name=CITIES[slug]["name"], source=MetroNetwork.Source.TIMETABLE, status=status)


# ---------- journeys (pages and the background job read what's stored) ----------


def route(network_obj, start, end):
    """The quickest journey between two stations, like MetroMan's: duration, changes, and each
    ride's line and stations (a short walk between nearby stations counts as a ride)."""
    if start == end:
        return None
    by_slug = {s["slug"]: s for s in network_obj.stations}
    if start not in by_slug or end not in by_slug:
        return None
    rides_from = defaultdict(list)  # (station, line) -> [(minutes, next station)]
    walks_from = defaultdict(list)  # station -> [(minutes, other station)]
    for a, b, line, minutes in network_obj.graph:
        if line:
            rides_from[(a, line)].append((minutes, b))
        else:
            walks_from[a].append((minutes, b))
            walks_from[b].append((minutes, a))
    lines_at = {slug: station["lines"] for slug, station in by_slug.items()}

    # States: (station, line) riding that line, or (station, "") on foot at the station.
    best = {(start, ""): 0.0}
    came = {}
    queue = [(0.0, start, "", 0)]
    found = None
    while queue:
        cost, station, line, boardings = heapq.heappop(queue)
        if cost > best.get((station, line), float("inf")):
            continue
        if station == end:
            found = (station, line)
            break
        moves = []
        if line:
            moves += [(minutes, nxt, line, boardings) for minutes, nxt in rides_from[(station, line)]]
            moves.append((0, station, "", boardings))  # get off
        else:
            for other in lines_at.get(station, []):
                moves.append((0 if station == start and not boardings else CHANGE_MIN, station, other, boardings + 1))
            moves += [(minutes, nxt, "", boardings) for minutes, nxt in walks_from[station]]
        for extra, nxt, nxt_line, nxt_boardings in moves:
            new_cost = cost + extra
            if new_cost < best.get((nxt, nxt_line), float("inf")):
                best[(nxt, nxt_line)] = new_cost
                came[(nxt, nxt_line)] = (station, line)
                heapq.heappush(queue, (new_cost, nxt, nxt_line, nxt_boardings))
    if not found:
        return None

    states = [found]
    while states[-1] in came:
        states.append(came[states[-1]])
    states.reverse()
    rides, walk_minutes, serving = [], {}, defaultdict(set)
    for a, b, line, minutes in network_obj.graph:
        if line:
            serving[(a, b)].add(line)
        else:
            walk_minutes[(a, b)] = walk_minutes[(b, a)] = minutes
    for (station, line), (nxt, nxt_line) in zip(states, states[1:]):
        if line and nxt_line == line and nxt != station:
            if rides and rides[-1].get("key") == line and rides[-1]["stops"][-1] == station:
                rides[-1]["stops"].append(nxt)
            else:
                rides.append({"key": line, "stops": [station, nxt]})
        elif not line and not nxt_line and nxt != station:
            rides.append({"key": "", "walk": True, "stops": [station, nxt], "minutes": walk_minutes.get((station, nxt), 3)})
    shaped = []
    for index, ride in enumerate(rides):
        first, last = by_slug[ride["stops"][0]], by_slug[ride["stops"][-1]]
        if ride.get("walk"):
            names = [first["name"], last["name"]]
            if names[0] == names[1]:
                if index in (0, len(rides) - 1):
                    continue  # between two stops of one name (Circular Quay station and wharf) at either end: nothing to tell
                names = [f"{name} ({MODE_WORD.get(station['mode'], station['mode'])})" for name, station in zip(names, (first, last))]
            shaped.append({"line": "Walk", "walk": True, "colour": "", "text": "", "from": names[0], "to": names[1],
                           "stops": ride["stops"], "count": f"{max(1, round(ride['minutes']))} min"})
            continue
        meta = network_obj.lines.get(ride["key"], {})
        count = len(ride["stops"]) - 1
        pairs = list(zip(ride["stops"], ride["stops"][1:]))
        able = [key for key, lines_meta in network_obj.lines.items()
                if lines_meta.get("mode") == meta.get("mode") and all(key in serving.get(pair, ()) for pair in pairs)]
        if len(able) >= ANY_LINE:
            label = f"Any {MODE_WORD.get(meta.get('mode'), 'service')}"
        elif len(able) == 2:
            # "T4 or SCO", "Belgrave or Lilydale": numbered city lines first, and the ride takes the first one's colour.
            names = sorted({network_obj.lines[key]["name"]: key for key in able}.items(), key=lambda pair: (not re.match(r"^[TMLF]\d", pair[0]), pair[0]))
            label, meta = " or ".join(name for name, _ in names), network_obj.lines[names[0][1]]
        else:
            label = meta.get("name", "")
        shaped.append({"line": label[:60], "key": ride["key"], "colour": meta.get("colour", ""), "text": meta.get("text", ""),
                       "operator": meta.get("operator", ""), "mode": meta.get("mode", ""), "from": first["name"][:100], "to": last["name"][:100],
                       "stops": ride["stops"], "count": f"{count} stop{'s' if count != 1 else ''}"})
    changes = sum(1 for ride in shaped if not ride.get("walk")) - 1
    total = best[found]
    return {"from": start, "to": end, "duration": f"{max(1, round(total))} min", "minutes": round(total, 1), "fare": "",
            "transfers": "No changes" if changes <= 0 else f"{changes} change{'s' if changes != 1 else ''}", "rides": shaped}


def ride_path(network_obj, ride):
    """A ride as map points: along its line's real path where the timetable has one, else station to station."""
    by_slug = {s["slug"]: s for s in network_obj.stations}
    spots = [[by_slug[slug]["lat"], by_slug[slug]["lon"]] for slug in ride["stops"] if slug in by_slug]
    if ride.get("walk") or len(spots) < 2:
        return spots
    best = None
    for path in (network_obj.shapes or {}).get(ride.get("key", ""), []):
        i = min(range(len(path)), key=lambda n: distance_km(path[n], spots[0]))
        j = min(range(len(path)), key=lambda n: distance_km(path[n], spots[-1]))
        miss = distance_km(path[i], spots[0]) + distance_km(path[j], spots[-1])
        if i != j and (best is None or miss < best[0]):
            best = (miss, path[i:j + 1] if i < j else path[j:i + 1][::-1])
    if best and best[0] <= SHAPE_MATCH_KM * 2:
        return [spots[0]] + [list(p) for p in best[1]] + [spots[-1]]
    return spots


def journey_between(origin, destination):
    """(network, route) for a leg between two timetable stations of one city, else None."""
    if origin.source != "timetable" or destination.source != "timetable":
        return None
    slug = city_for(origin.coords)
    if not slug or slug != city_for(destination.coords):
        return None
    network_obj = network(slug)
    found = route(network_obj, origin.source_id, destination.source_id) if network_obj else None
    return (network_obj, found) if found else None


def leg_path(network_obj, found):
    """The whole journey as one path, for a transport leg on the map."""
    path = []
    for ride in found["rides"]:
        piece = ride_path(network_obj, ride)
        path += piece[1:] if path and piece and path[-1] == piece[0] else piece
    return path


def fill_leg(leg):
    """For a leg between two of a city's stations: the operator and (from the departure time) the
    arrival, where they're still empty. Returns the journey, or None."""
    journey = journey_between(leg.origin, leg.destination)
    if not journey:
        return None
    found = journey[1]
    if not leg.operator:
        leg.operator = " / ".join(dict.fromkeys(ride["operator"] for ride in found["rides"] if ride.get("operator")))[:100]
    if leg.depart_time and not leg.arrive_time:
        arrival = datetime.combine(leg.depart_date, leg.depart_time) + timedelta(minutes=round(found["minutes"]))
        leg.arrive_time = arrival.time().replace(second=0, microsecond=0)
        if arrival.date() != leg.depart_date:
            leg.arrive_date = arrival.date()
    return found


def summary(found):
    """'Cleveland line · 23 min · No changes' for the planner."""
    names = [ride["line"] for ride in found["rides"] if not ride.get("walk")]
    return " → ".join(dict.fromkeys(names)) + f" · {found['duration']} · {found['transfers']}"


def networks_near(points):
    """The built timetable networks whose cities take in any of these points."""
    slugs = {city_for(point) for point in points} - {None}
    return [n for n in MetroNetwork.objects.filter(slug__in=slugs, source=MetroNetwork.Source.TIMETABLE).exclude(stations=[])]


def map_lines(networks):
    """Each line's path in its official colour, for the trip map."""
    lines = []
    for network_obj in networks:
        for item in network_obj.overview or []:
            meta = network_obj.lines.get(item["line"], {})
            lines.append({"name": meta.get("long") or meta.get("name", ""), "short": meta.get("name", ""), "mode": meta.get("mode", ""),
                          "colour": meta.get("colour", ""), "city": network_obj.name, "path": item["path"]})
    return lines
