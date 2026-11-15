"""Every departure of a city's trains, metro, trams and ferries, for the public journey planner.

Built each week with the city's network (transit.refresh, from the same download) into its own
SQLite file beside the database, transit/<city>.sqlite3. The new file is written beside the old
one and swapped in at the end, so pages keep reading the old one until the new one is complete.

Tables (times are seconds after the service day's midnight, so they can pass 24:00):
  stop(id, feed, gtfs_id, station, name, platform, lat, lon)   station: the network station's slug
  line(key, name, long, colour, text, mode, operator)           every line, buses included
  trip(id, feed, gtfs_id, line, service, headsign)              line: the line's key
  service_day(service, day)                                     each date (YYYYMMDD) a service runs
  hop(service, dep, arr, trip, seq, a, b)                       one stop to the next on a trip
  footpath(a, b, secs)                                          walks between nearby stops

Pages only read these files; nothing about a visitor is ever written to them.
"""

import heapq
import json
import math
import os
import re
from array import array
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from django.conf import settings

from .geo import distance_km

FOOTPATH_M = 400  # stops this close are linked by a walk
WALK_SPEED = 1.3  # metres a second (about 4.7 km/h)
DETOUR = 1.25  # streets are longer than a straight line
CHANGE_SECS = 60  # least time to change trains on one platform
SERVICE_DAYS = 45  # how far ahead service dates are kept
MODES = ("train", "metro", "tram", "ferry", "bus")  # numbered from 1 in City.trip_rules
BATCH = 50_000

