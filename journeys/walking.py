"""Walking along streets and paths, for the journey planner: from OpenStreetMap, worked out here.

Once a month each city's OpenStreetMap extract (from BBBike, updated weekly; ODbL) is read into
a compact walking network in its own SQLite file beside the timetable, transit/<city>-walk.sqlite3:

  node(cell, id, lat, lon)               a junction, a path's end, or a point every ~80 m along a long way
  edge(cell, id, a, b, metres)           a walkable piece between two nodes
  shape(id, path)                        a piece's shape between its ends, where it isn't straight

Walkable means footpaths, paths, steps, pedestrian streets, platforms and ordinary streets, but
not motorways or anything marked private or foot=no. "cell" is a ~500 m square, so a search
loads only the streets around the places it's working with; both tables are stored in cell
order, so that's a few contiguous reads.

Walks are routed on this server: a visitor's position is never sent anywhere. Beyond a city's
extract (the Gold Coast, say) the planner falls back to a straight-line estimate.
"""

import heapq
import json
import logging
import math
import os
import sqlite3
import tempfile
import urllib.request
from array import array
from collections import defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

from . import timetable
from .geo import distance_km, simplify
from .http import USER_AGENT

log = logging.getLogger(__name__)

EXTRACTS = {
    "brisbane": "https://download.bbbike.org/osm/bbbike/Brisbane/Brisbane.osm.pbf",
    "melbourne": "https://download.bbbike.org/osm/bbbike/Melbourne/Melbourne.osm.pbf",
    "sydney": "https://download.bbbike.org/osm/bbbike/Sydney/Sydney.osm.pbf",
}
REFRESH_DAYS = 30
SPEED = 1.3  # metres a second along the way itself (about 4.7 km/h)
PIECE_M = 80  # long ways are cut about this often, so places snap to a point near them
SNAP_M = 250  # a place further than this from any walkable way can't use the network
SNAP_SPREAD_M = 40  # walks start from every point of the network this much further than the nearest
ISLAND = 100  # pieces of network with fewer points than this (or a tenth of the largest), joined to nothing else, are dropped
CELL = 200  # cells per degree (~500 m)
FINE = 1000  # finer cells (~100 m) for snapping a place to its nearest point of the network

WALKABLE = {
    "footway", "path", "pedestrian", "steps", "living_street", "residential", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link", "trunk", "trunk_link", "service", "track", "cycleway",
    "corridor", "road", "bridleway", "platform", "crossing",
}
NO_ACCESS = {"private", "no"}
FOOT_OK = {"yes", "designated", "permissive", "destination"}


def path_for(slug):
    return timetable.folder() / f"{slug}-walk.sqlite3"


def cell_of(lat, lon):
    return (math.floor(lat * CELL) + 40000) * 100000 + math.floor(lon * CELL) + 50000


def _walkable(tags):
    highway = tags.get("highway")
    if highway not in WALKABLE and tags.get("railway") != "platform" and tags.get("public_transport") != "platform":
        return False
    foot = tags.get("foot")
    if foot == "no" or (tags.get("access") in NO_ACCESS and foot not in FOOT_OK):
        return False
    return tags.get("area") != "yes" or highway == "pedestrian"


# ---------- building (background job, monthly) ----------


