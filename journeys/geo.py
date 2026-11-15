"""Small geography helpers: distances, time zones, arcs, line simplification, names."""

import math
import re
from functools import lru_cache

EARTH_RADIUS_KM = 6371.0088


@lru_cache(maxsize=1)
def _timezone_finder():
    from timezonefinder import TimezoneFinder

    return TimezoneFinder(in_memory=False)


def timezone_for(lat, lon):
    return _timezone_finder().timezone_at(lat=lat, lng=lon) or "UTC"


def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, h)))


def great_circle(a, b):
    """Points along the shortest path over the globe, roughly one every 50 km."""
    steps = max(16, min(256, int(distance_km(a, b) / 50)))
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    p1 = (math.cos(lat1) * math.cos(lon1), math.cos(lat1) * math.sin(lon1), math.sin(lat1))
    p2 = (math.cos(lat2) * math.cos(lon2), math.cos(lat2) * math.sin(lon2), math.sin(lat2))
    angle = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(p1, p2)))))
    if angle < 1e-9:
        return [list(a), list(b)]
    points = []
    for i in range(steps + 1):
        f = i / steps
        k1 = math.sin((1 - f) * angle) / math.sin(angle)
        k2 = math.sin(f * angle) / math.sin(angle)
        x, y, z = (k1 * u + k2 * v for u, v in zip(p1, p2))
        points.append([round(math.degrees(math.atan2(z, math.hypot(x, y))), 5), round(math.degrees(math.atan2(y, x)), 5)])
    return points


def fill_gaps(points, max_km=300):
    """Join positions further apart than max_km (gaps in tracking coverage) with great-circle arcs."""
    filled = [points[0]]
    for point in points[1:]:
        if distance_km(filled[-1], point) > max_km:
            filled.extend(great_circle(filled[-1], point)[1:-1])
        filled.append(point)
    return filled


def corridor(a, b, width_km):
    """A rectangle around the line a->b, extended width_km on every side, as [lat, lon] corners."""
    lat0 = math.radians((a[0] + b[0]) / 2)
    kx, ky = 111.32 * math.cos(lat0), 110.57  # km per degree

    def to_xy(p):
        return p[1] * kx, p[0] * ky

    def to_latlon(x, y):
        return [y / ky, x / kx]

    (ax, ay), (bx, by) = to_xy(a), to_xy(b)
    length = math.hypot(bx - ax, by - ay) or 1.0
    dx, dy = (bx - ax) / length, (by - ay) / length
    nx, ny = -dy, dx
    w = width_km
    return [
        to_latlon(ax - dx * w + nx * w, ay - dy * w + ny * w),
        to_latlon(bx + dx * w + nx * w, by + dy * w + ny * w),
        to_latlon(bx + dx * w - nx * w, by + dy * w - ny * w),
        to_latlon(ax - dx * w - nx * w, ay - dy * w - ny * w),
    ]


def simplify(points, tolerance=0.0003):
    """Douglas-Peucker simplification (tolerance in degrees; 0.0003 is about 30 m)."""
    if len(points) < 3:
        return [[round(p[0], 5), round(p[1], 5)] for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        (ay, ax), (by, bx) = points[start], points[end]
        seg = math.hypot(bx - ax, by - ay)
        worst, index = 0.0, None
        for i in range(start + 1, end):
            py, px = points[i]
            if seg == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / seg
            if d > worst:
                worst, index = d, i
        if index is not None and worst > tolerance:
            keep[index] = True
            stack.extend([(start, index), (index, end)])
    return [[round(p[0], 5), round(p[1], 5)] for p, k in zip(points, keep) if k]


_DIRECTIONS = {"东": ("East", "dong"), "西": ("West", "xi"), "南": ("South", "nan"), "北": ("North", "bei")}


def tidy_station_name(name_en, name_zh):
    """Turn OpenStreetMap's 'Zhangjiajiexi Railway Station' into 'Zhangjiajie West'."""
    name = re.sub(r"\s*(railway station|train station|station)$", "", (name_en or "").strip(), flags=re.I)
    if name and name_zh and len(name_zh) >= 3 and name_zh[-1] in _DIRECTIONS:
        english, pinyin = _DIRECTIONS[name_zh[-1]]
        match = re.fullmatch(rf"(.{{2,}}?)\s*{pinyin}", name, flags=re.I)
        if match:
            return f"{match.group(1).strip().title()} {english}"
    return name