# access, for stops and trips, is GTFS's wheelchair field: 1 step-free, 2 not, 0 not said.
SCHEMA = """
CREATE TABLE stop (id INTEGER PRIMARY KEY, feed INTEGER, gtfs_id TEXT, station TEXT, name TEXT, platform TEXT, lat REAL, lon REAL, access INTEGER);
CREATE TABLE line (key TEXT PRIMARY KEY, name TEXT, long TEXT, colour TEXT, text TEXT, mode TEXT, operator TEXT);
CREATE TABLE trip (id INTEGER PRIMARY KEY, feed INTEGER, gtfs_id TEXT, line TEXT, service INTEGER, headsign TEXT, access INTEGER);
CREATE TABLE service_day (service INTEGER, day TEXT);
CREATE TABLE stop_time (trip INTEGER, seq INTEGER, stop INTEGER, arr INTEGER, dep INTEGER);
CREATE TABLE footpath (a INTEGER, b INTEGER, secs INTEGER);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def folder():
    path = Path(settings.TRANSIT_DIR)
    path.mkdir(mode=0o750, exist_ok=True)
    return path


def path_for(slug):
    return folder() / f"{slug}.sqlite3"


def walk_secs(metres):
    return round(metres * DETOUR / WALK_SPEED)


def _seconds(text):
    hours, minutes, secs = (int(part) for part in text.split(":"))
    return hours * 3600 + minutes * 60 + secs


class Writer:
    """Collects one city's timetable while transit's builder reads the feeds."""

    def __init__(self, slug):
        self.slug = slug
        self.final = path_for(slug)
        self.temp = self.final.with_suffix(".building")
        self.temp.unlink(missing_ok=True)
        os.environ.setdefault("SQLITE_TMPDIR", str(folder()))  # big sorts spill to disk, not to /tmp (memory here)
        self.db = sqlite3.connect(self.temp)
        self.db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;" + SCHEMA)
        self.feed = 0
        self.trips, self.services, self.stops = {}, {}, {}
        self.buffer = []

    def start_feed(self, index):
        self.feed = index

    def add_line(self, key, meta):
        self.db.execute("INSERT OR IGNORE INTO line VALUES (?, ?, ?, ?, ?, ?, ?)", (
            key, meta.get("name", "")[:60], meta.get("long", "")[:120], meta.get("colour", ""), meta.get("text", ""),
            meta.get("mode", ""), (meta.get("operator") or "")[:80]))

    def add_trip(self, trip_id, line, service_id, headsign, access=""):
        number = len(self.trips) + 1
        self.trips[(self.feed, trip_id)] = number
        service = self.services.setdefault((self.feed, service_id), len(self.services) + 1)
        self.db.execute("INSERT INTO trip VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (number, self.feed, trip_id, line, service, headsign[:80], int(access) if access in ("1", "2") else 0))

    def wants(self, trip_id):
        return (self.feed, trip_id) in self.trips

    def add_stop_time(self, trip_id, seq, stop_id, arrive, depart):
        stop = self.stops.setdefault((self.feed, stop_id), len(self.stops) + 1)
        self.buffer.append((self.trips[(self.feed, trip_id)], seq, stop, _seconds(arrive), _seconds(depart)))
        if len(self.buffer) >= BATCH:
            self._flush()

    def _flush(self):
        self.db.executemany("INSERT INTO stop_time VALUES (?, ?, ?, ?, ?)", self.buffer)
        self.buffer = []

    def add_calendar(self, archive, rows):
        """Expand calendar.txt and calendar_dates.txt into the dates each service runs."""
        names = archive.namelist()
        first, last = date.today() - timedelta(days=1), date.today() + timedelta(days=SERVICE_DAYS)
        days = defaultdict(set)
        if "calendar.txt" in names:
            weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            for row in rows(archive, "calendar.txt"):
                service = self.services.get((self.feed, row["service_id"]))
                if not service:
                    continue
                start = max(first, datetime.strptime(row["start_date"], "%Y%m%d").date())
                end = min(last, datetime.strptime(row["end_date"], "%Y%m%d").date())
                day = start
                while day <= end:
                    if row[weekdays[day.weekday()]] == "1":
                        days[service].add(day.strftime("%Y%m%d"))
                    day += timedelta(days=1)
        if "calendar_dates.txt" in names:
            for row in rows(archive, "calendar_dates.txt"):
                service = self.services.get((self.feed, row["service_id"]))
                if not service:
                    continue
                if not first.strftime("%Y%m%d") <= row["date"] <= last.strftime("%Y%m%d"):
                    continue
                if row["exception_type"] == "1":
                    days[service].add(row["date"])
                else:
                    days[service].discard(row["date"])
        self.db.executemany("INSERT INTO service_day VALUES (?, ?)", [(service, day) for service, found in days.items() for day in found])

    def add_stops(self, stops, station_of):
        """The stops this feed's trips use, with their station (where the network has one) and platform."""
        rows = []
        for (feed, stop_id), number in self.stops.items():
            if feed != self.feed:
                continue
            stop = stops.get(stop_id)
            if not stop:
                continue
            parent = stops.get(stop.get("parent_station") or "")
            name = (parent or stop)["stop_name"]
            platform = stop.get("platform_code") or ""
            if not platform:  # Sydney's are in the name: "Town Hall Station Platform 4"
                found = re.search(r"(?i)\bplatform\s+(\w+)", stop["stop_name"])
                platform = found.group(1) if found else ""
            access = stop.get("wheelchair_boarding") or ""
            if access not in ("1", "2"):  # a platform that doesn't say takes its station's word
                access = (parent or {}).get("wheelchair_boarding") or ""
            rows.append((number, feed, stop_id, station_of.get(stop_id, ""), name[:120], platform[:10],
                         float(stop["stop_lat"]), float(stop["stop_lon"]), int(access) if access in ("1", "2") else 0))
        self.db.executemany("INSERT INTO stop VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def finish(self):
        """Hops, footpaths and indexes, then swap the new file in."""
        self._flush()
        db = self.db
        db.executescript("""
            -- Kept in (service, departure) order, so a search's time range is one contiguous read.
            CREATE TABLE hop (service INTEGER, dep INTEGER, arr INTEGER, trip INTEGER, seq INTEGER, a INTEGER, b INTEGER,
                              PRIMARY KEY (service, dep, trip, seq)) WITHOUT ROWID;
            INSERT INTO hop
              SELECT t.service, dep, next_arr, s.trip, seq, stop, next_stop FROM (
                SELECT trip, seq, stop, dep, LEAD(stop) OVER w AS next_stop, LEAD(arr) OVER w AS next_arr
                FROM stop_time WINDOW w AS (PARTITION BY trip ORDER BY seq)) s
              JOIN trip t ON t.id = s.trip
              WHERE next_stop IS NOT NULL
              ORDER BY t.service, dep, s.trip, seq;
            DROP TABLE stop_time;
            CREATE INDEX hop_stop ON hop (a, service, dep);
            CREATE INDEX service_days ON service_day (day);
            CREATE INDEX trip_names ON trip (gtfs_id);
        """)
        paths = self._footpaths(db.execute("SELECT id, station, lat, lon FROM stop").fetchall())
        db.executemany("INSERT INTO footpath VALUES (?, ?, ?)", paths)
        db.execute("CREATE INDEX footpaths ON footpath (a)")
        db.execute("INSERT INTO meta VALUES ('built', ?)", (datetime.now().isoformat(),))
        db.commit()
        db.execute("ANALYZE")
        db.close()
        os.chmod(self.temp, 0o640)
        os.replace(self.temp, self.final)
        return self.final.stat().st_size

    def _footpaths(self, stops):
        """Walks between stops within FOOTPATH_M of each other, and between a station's platforms:
        along the streets where the city's walking network covers them (so two stops either side
        of a river aren't linked), else the straight-line estimate."""
        from . import walking  # walking.py imports this module

        cells = defaultdict(list)
        for stop in stops:
            cells[(math.floor(stop[2] * 200), math.floor(stop[3] * 200))].append(stop)  # about 500 m cells
        pairs = defaultdict(list)  # stop -> [(other stop, metres in a straight line, same station)]
        for (row, col), here in cells.items():
            near = [s for dr in (-1, 0, 1) for dc in (-1, 0, 1) for s in cells.get((row + dr, col + dc), [])]
            for a in here:
                for b in near:
                    if a[0] == b[0]:
                        continue
                    metres = distance_km(a[2:], b[2:]) * 1000
                    same_station = bool(a[1] and a[1] == b[1])
                    if metres <= FOOTPATH_M or same_station:
                        pairs[a].append((b, metres, same_station))
        walker = walking.walker(self.slug)
        tiles = defaultdict(list)  # about 1 km squares, each routed with the streets around it
        for stop in pairs:
            tiles[(math.floor(stop[2] * 100), math.floor(stop[3] * 100))].append(stop)
        paths = []
        for (row, col), here in tiles.items():
            # A tile's corners are ~700 m from its centre, a walk up to 1.6 × FOOTPATH_M, plus the snap to the street.
            area = walker.area([((row + 0.5) / 100, (col + 0.5) / 100)], 700 + FOOTPATH_M * 1.6 + walking.SNAP_M) if walker else None
            for a in here:
                spread = area.spread(a[2:], FOOTPATH_M * 1.6) if area else None
                for b, metres, same_station in pairs[a]:
                    if spread:
                        reached = area.reach(spread, b[2:])
                        if reached is None and not same_station:
                            continue  # too far round by the streets
                        secs = walking.secs(reached[0]) + 30 if reached else 0
                    else:
                        secs = walk_secs(metres) + 30
                    paths.append((a[0], b[0], max(120 if same_station else 60, secs)))
        return paths

    def abandon(self):
        try:
            self.db.close()
        finally:
            self.temp.unlink(missing_ok=True)


# ---------- reading (the journey planner) ----------


_cache = {}  # slug -> (file modified time, City)


class City:
    """A city's timetable, opened read-only, with its stops, lines and the walks between stops in memory."""

    def __init__(self, slug):
        self.slug = slug
        self.db = sqlite3.connect(f"file:{path_for(slug)}?mode=ro", uri=True, check_same_thread=False)
        try:
            self.stops = {row[0]: row for row in self.db.execute("SELECT id, feed, gtfs_id, station, name, platform, lat, lon, access FROM stop")}
            self.has_access = True
        except sqlite3.OperationalError:  # built before step-free was kept: until the weekly rebuild
            self.stops = {row[0]: row + (0,) for row in self.db.execute("SELECT id, feed, gtfs_id, station, name, platform, lat, lon FROM stop")}
            self.has_access = False
        said = sum(1 for row in self.stops.values() if row[8])
        self.access_share = said / len(self.stops) if self.stops else 0  # how many stops say whether they're step-free
        self.trip_mode = self.trip_access = None  # read when a search first needs them
        self.lines = {row[0]: {"name": row[1], "long": row[2], "colour": row[3], "text": row[4], "mode": row[5], "operator": row[6]}
                      for row in self.db.execute("SELECT key, name, long, colour, text, mode, operator FROM line")}
        self.stop_ids = {(row[1], row[2]): row[0] for row in self.stops.values()}
        self.covered = None  # (first, last) day the published timetable covers, read when first needed
        # Walks between stops, compactly: the walks from stop s are at first[s]..first[s + 1] of to and secs.
        top = max(self.stops, default=0) + 2
        self.first, self.to, self.secs = array("i", [0]) * top, array("i"), array("i")
        current = 0
        for a, b, secs in self.db.execute("SELECT a, b, secs FROM footpath ORDER BY a"):
            while current < a:
                current += 1
                self.first[current] = len(self.to)
            self.to.append(b)
            self.secs.append(secs)
        for later in range(current + 1, top):
            self.first[later] = len(self.to)

    def within(self, a, b, margin_m):
        """The stops in the box around two points, widened by a margin: the corridor a journey uses."""
        lat_pad = margin_m / 111_000
        lon_pad = margin_m / (111_000 * max(0.2, math.cos(math.radians((a[0] + b[0]) / 2))))
        south, north = min(a[0], b[0]) - lat_pad, max(a[0], b[0]) + lat_pad
        west, east = min(a[1], b[1]) - lon_pad, max(a[1], b[1]) + lon_pad
        return {stop_id for stop_id, row in self.stops.items() if south <= row[6] <= north and west <= row[7] <= east}

    def walks(self, stop):
        """[(other stop, seconds)] for the short walks from a stop."""
        if stop + 1 >= len(self.first):
            return []
        start, end = self.first[stop], self.first[stop + 1]
        return list(zip(self.to[start:end], self.secs[start:end]))

    def near(self, point, metres):
        """[(stop id, walking seconds)] for the stops within this distance of a point."""
        lat, lon = point
        dlat, dlon = metres / 111_000, metres / (111_000 * max(0.2, math.cos(math.radians(lat))))
        found = []
        for stop_id, row in self.stops.items():
            if abs(row[6] - lat) <= dlat and abs(row[7] - lon) <= dlon:
                distance = distance_km(point, (row[6], row[7])) * 1000
                if distance <= metres:
                    found.append((stop_id, walk_secs(distance), round(distance)))
        return found

    def services_on(self, day):
        return [row[0] for row in self.db.execute("SELECT service FROM service_day WHERE day = ?", (day.strftime("%Y%m%d"),))]

    def trip_rules(self):
        """(mode of each trip, step-free of each trip), as bytes indexed by trip id: read once."""
        if self.trip_mode is None:
            codes = {mode: code for code, mode in enumerate(MODES, start=1)}
            line_mode = {key: codes.get(meta["mode"], 0) for key, meta in self.lines.items()}
            top = self.db.execute("SELECT max(id) FROM trip").fetchone()[0] or 0
            modes, access = bytearray(top + 1), bytearray(top + 1)
            query = "SELECT id, line, access FROM trip" if self.has_access else "SELECT id, line, 0 FROM trip"
            for trip, line, said in self.db.execute(query):
                modes[trip] = line_mode.get(line, 0)
                access[trip] = said or 0
            self.trip_mode, self.trip_access = bytes(modes), bytes(access)
        return self.trip_mode, self.trip_access

    def timetable_day(self, day):
        """The day whose timetable to use for this one: itself while the published timetable reaches
        it, else the same weekday in the timetable's last week (agencies publish a few weeks ahead).
        None for a day before the timetable starts."""
        if self.covered is None:
            days = sorted(date.fromisoformat(f"{d[:4]}-{d[4:6]}-{d[6:]}") for (d,) in self.db.execute("SELECT DISTINCT day FROM service_day"))
            # The run of days around today: a lone special-event date months away isn't the timetable's end.
            today, run = date.today(), []
            for found in days:
                if run and (found - run[-1]).days > 3:
                    if run[-1] >= today - timedelta(days=1):
                        break
                    run = []
                run.append(found)
            self.covered = (run[0], run[-1]) if run else (today, today - timedelta(days=1))
        first, last = self.covered
        if day < first:
            return None
        if day <= last:
            return day
        return day - timedelta(weeks=math.ceil((day - last).days / 7))

    def trip_numbers(self, keys):
        """{trip id: (feed, gtfs id)} for the (feed, gtfs id) pairs given (live updates name trips by gtfs id)."""
        wanted = defaultdict(set)
        for feed, gtfs_id in keys:
            wanted[gtfs_id].add(feed)
        found = {}
        names = list(wanted)
        for start in range(0, len(names), 900):
            chunk = names[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for number, feed, gtfs_id in self.db.execute(f"SELECT id, feed, gtfs_id FROM trip WHERE gtfs_id IN ({marks})", chunk):
                if feed in wanted[gtfs_id]:
                    found[number] = (feed, gtfs_id)
        return found

    def stream(self, day, start, end, stops):
        """Hops departing between start and end that leave one of these stops, in departure order,
        read from the database as they're needed (sorted there, filtered there)."""
        allowed = json.dumps(sorted(stops))
        streams = []
        for offset in (-1, 0, 1):
            services = self.services_on(day + timedelta(days=offset))
            low, high = start - offset * 86400, end - offset * 86400
            if not services or high < 0 or low > 48 * 3600:
                continue
            # "+a" keeps SQLite reading by service and time: left to itself it looks up every corridor
            # stop in turn, ten times slower.
            rows = self.db.execute(
                "SELECT dep, arr, trip, seq, a, b FROM hop WHERE service IN (SELECT value FROM json_each(?)) "
                "AND dep BETWEEN ? AND ? AND +a IN (SELECT value FROM json_each(?)) ORDER BY dep",
                (json.dumps(services), low, high, allowed),
            )
            shift = offset * 86400
            streams.append(rows if not shift else ((d + shift, r + shift, t, q, a, b) for d, r, t, q, a, b in rows))
        return heapq.merge(*streams) if len(streams) > 1 else iter(streams[0] if streams else ())

    def trips(self, ids):
        """trip id -> (feed, gtfs id, line, headsign)."""
        found = {}
        ids = list(ids)
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(f"SELECT id, feed, gtfs_id, line, headsign FROM trip WHERE id IN ({marks})", chunk):
                found[row[0]] = row[1:]
        return found

    def hops(self, day, start, end, stop=None, stops=None):
        """Hops departing between start and end (seconds after the day's midnight), on the services
        running that day, the day before (trips past midnight) and the day after, as
        (dep, arr, trip, seq, a, b) in the day's own seconds. With stops (a set of stop ids), only
        hops leaving those stops: the database filters them, so a city's buses don't all pass through Python."""
        allowed = json.dumps(sorted(stops)) if stops is not None else None
        found = []
        for offset in (-1, 0, 1):
            services = self.services_on(day + timedelta(days=offset))
            low, high = start - offset * 86400, end - offset * 86400
            if not services or high < 0 or low > 48 * 3600:
                continue
            for begin in range(0, len(services), 900):
                chunk = services[begin:begin + 900]
                marks = ",".join("?" * len(chunk))
                where = "a = ? AND " if stop is not None else ""
                within = " AND a IN (SELECT value FROM json_each(?))" if allowed is not None else ""
                params = ([stop] if stop is not None else []) + [*chunk, low, high] + ([allowed] if allowed is not None else [])
                for dep, arr, trip, seq, a, b in self.db.execute(
                    f"SELECT dep, arr, trip, seq, a, b FROM hop WHERE {where}service IN ({marks}) AND dep BETWEEN ? AND ?{within}", params,
                ):
                    found.append((dep + offset * 86400, arr + offset * 86400, trip, seq, a, b))
        return found


def open_city(slug):
    """The city's timetable, reopened when the weekly rebuild replaces the file; None if not built."""
    path = path_for(slug)
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return None
    cached = _cache.get(slug)
    if cached and cached[0] == modified:
        return cached[1]
    city = City(slug)
    _cache[slug] = (modified, city)
    return city