def _download(url, folder):
    path = os.path.join(folder, "extract.osm.pbf")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response, open(path, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    return path


def download(slug, folder):
    """The city's OpenStreetMap extract, saved in folder (the road network is read from the same one)."""
    return _download(EXTRACTS[slug], folder)


def _junctions(extract):
    """The OpenStreetMap ids (sorted) of the nodes where walkable ways meet or end."""
    import numpy
    import osmium

    refs, ends = array("q"), array("q")
    for way in osmium.FileProcessor(extract, osmium.osm.WAY):
        if len(way.nodes) > 1 and _walkable(way.tags):
            ids = [node.ref for node in way.nodes]
            refs.extend(ids)
            ends.append(ids[0])
            ends.append(ids[-1])
    counted, counts = numpy.unique(numpy.frombuffer(refs, dtype=numpy.int64), return_counts=True)
    return numpy.union1d(counted[counts > 1], numpy.frombuffer(ends, dtype=numpy.int64))


class _Writer:
    """Cuts walkable ways into pieces (at junctions, and every ~80 m along long ways) and writes them."""

    def __init__(self, path, junctions):
        import numpy

        self.numpy = numpy
        self.junctions = junctions
        self.located = numpy.zeros(len(junctions), dtype=bool)
        self.cut_points = {}  # osm id -> our id, for points cut along a way (junctions are numbered by position)
        self.next_id, self.edges = len(junctions), 0
        self.node_rows, self.edge_rows = [], []
        self.db = sqlite3.connect(path)
        os.environ.setdefault("SQLITE_TMPDIR", str(timetable.folder()))  # sorting spills to disk, not /tmp (memory here)
        self.db.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;
            CREATE TABLE node_raw (id INTEGER PRIMARY KEY, lat REAL, lon REAL, cell INTEGER);
            CREATE TABLE edge_raw (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, metres REAL, path TEXT, cell INTEGER);
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

    def _node(self, number, lat, lon):
        if number < len(self.junctions):
            if self.located[number]:
                return
            self.located[number] = True
        self.node_rows.append((number, round(lat, 6), round(lon, 6), cell_of(lat, lon)))

    def way(self, way):
        numpy = self.numpy
        points = [(node.ref, node.location.lat, node.location.lon) for node in way.nodes if node.location.valid()]
        if len(points) < 2:
            return
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
                self._node(a, *points[start][1:])
                self._node(b, *points[index][1:])
                inner = simplify(shape, tolerance=0.00004)[1:-1] if len(shape) > 2 else []
                self.edges += 1
                self.edge_rows.append((self.edges, a, b, round(length, 1), json.dumps(inner, separators=(",", ":")) if inner else "",
                                       cell_of(*shape[len(shape) // 2])))
                start, shape, length = index, [points[index][1:]], 0.0
        if len(self.edge_rows) > 50_000:
            self._flush()

    def _flush(self):
        self.db.executemany("INSERT OR IGNORE INTO node_raw VALUES (?, ?, ?, ?)", self.node_rows)
        self.db.executemany("INSERT INTO edge_raw VALUES (?, ?, ?, ?, ?, ?)", self.edge_rows)
        self.node_rows, self.edge_rows = [], []

    def _drop_islands(self):
        """Remove small pieces of network joined to nothing else (station platforms mapped without
        their stairs, say): a place snapped onto one could walk nowhere."""
        numpy = self.numpy
        parent = numpy.arange(self.next_id, dtype=numpy.int64)

        def root(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for a, b in self.db.execute("SELECT a, b FROM edge_raw"):
            ra, rb = root(a), root(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        roots = numpy.fromiter((root(n) for n in range(self.next_id)), dtype=numpy.int64, count=self.next_id)
        sizes = numpy.bincount(roots, minlength=self.next_id)
        limit = min(ISLAND, int(sizes.max()) // 10)  # a city's main network has hundreds of thousands of points
        small = numpy.nonzero(sizes[roots] < limit)[0]
        self.db.execute("CREATE TEMP TABLE island (id INTEGER PRIMARY KEY)")
        self.db.executemany("INSERT INTO island VALUES (?)", ((int(n),) for n in small))
        self.db.execute("DELETE FROM edge_raw WHERE a IN island")
        self.db.execute("DELETE FROM node_raw WHERE id IN island")
        self.edges = self.db.execute("SELECT count(*) FROM edge_raw").fetchone()[0]

    def finish(self):
        self._flush()
        self._drop_islands()
        self.db.executescript("""
            CREATE TABLE node (cell INTEGER, id INTEGER, lat REAL, lon REAL, PRIMARY KEY (cell, id)) WITHOUT ROWID;
            INSERT INTO node SELECT cell, id, lat, lon FROM node_raw ORDER BY cell, id;
            CREATE TABLE edge (cell INTEGER, id INTEGER, a INTEGER, b INTEGER, metres REAL, PRIMARY KEY (cell, id)) WITHOUT ROWID;
            INSERT INTO edge SELECT cell, id, a, b, metres FROM edge_raw ORDER BY cell, id;
            CREATE TABLE shape (id INTEGER PRIMARY KEY, path TEXT);
            INSERT INTO shape SELECT id, path FROM edge_raw WHERE path != '';
            DROP TABLE node_raw;
            DROP TABLE edge_raw;
        """)
        self.db.execute("INSERT INTO meta VALUES ('built', ?)", (timezone.now().isoformat(),))
        self.db.commit()
        self.db.execute("VACUUM")
        nodes = self.db.execute("SELECT count(*) FROM node").fetchone()[0]
        self.db.close()
        return nodes, self.edges


def build(slug, extract=None):
    """Download the city's extract (or read the one given) and write its walking network."""
    import osmium

    final = path_for(slug)
    temp = final.with_suffix(".building")
    with tempfile.TemporaryDirectory(dir=timetable.folder()) as work:
        extract = extract or download(slug, work)
        writer = _Writer(temp, _junctions(extract))
        try:
            # A city's node positions go in a file on disk (memory is short here); a small map's stay in memory.
            small = os.path.getsize(extract) < 5_000_000
            locations = "flex_mem" if small else f"sparse_file_array,{os.path.join(work, 'locations.idx')}"
            for item in osmium.FileProcessor(extract, osmium.osm.NODE | osmium.osm.WAY).with_locations(locations):
                if item.is_way() and len(item.nodes) > 1 and _walkable(item.tags):
                    writer.way(item)
            nodes, edges = writer.finish()
        except BaseException:
            writer.db.close()
            temp.unlink(missing_ok=True)
            raise
    os.chmod(temp, 0o640)
    os.replace(temp, final)
    _failed_marker(slug).unlink(missing_ok=True)
    log.info("Walking network for %s: %s nodes, %s edges", slug, nodes, edges)
    return {"nodes": nodes, "edges": edges, "bytes": final.stat().st_size}


RETRY_HOURS = 6


def _failed_marker(slug):
    return path_for(slug).with_suffix(".failed")


def mark_failed(slug):
    _failed_marker(slug).touch()


def due():
    """The city whose walking network should be built next: one the planner has timetables for,
    whose network is missing or a month old, and not tried and failed in the last few hours."""
    from .models import MetroNetwork

    now = datetime.now().timestamp()
    served = set(MetroNetwork.objects.filter(slug__in=EXTRACTS, source=MetroNetwork.Source.TIMETABLE).exclude(stations=[])
                 .values_list("slug", flat=True))
    for slug in EXTRACTS:
        if slug not in served:
            continue
        try:
            if now - _failed_marker(slug).stat().st_mtime < timedelta(hours=RETRY_HOURS).total_seconds():
                continue
        except FileNotFoundError:
            pass
        try:
            age = now - path_for(slug).stat().st_mtime
        except FileNotFoundError:
            return slug
        if age > timedelta(days=REFRESH_DAYS).total_seconds():
            return slug
    return None


# ---------- routing (pages: read only) ----------


class Area:
    """The walking network around some places, loaded for one search."""

    def __init__(self, db, points, reach_m):
        self.db = db
        cells = set()
        for lat, lon in points:
            pad_lat = reach_m / 111_000
            pad_lon = reach_m / (111_000 * max(0.2, math.cos(math.radians(lat))))
            for row in range(math.floor((lat - pad_lat) * CELL), math.floor((lat + pad_lat) * CELL) + 1):
                for col in range(math.floor((lon - pad_lon) * CELL), math.floor((lon + pad_lon) * CELL) + 1):
                    cells.add((row + 40000) * 100000 + col + 50000)
        marks = json.dumps(sorted(cells))
        self.nodes = {n: (lat, lon) for n, lat, lon in db.execute(
            "SELECT id, lat, lon FROM node WHERE cell IN (SELECT value FROM json_each(?))", (marks,))}
        self.links, self.starts_at = defaultdict(list), {}
        for edge, a, b, metres in db.execute("SELECT id, a, b, metres FROM edge WHERE cell IN (SELECT value FROM json_each(?))", (marks,)):
            self.links[a].append((b, metres, edge))
            self.links[b].append((a, metres, edge))
            self.starts_at[edge] = a
        self.grid = defaultdict(list)
        for node, (lat, lon) in self.nodes.items():
            if node in self.links:
                self.grid[(math.floor(lat * FINE), math.floor(lon * FINE))].append((node, lat, lon))

    def snap(self, point, count=8):
        """[(node, metres)]: the points of the network nearest a place (up to count, within SNAP_SPREAD_M
        of the nearest), if within reach. Looks in widening rings of ~100 m cells, stopping once
        they're clearly inside the ring."""
        lat, lon = point
        across = 111_320 * math.cos(math.radians(lat))  # metres per degree of longitude here (flat is fine at this scale)
        row, col = math.floor(lat * FINE), math.floor(lon * FINE)
        rings = math.ceil(SNAP_M / 100) + 1
        for ring in range(1, rings + 1):
            near = []
            for dr in range(-ring, ring + 1):
                for dc in range(-ring, ring + 1):
                    for node, node_lat, node_lon in self.grid.get((row + dr, col + dc), ()):
                        metres = math.hypot((node_lat - lat) * 110_540, (node_lon - lon) * across)
                        if metres <= SNAP_M:
                            near.append((metres, node))
            near.sort()
            if near and near[0][0] + SNAP_SPREAD_M <= ring * 90:
                break
        if not near:
            return []
        # Every point near the nearest, so a place beside a stray fragment still reaches the streets.
        return [(node, metres) for metres, node in near[:count] if metres <= near[0][0] + SNAP_SPREAD_M]

    def spread(self, point, limit_m):
        """Walking distances from a place to every point of the network within limit_m:
        ({node: metres}, {node: (previous node, edge)}, the starting snaps), or None off the network."""
        starts = self.snap(point)
        if not starts:
            return None
        best, came, queue = {}, {}, []
        for node, metres in starts:
            if metres < best.get(node, math.inf):
                best[node] = metres
                came[node] = None
                heapq.heappush(queue, (metres, node))
        while queue:
            metres, node = heapq.heappop(queue)
            if metres > best.get(node, math.inf) or metres > limit_m:
                continue
            for other, length, edge in self.links.get(node, ()):
                total = metres + length
                if total < best.get(other, math.inf) and total <= limit_m:
                    best[other] = total
                    came[other] = (node, edge)
                    heapq.heappush(queue, (total, other))
        return best, came, starts

    def reach(self, spread, place):
        """(metres, node) from a spread's start to a place (a stop, a destination), or None."""
        best = spread[0]
        found = None
        for node, metres in self.snap(place):
            if node in best and (found is None or best[node] + metres < found[0]):
                found = (best[node] + metres, node)
        return found

    def shape(self, spread, node, start, end):
        """The walk as map points from start to end, along the network to `node` (end's snap)."""
        came = spread[1]
        steps = []
        while came.get(node):
            previous, edge = came[node]
            steps.append((previous, node, edge))
            node = previous
        steps.reverse()
        shapes = dict(self.db.execute("SELECT id, path FROM shape WHERE id IN (SELECT value FROM json_each(?))",
                                      (json.dumps([s[2] for s in steps]),)))
        points = [list(start)]
        if steps:
            points.append(list(self.nodes.get(steps[0][0], start)))
        for previous, node, edge in steps:
            inner = json.loads(shapes[edge]) if shapes.get(edge) else []
            points += [list(p) for p in (inner if self.starts_at.get(edge) == previous else inner[::-1])]
            points.append(list(self.nodes.get(node, end)))
        points.append(list(end))
        return points


class Walker:
    """A city's walking network, opened read-only."""

    def __init__(self, slug):
        self.db = sqlite3.connect(f"file:{path_for(slug)}?mode=ro", uri=True, check_same_thread=False)

    def area(self, points, reach_m):
        return Area(self.db, points, reach_m)


_cache = {}


def walker(slug):
    """The city's walking network (reopened after the monthly rebuild), or None if there isn't one yet."""
    try:
        modified = path_for(slug).stat().st_mtime
    except FileNotFoundError:
        return None
    cached = _cache.get(slug)
    if cached and cached[0] == modified:
        return cached[1]
    found = Walker(slug)
    _cache[slug] = (modified, found)
    return found


def secs(metres):
    return round(metres / SPEED)
