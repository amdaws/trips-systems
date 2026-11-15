"""Work out the line each leg is drawn with on the map.

- Flights: the real track of a recent flight with the same number (Flightradar24
  when a token is set, otherwise adsb.lol's open archive), or a great-circle arc.
- Trains: shortest path along OpenStreetMap railway track between the two
  stations, preferring high-speed lines for G/D/C trains.
- Boats: along the river (OpenStreetMap waterways).
- Taxis, buses, shuttles: road route from the OSRM demo server.
- Anything else, or if a lookup fails: a straight line marked as approximate.

Network lookups take up to a minute, so they run in the background job
(`manage.py run_jobs`); only flights and straight lines are drawn instantly.
"""

import heapq
import logging
import math
from collections import defaultdict

from django.utils import timezone

from . import transit
from .geo import corridor, distance_km, great_circle, simplify
from .http import get_json, overpass
from .models import Leg

log = logging.getLogger(__name__)

SNAP_KM = 5  # how far a station may be from the track it's snapped onto
BRIDGE_KM = 0.15  # track ends closer than this to another track are treated as joined
JOIN_KM = 0.3  # largest gap closed when a journey's start and end are in separate pieces of the network
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{a_lon},{a_lat};{b_lon},{b_lat}"
NEEDS_NETWORK = {Leg.Mode.TRAIN, Leg.Mode.BOAT, Leg.Mode.CAR, Leg.Mode.BUS, Leg.Mode.SHUTTLE}
TIMETABLE_MODES = {Leg.Mode.TRAIN, Leg.Mode.METRO, Leg.Mode.FERRY, Leg.Mode.BOAT}  # drawn from a city's timetable when both ends are its stations


def refresh_path(leg, allow_network=True):
    """Set leg.path for the leg's mode and save it. Network modes wait for the background job unless allowed."""
    a, b = leg.origin.coords, leg.destination.coords
    Mode, Source, Status = Leg.Mode, Leg.PathSource, Leg.PathStatus
    path = source = None
    message = ""

    if leg.mode == Mode.FLIGHT:
        from . import adsb_archive, flight_tracks

        if flight_tracks.enabled() and leg.number and leg.origin.code and leg.destination.code:
            if not allow_network:
                _save(leg, great_circle(a, b), Source.GREAT_CIRCLE, Status.PENDING, "Waiting for the background job to fetch the real flight track.")
                return leg
            try:
                path, message = flight_tracks.flown_path(leg)
                _save(leg, path, Source.FLOWN, Status.OK, message)
                return leg
            except flight_tracks.TrackError as exc:
                message = f"{exc} Drawn as a great-circle arc."
        elif adsb_archive.enabled() and leg.number:
            if not allow_network:
                _save(leg, great_circle(a, b), Source.GREAT_CIRCLE, Status.PENDING, "Waiting for the background job to look the flight up in adsb.lol's archive.")
            else:
                trace_flights([leg])
            return leg
        path, source = great_circle(a, b), Source.GREAT_CIRCLE
    elif leg.mode in TIMETABLE_MODES and (journey := transit.journey_between(leg.origin, leg.destination)):
        path, source, message = transit.leg_path(*journey), Source.TIMETABLE, transit.summary(journey[1])
    elif leg.mode in NEEDS_NETWORK:
        if not allow_network:
            _save(leg, None, "", Status.PENDING, "Waiting for the background job to trace the route.")
            return leg
        # Mark it approximate first: if this lookup is ever killed (e.g. the job's memory cap),
        # the leg isn't picked up again and again; Retrace tries once more.
        _save(leg, [list(a), list(b)], Source.STRAIGHT, Status.APPROX,
              "Tracing was interrupted; drawn as a straight line. Use Retrace to try again.")
        try:
            if leg.mode == Mode.TRAIN:
                path, source = rail_path(a, b, high_speed=is_high_speed(leg.number)), Source.RAIL
            elif leg.mode == Mode.BOAT:
                path, source = water_path(a, b), Source.WATER
            else:
                path, source = road_path(a, b), Source.ROAD
            if not path:
                message = "No connected route found in the map data; drawn as a straight line."
        except Exception as exc:  # network trouble shouldn't break the planner
            log.warning("Route lookup failed for leg %s: %s", leg.pk, exc)
            message = f"Route lookup failed ({exc}); drawn as a straight line."

    if path:
        _save(leg, path, source, Status.OK, message)
    else:
        _save(leg, [list(a), list(b)], Source.STRAIGHT, Status.APPROX if leg.mode in NEEDS_NETWORK else Status.OK, message)
    return leg


