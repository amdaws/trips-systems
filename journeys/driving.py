"""Driving along the roads, for the journey planner: from OpenStreetMap, worked out here.

Each city's whole planner area (the Gold Coast, Sunshine Coast and Tweed with Brisbane, for
instance) is fetched once a month as ~28 km tiles of roads only, from OpenStreetMap's Overpass
service, a few tiles each run of the background job; the tiles are then put together into
transit/<city>-roads.bin, and a later run reads that into transit/<city>-drive.sqlite3 (one
step a run, so no run needs much memory or time). Until then the last network stays in use:

  edge(id, a, b, metres, secs, oneway, toll, kind, road)   a drivable piece between two points
  road(id, name)                                           road names and numbers, for directions
  shape(id, path)                                          a piece's shape between its ends
  blob(key, data)                                          the network packed into arrays for routing

Points are junctions, road ends and a point every ~300 m along long roads. Speeds come from each
road's limit (or its kind) less a share for junctions and traffic lights, so times are for clear
roads: there's no live traffic, and finding a park isn't included. One-way streets are followed;
turn bans aren't (rare enough for an estimate). Toll roads are marked, so a drive can avoid them.

Drives are routed on this server (A* over the packed arrays, read once per worker): a visitor's
position is never sent anywhere.
"""

import heapq
import json
import logging
import math
import os
import re
import sqlite3
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from datetime import timedelta

from django.utils import timezone

from . import timetable, walking
from .geo import distance_km, simplify
from .http import OVERPASS_SERVERS, USER_AGENT

log = logging.getLogger(__name__)