def trace_flights(legs):
    """Real tracks for flights from adsb.lol's archive: one read of the newest day for all of them."""
    from . import adsb_archive, flight_tracks

    Source, Status = Leg.PathSource, Leg.PathStatus
    # Drawn as arcs first, so a read that's cut short isn't started again and again; Retrace tries again.
    for leg in legs:
        _save(leg, great_circle(leg.origin.coords, leg.destination.coords), Source.GREAT_CIRCLE, Status.OK,
              "Looking this flight up was interrupted; drawn as a great-circle arc. Use Retrace to try again.")
    try:
        day, tracks = adsb_archive.find_tracks(legs)
    except flight_tracks.TrackError as exc:
        log.warning("adsb.lol archive read failed: %s", exc)
        for leg in legs:
            _save(leg, leg.path, Source.GREAT_CIRCLE, Status.OK, f"{exc} Drawn as a great-circle arc; use Retrace to try again.")
        return
    for leg in legs:
        if leg.pk in tracks:
            path, message = tracks[leg.pk]
            _save(leg, path, Source.ARCHIVE, Status.OK, message)
        else:
            _save(leg, leg.path, Source.GREAT_CIRCLE, Status.OK,
                  f"{leg.number} isn't in adsb.lol's archive for {day:%-d %b %Y} (no receivers along this route, or it "
                  "didn't fly that day). Drawn as a great-circle arc; use Retrace to look again.")


def _save(leg, path, source, status, message):
    now = timezone.now()
    leg.path, leg.path_source, leg.path_status, leg.path_message, leg.path_updated = path, source, status, message[:300], now
    Leg.objects.filter(pk=leg.pk).update(
        path=path, path_source=source, path_status=status, path_message=message[:300], path_updated=now
    )


def is_high_speed(number):
    return bool(number) and number[0].upper() in "GDC"


# ---- Railways and rivers: shortest path over OpenStreetMap ways ----


def rail_path(a, b, high_speed=True):
    dist = distance_km(a, b)
    # Every running line, plus the station sidings and crossovers that often join one line to
    # another at big junctions (yards and spurs left out).
    every_line = ['["railway"="rail"]["service"!~"^(yard|spur)$"]']
    # The fast network only: lines marked high-speed, or with a 160 km/h+ limit (many Chinese
    # high-speed lines carry only the speed tag). Much smaller downloads for long journeys.
    fast_lines = ['["railway"="rail"]["highspeed"="yes"]', '["railway"="rail"]["maxspeed"~"^(1[6-9][0-9]|[2-4][0-9][0-9])"]']

    def weight(tags):
        fast = tags.get("highspeed") == "yes" or (tags.get("maxspeed", "")[:3].isdigit() and int(tags["maxspeed"][:3]) >= 160)
        factor = (0.6 if fast else 1.0) if high_speed else (1.5 if fast else 1.0)
        return factor * (3 if tags.get("service") else 1)  # station tracks only to bridge lines

    width = min(max(40, dist * 0.25), 250)
    if dist <= 700 or not high_speed:
        attempts = [(every_line, width), (every_line, width * 1.8)]
    else:
        attempts = [(fast_lines, width), (fast_lines, width * 1.6), (every_line, min(width, 110))]
    for selectors, corridor_width in attempts:
        path = _shortest_path(_ways_along(a, b, selectors, corridor_width), a, b, weight)
        if path:
            return path
    return None


def _ways_along(a, b, selectors, width):
    """OpenStreetMap ways matching the selectors in a corridor around a->b, fetched in sections of
    at most ~450 km so each download stays small enough to finish reliably."""
    sections = max(1, math.ceil(distance_km(a, b) / 450))
    points = [[a[0] + (b[0] - a[0]) * i / sections, a[1] + (b[1] - a[1]) * i / sections] for i in range(sections + 1)]
    ways = {}
    for start, end in zip(points, points[1:]):
        poly = _poly(corridor(start, end, width))
        parts = "".join(f'way{selector}(poly:"{poly}");' for selector in selectors)
        for element in overpass(f"[out:json][timeout:180];({parts});out body geom qt;").get("elements", []):
            if element.get("type") == "way":
                ways[element["id"]] = element
    return {"elements": list(ways.values())}


def water_path(a, b):
    width = min(max(15, distance_km(a, b) * 0.3), 100)
    poly = _poly(corridor(a, b, width))
    data = overpass(f'[out:json][timeout:120];way["waterway"~"^(river|canal)$"](poly:"{poly}");out body geom qt;')
    return _shortest_path(data, a, b, lambda tags: 1.0)


def _poly(points):
    return " ".join(f"{lat:.5f} {lon:.5f}" for lat, lon in points)


def _shortest_path(data, a, b, weight):
    coords = {}
    graph = defaultdict(list)
    for way in data.get("elements", []):
        if way.get("type") != "way" or not way.get("geometry"):
            continue
        factor = weight(way.get("tags", {}))
        ids, geometry = way["nodes"], way["geometry"]
        for i in range(len(ids) - 1):
            if not geometry[i] or not geometry[i + 1]:
                continue
            u, v = ids[i], ids[i + 1]
            coords[u] = (geometry[i]["lat"], geometry[i]["lon"])
            coords[v] = (geometry[i + 1]["lat"], geometry[i + 1]["lon"])
            cost = distance_km(coords[u], coords[v]) * factor
            graph[u].append((v, cost))
            graph[v].append((u, cost))
    if not coords:
        return None
    _bridge_gaps(coords, graph)

    start = min(coords, key=lambda n: distance_km(coords[n], a))
    end = min(coords, key=lambda n: distance_km(coords[n], b))
    if distance_km(coords[start], a) > SNAP_KM or distance_km(coords[end], b) > SNAP_KM:
        return None
    _join_pieces(coords, graph, start, end)

    best = {start: 0.0}
    previous = {}
    queue = [(0.0, start)]
    while queue:
        cost, node = heapq.heappop(queue)
        if node == end:
            break
        if cost > best.get(node, float("inf")):
            continue
        for neighbour, step in graph[node]:
            new_cost = cost + step
            if new_cost < best.get(neighbour, float("inf")):
                best[neighbour] = new_cost
                previous[neighbour] = node
                heapq.heappush(queue, (new_cost, neighbour))
    if end not in best:
        return None

    route = [end]
    while route[-1] != start:
        route.append(previous[route[-1]])
    route.reverse()
    return simplify([list(a)] + [list(coords[n]) for n in route] + [list(b)])


def _bridge_gaps(coords, graph):
    """Join track ends that stop just short of another track. The map data often leaves a few
    metres between lines at stations (e.g. 15 m at Guilin West), which would split the network.
    Only ends are joined, so a line crossing over another on a bridge isn't treated as a junction."""
    cell = 0.002  # about 200 m
    grid = defaultdict(list)
    for node, (lat, lon) in coords.items():
        grid[(int(lat / cell), int(lon / cell))].append(node)
    for end in [n for n, links in graph.items() if len(links) <= 1]:
        here = coords[end]
        key = (int(here[0] / cell), int(here[1] / cell))
        linked = {n for n, _cost in graph[end]}
        best, best_km = None, BRIDGE_KM
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((key[0] + dx, key[1] + dy), ()):
                    if other != end and other not in linked:
                        km = distance_km(here, coords[other])
                        if km < best_km:
                            best, best_km = other, km
        if best is not None:
            cost = best_km * 3 + 0.05
            graph[end].append((best, cost))
            graph[best].append((end, cost))


def _join_pieces(coords, graph, start, end):
    """If the start and destination are in separate pieces of the network, join the pieces where
    they come closest (at most JOIN_KM), one piece at a time, until the destination is reachable.
    Catches gaps between tracks that both carry on past each other, which _bridge_gaps can't."""
    reached = set()

    def flood(seed):
        reached.add(seed)
        stack = [seed]
        while stack:
            for neighbour, _cost in graph[stack.pop()]:
                if neighbour not in reached:
                    reached.add(neighbour)
                    stack.append(neighbour)

    flood(start)
    if end in reached:
        return
    cell = 0.003  # about 300 m
    grid = defaultdict(list)
    for node, (lat, lon) in coords.items():
        grid[(int(lat / cell), int(lon / cell))].append(node)
    for _attempt in range(30):
        best, best_km = None, JOIN_KM
        for u in reached:
            lat, lon = coords[u]
            key = (int(lat / cell), int(lon / cell))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for v in grid.get((key[0] + dx, key[1] + dy), ()):
                        if v not in reached:
                            km = distance_km(coords[u], coords[v])
                            if km < best_km:
                                best, best_km = (u, v), km
        if best is None:
            return
        u, v = best
        cost = best_km * 3 + 0.05
        graph[u].append((v, cost))
        graph[v].append((u, cost))
        flood(v)
        if end in reached:
            return


# ---- Roads ----


def road_path(a, b):
    url = OSRM_URL.format(a_lat=a[0], a_lon=a[1], b_lat=b[0], b_lon=b[1])
    data = get_json(url, {"overview": "full", "geometries": "geojson"}, timeout=30)
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    return simplify([[lat, lon] for lon, lat in data["routes"][0]["geometry"]["coordinates"]])