# Roads cars use, with the speed (km/h) assumed where no limit is mapped.
CLASS_SPEED = {
    "motorway": 100, "motorway_link": 60, "trunk": 80, "trunk_link": 50, "primary": 60, "primary_link": 40,
    "secondary": 60, "secondary_link": 40, "tertiary": 50, "tertiary_link": 40, "unclassified": 50,
    "residential": 40, "living_street": 10, "service": 15, "road": 40,
}
FLOW = {"motorway": 0.9, "motorway_link": 0.8, "trunk": 0.85, "trunk_link": 0.8}  # share of the limit kept on average
TOWN_FLOW = 0.7  # elsewhere: junctions, roundabouts and traffic lights
FAST = {"motorway", "motorway_link", "trunk", "trunk_link"}  # places aren't put onto these, only near them
LINKS = {"motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
PIECE_M = 300  # long roads are cut about this often, so places snap to a point near them
SNAP_M = 400  # roads looked for this close to a place first…
SNAP_FAR_M = 2000  # …then this far (an airport or park found by search is often in the middle of it)
SNAP_SPEED = 5  # metres a second for the bit between a place and the road
TOP_SPEED = 28  # metres a second: no road is faster on average (keeps A* exact enough)
GREED = 1.25  # A* leans this much towards the destination: much quicker, within a few per cent of the best
FINE = 1000  # cells per degree (~100 m), for snapping
ISLAND = 50
KIND_ROUNDABOUT, KIND_LINK = 1, 2
REFRESH_DAYS = 30
TILE = 0.25  # degrees a side (about 28 km)
OVERPASS_RETRY_SECS = 20 * 60  # Overpass is often busy for a while: try again after this, not hours later
CUT_TAGS = ("highway", "name", "ref", "maxspeed", "oneway", "junction", "toll", "toll:motorcar", "access", "motorcar",
            "motor_vehicle", "vehicle", "service", "area")


def path_for(slug):
    return timetable.folder() / f"{slug}-drive.sqlite3"


def cut_path(slug):
    return timetable.folder() / f"{slug}-roads.bin"


def region(slug):
    """(south, west, north, east): the box around the city's whole planner area."""
    from .transit import CITIES

    (lat, lon), km = CITIES[slug]["centre"], CITIES[slug]["radius_km"]
    dlat, dlon = km / 111, km / (111 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def drivable(tags):
    highway = tags.get("highway")
    if highway not in CLASS_SPEED or tags.get("area") == "yes":
        return False
    if highway == "service" and tags.get("service") in ("parking_aisle", "drive-through", "emergency_access"):
        return False
    vehicle = tags.get("motorcar") or tags.get("motor_vehicle") or tags.get("vehicle")
    if vehicle in ("no", "private"):
        return False
    return not (tags.get("access") in ("no", "private") and vehicle not in ("yes", "designated", "permissive", "destination"))


def speed(tags):
    """Metres a second along a road, on average with the traffic lights and junctions."""
    highway = tags.get("highway")
    value = (tags.get("maxspeed") or "").strip()
    limit = None
    found = re.match(r"^(\d+(?:\.\d+)?)\s*(mph)?", value)
    if found:
        limit = float(found[1]) * (1.609 if found[2] else 1)
    elif value.endswith(":urban"):
        limit = 50
    elif value.endswith(":rural"):
        limit = 100
    if not limit or not 5 <= limit <= 130:
        limit = CLASS_SPEED[highway]
    return limit * FLOW.get(highway, TOWN_FLOW) / 3.6


def oneway(tags):
    """1 one way along the road, -1 against it, 0 both ways."""
    value = tags.get("oneway", "")
    if value in ("yes", "true", "1"):
        return 1
    if value in ("-1", "reverse"):
        return -1
    if value == "no":
        return 0
    return 1 if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") in ("motorway", "motorway_link") else 0


def _name(tags):
    """'Airport Link (M7)': the name, with the route number drivers see on signs (M7, A3; not '15')."""
    name = (tags.get("name") or "").strip()
    refs = [ref.strip() for ref in (tags.get("ref") or "").split(";") if re.fullmatch(r"[A-Z]{1,2}\d{1,3}", ref.strip())]
    ref = ", ".join(refs)
    if name and ref and ref not in name:
        return f"{name} ({ref})"[:120]
    return (name or ref)[:120]


def _road_key(name):
    """Stretches with the same route number are one road, whatever each piece is called ('Centenary
    Highway (M5)' and 'Centenary Motorway (M5)')."""
    found = re.search(r"\(([^)]+)\)$", name)
    return found[1] if found else name


# ---------- building (background job, monthly) ----------


def tiles(slug):
    """[(name, (south, west, north, east))]: the tiles covering the city's planner area."""
    south, west, north, east = region(slug)
    found = []
    for row in range(math.floor(south / TILE), math.floor(north / TILE) + 1):
        for col in range(math.floor(west / TILE), math.floor(east / TILE) + 1):
            found.append((f"{row}_{col}", (row * TILE, col * TILE, (row + 1) * TILE, (col + 1) * TILE)))
    return found


def tile_path(slug, name):
    return timetable.folder() / "road-tiles" / slug / f"{name}.osm"


def fetch_tile(slug, name, box):
    """One tile's drivable roads and their points, as OpenStreetMap XML, from the first Overpass server
    that answers."""
    south, west, north, east = box
    kinds = "|".join(sorted(CLASS_SPEED))
    query = f'[out:xml][timeout:300];way["highway"~"^({kinds})$"]({south},{west},{north},{east});(._;>;);out qt;'
    path = tile_path(slug, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    last = None
    for server in OVERPASS_SERVERS:
        request = urllib.request.Request(server, data=urllib.parse.urlencode({"data": query}).encode(), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=400) as response, open(temporary, "wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            with open(temporary, "rb") as check:  # a busy server can answer 200 with an error in the body
                check.seek(max(0, temporary.stat().st_size - 200))
                if b"</osm>" not in check.read():
                    raise ValueError("incomplete answer")
            os.replace(temporary, path)
            return path.stat().st_size
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last = exc
            temporary.unlink(missing_ok=True)
            time.sleep(5)
    raise RuntimeError(f"Overpass didn't send tile {name}: {last}")


def tiles_due(slug):
    """The city's tiles still to fetch: missing, or a month old."""
    return [(name, box) for name, box in tiles(slug) if _age(tile_path(slug, name)) > REFRESH_DAYS * 86400]


def compose(slug):
    """Put a city's tiles together into its cut file, each road once. Returns how many roads."""
    import osmium

    seen, count = set(), 0
    target = cut_path(slug)
    temporary = target.with_suffix(".cutting")
    with open(temporary, "wb") as out:
        for name, _ in tiles(slug):
            path = tile_path(slug, name)
            if not path.exists() or path.stat().st_size < 100:
                continue
            for way in osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY).with_locations("flex_mem"):
                if not way.is_way() or way.id in seen or way.tags.get("highway") not in CLASS_SPEED:
                    continue
                seen.add(way.id)
                tags = json.dumps({key: way.tags[key] for key in CUT_TAGS if key in way.tags}, separators=(",", ":")).encode()
                ids, lats, lons = array("q"), array("f"), array("f")
                for node in way.nodes:
                    if node.location.valid():
                        ids.append(node.ref)
                        lats.append(node.location.lat)
                        lons.append(node.location.lon)
                out.write(struct.pack("<HI", len(tags), len(ids)) + tags + ids.tobytes() + lats.tobytes() + lons.tobytes())
                count += 1
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    return count


def read_cut(path):
    """(tags, [(id, lat, lon), …]) for each road in a cut file."""
    with open(path, "rb") as source:
        while header := source.read(6):
            size, count = struct.unpack("<HI", header)
            tags = json.loads(source.read(size))
            ids, lats, lons = array("q"), array("f"), array("f")
            ids.frombytes(source.read(8 * count))
            lats.frombytes(source.read(4 * count))
            lons.frombytes(source.read(4 * count))
            yield tags, list(zip(ids, lats, lons))


def _ways(extract, work):
    """(tags, points) for each drivable road in a cut file or an OpenStreetMap file."""
    if str(extract).endswith(".bin"):
        yield from ((tags, points) for tags, points in read_cut(extract) if len(points) > 1 and drivable(tags))
        return
    import osmium

    small = os.path.getsize(extract) < 5_000_000
    locations = "flex_mem" if small else f"sparse_file_array,{os.path.join(work, 'drive-locations.idx')}"
    for item in osmium.FileProcessor(extract, osmium.osm.NODE | osmium.osm.WAY).with_locations(locations):
        if item.is_way() and len(item.nodes) > 1 and drivable(item.tags):
            yield ({tag.k: tag.v for tag in item.tags},
                   [(node.ref, node.location.lat, node.location.lon) for node in item.nodes if node.location.valid()])


def _junctions(extract):
    import numpy
    import osmium

    refs, ends = array("q"), array("q")
    if str(extract).endswith(".bin"):
        ways = ([point[0] for point in points] for tags, points in read_cut(extract) if len(points) > 1 and drivable(tags))
    else:
        ways = ([node.ref for node in way.nodes] for way in osmium.FileProcessor(extract, osmium.osm.WAY)
                if len(way.nodes) > 1 and drivable(way.tags))
    for ids in ways:
        refs.extend(ids)
        ends.append(ids[0])
        ends.append(ids[-1])
    counted, counts = numpy.unique(numpy.frombuffer(refs, dtype=numpy.int64), return_counts=True)
    return numpy.union1d(counted[counts > 1], numpy.frombuffer(ends, dtype=numpy.int64))


class _Writer:
    """Cuts drivable roads into pieces (at junctions, and every ~300 m) and writes them."""

    def __init__(self, path, junctions):
        import numpy

        self.numpy = numpy
        self.junctions = junctions
        self.located = numpy.zeros(len(junctions), dtype=bool)
        self.cut_points = {}
        self.next_id, self.edges = len(junctions), 0
        self.node_rows, self.edge_rows = [], []
        self.roads = {"": 0}
        self.db = sqlite3.connect(path)
        os.environ.setdefault("SQLITE_TMPDIR", str(timetable.folder()))
        self.db.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;
            CREATE TABLE node_raw (id INTEGER PRIMARY KEY, lat REAL, lon REAL, fast INTEGER);
            CREATE TABLE edge (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, metres REAL, secs REAL, oneway INTEGER,
                               toll INTEGER, kind INTEGER, road INTEGER, path TEXT);
            CREATE TABLE road (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE blob (key TEXT PRIMARY KEY, data BLOB);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)

    def _id(self, osm_id, junction):
        if junction:
            return int(self.numpy.searchsorted(self.junctions, osm_id))
        found = self.cut_points.get(osm_id)
        if found is None:
            found = self.cut_points[osm_id] = self.next_id
            self.next_id += 1
        return found

    def _node(self, number, lat, lon, fast):
        if number < len(self.junctions):
            if self.located[number]:
                if not fast:
                    self.node_rows.append((number, None, None, 0))  # a town road meets it: places may snap here
                return
            self.located[number] = True
        self.node_rows.append((number, round(lat, 6), round(lon, 6), 1 if fast else 0))

    def way(self, tags, points):
        numpy = self.numpy
        if len(points) < 2:
            return
        highway = tags.get("highway")
        fast, per_second, direction = highway in FAST, speed(tags), oneway(tags)
        toll = 1 if tags.get("toll") == "yes" or tags.get("toll:motorcar") == "yes" else 0
        kind = KIND_ROUNDABOUT if tags.get("junction") in ("roundabout", "circular") else KIND_LINK if highway in LINKS else 0
        road = self.roads.setdefault(_name(tags), len(self.roads))
        ids = numpy.fromiter((p[0] for p in points), dtype=numpy.int64, count=len(points))
        at = numpy.searchsorted(self.junctions, ids)
        junction = (at < len(self.junctions)) & (self.junctions[numpy.minimum(at, len(self.junctions) - 1)] == ids)
        start, shape, length = 0, [points[0][1:]], 0.0
        for index in range(1, len(points)):
            length += distance_km(points[index - 1][1:], points[index][1:]) * 1000
            shape.append(points[index][1:])
            last = index == len(points) - 1
            if junction[index] or last or length >= PIECE_M:
                a = self._id(points[start][0], bool(junction[start]) or start == 0)
                b = self._id(points[index][0], bool(junction[index]) or last)
                self._node(a, *points[start][1:], fast)
                self._node(b, *points[index][1:], fast)
                inner = simplify(shape, tolerance=0.00003)[1:-1] if len(shape) > 2 else []
                self.edges += 1
                self.edge_rows.append((self.edges, a, b, round(length, 1), round(max(length, 1) / per_second, 1), direction, toll, kind, road,
                                       json.dumps(inner, separators=(",", ":")) if inner else ""))
                start, shape, length = index, [points[index][1:]], 0.0
        if len(self.edge_rows) > 50_000:
            self._flush()

    def _flush(self):
        # A point first written as on a fast road and later met by a town road keeps its place but becomes snappable.
        located = [row for row in self.node_rows if row[1] is not None]
        reopened = [(row[0],) for row in self.node_rows if row[1] is None]
        self.db.executemany("INSERT OR IGNORE INTO node_raw VALUES (?, ?, ?, ?)", located)
        self.db.executemany("UPDATE node_raw SET fast = 0 WHERE id = ?", reopened)
        self.db.executemany("INSERT INTO edge VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", self.edge_rows)
        self.node_rows, self.edge_rows = [], []

    def finish(self):
        numpy = self.numpy
        self._flush()
        self.db.executemany("INSERT INTO road VALUES (?, ?)", [(number, name) for name, number in self.roads.items()])
        # Drop small pieces of road joined to nothing else (a car park mapped on its own, say).
        parent = numpy.arange(self.next_id, dtype=numpy.int64)

        def root(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for a, b in self.db.execute("SELECT a, b FROM edge"):
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        roots = numpy.fromiter((root(n) for n in range(self.next_id)), dtype=numpy.int64, count=self.next_id)
        sizes = numpy.bincount(roots, minlength=self.next_id)
        keep = sizes[roots] >= min(ISLAND, int(sizes.max()) // 10)

        count = self.db.execute("SELECT count(*) FROM node_raw").fetchone()[0]  # read straight into arrays: no big lists
        nodes = numpy.fromiter(self.db.execute("SELECT id, lat, lon, fast FROM node_raw"), dtype=(numpy.float64, 4), count=count)
        nodes = nodes[keep[nodes[:, 0].astype(numpy.int64)]]
        cells = numpy.floor(nodes[:, 1] * FINE).astype(numpy.int64) * 1_000_000 + numpy.floor(nodes[:, 2] * FINE).astype(numpy.int64)
        order = numpy.argsort(cells, kind="stable")  # numbered in cell order, so a cell's points sit together
        nodes, cells = nodes[order], cells[order]
        number = numpy.full(self.next_id, -1, dtype=numpy.int64)
        number[nodes[:, 0].astype(numpy.int64)] = numpy.arange(len(nodes))

        count = self.db.execute("SELECT count(*) FROM edge").fetchone()[0]
        edges = numpy.fromiter(self.db.execute("SELECT id, a, b, secs, oneway, toll FROM edge"), dtype=(numpy.float64, 6), count=count)
        a, b = number[edges[:, 1].astype(numpy.int64)], number[edges[:, 2].astype(numpy.int64)]
        kept = (a >= 0) & (b >= 0)
        self.db.execute("CREATE TEMP TABLE gone (id INTEGER PRIMARY KEY)")
        self.db.executemany("INSERT INTO gone VALUES (?)", ((int(i),) for i in edges[~kept, 0]))
        self.db.execute("DELETE FROM edge WHERE id IN gone")
        edges, a, b = edges[kept], a[kept], b[kept]
        self.db.executemany("UPDATE edge SET a = ?, b = ? WHERE id = ?", zip(a.tolist(), b.tolist(), edges[:, 0].astype(int).tolist()))
        ident, secs, direction, toll = edges[:, 0].astype(numpy.int64), edges[:, 3], edges[:, 4].astype(numpy.int64), edges[:, 5].astype(numpy.uint8)
        forward, backward = direction >= 0, direction <= 0
        source = numpy.concatenate([a[forward], b[backward]])
        target = numpy.concatenate([b[forward], a[backward]])
        cost = numpy.concatenate([secs[forward], secs[backward]]).astype(numpy.float32)
        which = numpy.concatenate([ident[forward], -ident[backward]]).astype(numpy.int32)  # negative: the piece run backwards
        tolls = numpy.concatenate([toll[forward], toll[backward]])
        order = numpy.argsort(source, kind="stable")
        source, target, cost, which, tolls = source[order], target[order], cost[order], which[order], tolls[order]
        first = numpy.searchsorted(source, numpy.arange(len(nodes) + 1)).astype(numpy.int32)
        unique_cells, cell_first = numpy.unique(cells, return_index=True)
        blobs = {
            "lat": nodes[:, 1].astype(numpy.float32), "lon": nodes[:, 2].astype(numpy.float32),
            "snap": (nodes[:, 3] == 0).astype(numpy.uint8), "first": first, "to": target.astype(numpy.int32), "cost": cost,
            "edge": which, "toll": tolls, "cells": unique_cells.astype(numpy.int64),
            "cell_first": numpy.append(cell_first, len(nodes)).astype(numpy.int32),
        }
        self.db.executemany("INSERT INTO blob VALUES (?, ?)", [(key, value.tobytes()) for key, value in blobs.items()])
        self.db.executescript("""
            CREATE TABLE shape (id INTEGER PRIMARY KEY, path TEXT);
            INSERT INTO shape SELECT id, path FROM edge WHERE path != '';
            CREATE TABLE edge_final (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, metres REAL, secs REAL, toll INTEGER, kind INTEGER, road INTEGER);
            INSERT INTO edge_final SELECT id, a, b, metres, secs, toll, kind, road FROM edge;
            DROP TABLE edge;
            ALTER TABLE edge_final RENAME TO edge;
            DROP TABLE node_raw;
        """)
        self.db.execute("INSERT INTO meta VALUES ('built', ?)", (timezone.now().isoformat(),))
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.close()
        return len(nodes), len(edges)


def build(slug, extract=None):
    """Read the city's roads (its cut file, or the OpenStreetMap file given) and write its road network."""
    final = path_for(slug)
    temp = final.with_suffix(".building")
    temp.unlink(missing_ok=True)
    extract = extract or cut_path(slug)
    with tempfile.TemporaryDirectory(dir=timetable.folder()) as work:
        writer = _Writer(temp, _junctions(extract))
        try:
            for tags, points in _ways(extract, work):
                writer.way(tags, points)
            nodes, edges = writer.finish()
        except BaseException:
            writer.db.close()
            temp.unlink(missing_ok=True)
            raise
    os.chmod(temp, 0o640)
    os.replace(temp, final)
    _failed_marker(slug).unlink(missing_ok=True)
    log.info("Road network for %s: %s points, %s pieces", slug, nodes, edges)
    return {"nodes": nodes, "edges": edges, "bytes": final.stat().st_size}


RETRY_HOURS = 6


def _failed_marker(slug):
    return path_for(slug).with_suffix(".failed")


def mark_failed(slug):
    _failed_marker(slug).touch()


def _age(path):
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return math.inf


def _recently_failed(slug):
    return _age(_failed_marker(slug)) < timedelta(hours=RETRY_HOURS).total_seconds()


def build_due():
    """A city whose cut is newer than its road network, to build next."""
    for slug in walking.EXTRACTS:
        if cut_path(slug).exists() and _age(path_for(slug)) > _age(cut_path(slug)) and not _recently_failed(slug):
            return slug
    return None


def compose_due():
    """A city whose tiles are all fetched and newer than its cut, to put together next."""
    for slug in walking.EXTRACTS:
        if tiles_due(slug) or _recently_failed(slug):
            continue
        newest = min(_age(tile_path(slug, name)) for name, _ in tiles(slug))
        if _age(cut_path(slug)) > newest:
            return slug
    return None


def fetch_due():
    """The first city with tiles still to fetch (None while Overpass failed in the last few hours)."""
    if _age(_failed_marker("overpass")) < OVERPASS_RETRY_SECS:
        return None
    return next((slug for slug in walking.EXTRACTS if tiles_due(slug)), None)


# ---------- routing (pages: read only) ----------


class Roads:
    """A city's road network, packed into arrays (about 10–25 MB a city), opened read-only."""

    def __init__(self, slug):
        self.db = sqlite3.connect(f"file:{path_for(slug)}?mode=ro", uri=True, check_same_thread=False)
        blobs = dict(self.db.execute("SELECT key, data FROM blob"))
        typed = {"lat": "f", "lon": "f", "first": "i", "to": "i", "cost": "f", "edge": "i", "cells": "q", "cell_first": "i"}
        for key, code in typed.items():
            values = array(code)
            values.frombytes(blobs[key])
            setattr(self, key, values)
        self.snappable, self.tolls = blobs["snap"], blobs["toll"]

    def snap(self, point, count=6, limit=SNAP_M):
        """[(node, metres)]: the nearest points a car can start from or stop at, within limit metres."""
        import bisect

        lat, lon = point
        across = 111_320 * math.cos(math.radians(lat))
        row, col = math.floor(lat * FINE), math.floor(lon * FINE)
        near = []
        for ring in range(1, math.ceil(limit / 100) + 2):
            near = []
            for dr in range(-ring, ring + 1):
                key_row = (row + dr) * 1_000_000
                low = bisect.bisect_left(self.cells, key_row + col - ring)
                high = bisect.bisect_right(self.cells, key_row + col + ring)
                for at in range(low, high):
                    for node in range(self.cell_first[at], self.cell_first[at + 1]):
                        if self.snappable[node]:
                            metres = math.hypot((self.lat[node] - lat) * 110_540, (self.lon[node] - lon) * across)
                            if metres <= limit:
                                near.append((metres, node))
            near.sort()
            if near and near[0][0] + 60 <= ring * 90:
                break
        return [(node, metres) for metres, node in near[:count]]

    def route(self, origin, destination, avoid_tolls=False):
        """The quickest drive: {secs, metres, path, steps, tolls}, or None if either end is off the roads
        or they aren't joined (avoiding tolls, say)."""
        starts = self.snap(origin) or self.snap(origin, limit=SNAP_FAR_M)
        ends = self.snap(destination) or self.snap(destination, limit=SNAP_FAR_M)
        if not starts or not ends:
            return None
        lat0, lon0 = destination
        across = 111_320 * math.cos(math.radians(lat0))
        lat, lon, first, to, cost, tolls = self.lat, self.lon, self.first, self.to, self.cost, self.tolls
        hypot, push, pop = math.hypot, heapq.heappush, heapq.heappop
        scale = GREED / TOP_SPEED

        def guess(node):
            return hypot((lat[node] - lat0) * 110_540, (lon[node] - lon0) * across) * scale

        targets = {node: metres / SNAP_SPEED for node, metres in ends}
        best, came, queue = {}, {}, []
        for node, metres in starts:
            secs = metres / SNAP_SPEED
            if secs < best.get(node, math.inf):
                best[node], came[node] = secs, None
                push(queue, (secs + guess(node), secs, node))
        found, finish = math.inf, None
        while queue:
            estimate, secs, node = pop(queue)
            if estimate >= found:
                break
            if secs > best.get(node, math.inf):
                continue
            if node in targets and secs + targets[node] < found:
                found, finish = secs + targets[node], node
            for at in range(first[node], first[node + 1]):
                if avoid_tolls and tolls[at]:
                    continue
                other, total = to[at], secs + cost[at]
                if total < best.get(other, math.inf):
                    best[other], came[other] = total, (node, at)
                    push(queue, (total + guess(other), total, other))
        if finish is None:
            return None
        steps = []
        node = finish
        while came.get(node):
            node, at = came[node]
            steps.append(self.edge[at])
        steps.reverse()
        return self._describe(steps, origin, destination, found)

    def _describe(self, pieces, origin, destination, secs):
        ids = [abs(piece) for piece in pieces]
        marks = json.dumps(ids)
        rows = {row[0]: row for row in self.db.execute(
            "SELECT e.id, e.a, e.b, e.metres, e.toll, e.kind, r.name FROM edge e LEFT JOIN road r ON r.id = e.road "
            "WHERE e.id IN (SELECT value FROM json_each(?))", (marks,))}
        shapes = dict(self.db.execute("SELECT id, path FROM shape WHERE id IN (SELECT value FROM json_each(?))", (marks,)))
        path, groups, tolls = [list(origin)], [], {}
        for piece in pieces:
            _, a, b, metres, toll, kind, name = rows[abs(piece)]
            start, end = (a, b) if piece > 0 else (b, a)
            inner = json.loads(shapes[abs(piece)]) if shapes.get(abs(piece)) else []
            points = [[self.lat[start], self.lon[start]]] + (inner if piece > 0 else inner[::-1]) + [[self.lat[end], self.lon[end]]]
            points = [[round(p[0], 6), round(p[1], 6)] for p in points]
            if toll and name:
                tolls[name] = tolls.get(name, 0) + metres
            if groups and _road_key(groups[-1]["name"]) == _road_key(name or "") and groups[-1]["kind"] == kind:
                groups[-1]["metres"] += metres
                groups[-1]["points"] += points[1:]
                groups[-1]["toll"] = groups[-1]["toll"] or bool(toll)
            else:
                groups.append({"name": name or "", "kind": kind, "metres": metres, "points": points, "toll": bool(toll)})
            path += points
        path.append(list(destination))
        metres = sum(group["metres"] for group in groups)
        return {"secs": round(secs), "metres": round(metres), "path": simplify(path, tolerance=0.00002) if len(path) > 2 else path,
                "steps": _directions(groups), "tolls": _toll_roads(tolls)}


def _toll_roads(lengths):
    """The toll roads a drive uses, as drivers know them: on- and off-ramps and spellings of the same
    road left out, and any road used for only a few hundred metres unless it's the only one."""
    best = {}
    for name, metres in lengths.items():
        if re.search(r"(?i)\b(on|off|out|in)?-?\s*ramps?\b|onramp|offramp", name):
            continue
        key = re.sub(r"[^a-z0-9]", "", re.sub(r"\s*\([^)]*\)$", "", name.lower()))
        if key not in best or metres > best[key][1]:
            best[key] = (name, best.get(key, ("", 0))[1] + metres)
        else:
            best[key] = (best[key][0], best[key][1] + metres)
    kept = [name for name, metres in best.values() if metres >= 300]
    return kept or [name for name, _ in best.values()][:1] or list(lengths)[:1]


def _bearing(a, b):
    y = math.sin(math.radians(b[1] - a[1])) * math.cos(math.radians(b[0]))
    x = math.cos(math.radians(a[0])) * math.sin(math.radians(b[0])) - math.sin(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.cos(math.radians(b[1] - a[1]))
    return math.degrees(math.atan2(y, x))


def _turn(before, after):
    """'left', 'slight right'… from the end of one stretch of road to the start of the next."""
    if len(before) < 2 or len(after) < 2:
        return ""
    change = (_bearing(after[0], after[min(len(after) - 1, 3)]) - _bearing(before[max(0, len(before) - 4)], before[-1]) + 540) % 360 - 180
    if abs(change) < 25:
        return "straight"
    side = "right" if change > 0 else "left"
    if abs(change) < 60:
        return f"slight {side}"
    return "u-turn" if abs(change) > 160 else side


def _directions(groups):
    """Each stretch of road as a step: 'Turn left onto Ann Street', with its length. Unnamed ramps and
    roundabouts are folded into the road they lead to."""
    merged = []
    for group in groups:
        if merged and not group["name"] and group["kind"] == 0 and group["metres"] < 60:
            merged[-1]["metres"] += group["metres"]  # a short unnamed bit (a slip lane) belongs to the road before
            merged[-1]["points"] += group["points"][1:]
            continue
        merged.append(dict(group))
    steps, pending = [], None
    for index, group in enumerate(merged):
        if not group["name"] and group["kind"] in (KIND_ROUNDABOUT, KIND_LINK) and index + 1 < len(merged):
            pending = pending or group  # say it with the road it leads to
            continue
        name = group["name"] or "an unnamed road"
        if not steps:
            text = f"Head along {name}"
        elif pending and pending["kind"] == KIND_ROUNDABOUT:
            text = f"At the roundabout, take the exit onto {name}"
        elif pending:
            text = f"Take the ramp onto {name}"
        else:
            turn = _turn(merged[index - 1]["points"], group["points"])
            text = {"straight": f"Continue onto {name}", "u-turn": f"Make a U-turn onto {name}"}.get(turn) or (
                f"Turn {turn} onto {name}" if turn else f"Continue onto {name}")
        metres = group["metres"] + (pending["metres"] if pending else 0)
        if steps and _road_key(steps[-1]["road"]) == _road_key(name) and not pending:
            steps[-1]["metres"] += round(metres)
        else:
            steps.append({"text": text + (" (toll)" if group["toll"] else ""), "road": name, "metres": round(metres)})
        pending = None
    # A few metres of another road (a corner clipped), or a short unnamed link between two roads,
    # isn't worth a step of its own: it's counted in the step before.
    tidy = []
    for index, step in enumerate(steps):
        small = step["metres"] < 40 or (step["road"] == "an unnamed road" and step["metres"] < 250)
        if tidy and small and index + 1 < len(steps):
            tidy[-1]["metres"] += step["metres"]
            continue
        if tidy and _road_key(tidy[-1]["road"]) == _road_key(step["road"]):
            tidy[-1]["metres"] += step["metres"]
            if step["text"].endswith(" (toll)") and not tidy[-1]["text"].endswith(" (toll)"):
                tidy[-1]["text"] += " (toll)"
            continue
        tidy.append(step)
    return tidy


_cache = {}


def roads(slug):
    """The city's road network (reopened after the monthly rebuild), or None if it isn't built yet."""
    try:
        modified = path_for(slug).stat().st_mtime
    except FileNotFoundError:
        return None
    cached = _cache.get(slug)
    if cached and cached[0] == modified:
        return cached[1]
    found = Roads(slug)
    _cache[slug] = (modified, found)
    return found
