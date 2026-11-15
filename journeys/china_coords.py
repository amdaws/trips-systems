"""Map positions from Chinese map sources, brought onto the world (WGS-84) system OpenStreetMap uses.

By law, maps of mainland China are drawn on an offset system, GCJ-02, which moves
points by a few hundred metres. Google's and Apple's maps of the mainland, Trip.com,
Amap (Gaode) and others give GCJ-02 positions; Baidu adds a second offset (BD-09).
Hong Kong, Macau and Taiwan aren't offset. Left as they are, a hotel can land next
to the wrong metro station (Trip.com's Chongqing hotels are about 500 m out).

The conversion is the standard published approximation, accurate to a metre or two.
"""

import math

_A = 6378245.0
_EE = 0.00669342162296594323

# Roughly the Hong Kong border (along the Shenzhen River) and Macau, as (lat, lon) outlines.
_HONG_KONG = [(22.14, 113.82), (22.40, 113.82), (22.505, 113.90), (22.51, 113.99), (22.513, 114.04), (22.512, 114.07),
              (22.53, 114.11), (22.545, 114.15), (22.56, 114.20), (22.56, 114.45), (22.14, 114.45)]
_MACAU = [(22.105, 113.552), (22.165, 113.552), (22.18, 113.528), (22.217, 113.528), (22.217, 113.60), (22.105, 113.60)]


def _inside(point, outline):
    lat, lon = point
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(outline, outline[1:] + outline[:1]):
        if (lon1 > lon) != (lon2 > lon) and lat < lat1 + (lon - lon1) * (lat2 - lat1) / (lon2 - lon1):
            inside = not inside
    return inside


def in_mainland(lat, lon):
    """Whether a point is in mainland China (where map positions are offset)."""
    if not (0.83 < lat < 55.83 and 72.0 < lon < 137.84):
        return False
    if 21.8 < lat < 25.4 and 119.3 < lon < 122.1:  # Taiwan
        return False
    return not (_inside((lat, lon), _HONG_KONG) or _inside((lat, lon), _MACAU))


def _shift(lat, lon):
    x, y = lon - 105.0, lat - 35.0
    d_lat = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    d_lat += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    d_lat += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    d_lat += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    d_lon = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    d_lon += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    d_lon += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    d_lon += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    rad = lat / 180 * math.pi
    magic = 1 - _EE * math.sin(rad) ** 2
    root = math.sqrt(magic)
    d_lat = d_lat * 180 / ((_A * (1 - _EE)) / (magic * root) * math.pi)
    d_lon = d_lon * 180 / (_A / root * math.cos(rad) * math.pi)
    return d_lat, d_lon


def wgs_to_gcj(lat, lon):
    d_lat, d_lon = _shift(lat, lon)
    return lat + d_lat, lon + d_lon


def gcj_to_wgs(lat, lon):
    """Undo the GCJ-02 offset (a few rounds of correction, to well under a metre)."""
    wlat, wlon = lat, lon
    for _ in range(5):
        glat, glon = wgs_to_gcj(wlat, wlon)
        wlat, wlon = wlat - (glat - lat), wlon - (glon - lon)
    return wlat, wlon


def bd_to_gcj(lat, lon):
    x, y = lon - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi * 3000 / 180)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi * 3000 / 180)
    return z * math.sin(theta), z * math.cos(theta)


def to_wgs84(lat, lon, datum):
    """(lat, lon) on the world system, from a position given in `datum` ("gcj02", "bd09" or "wgs84")."""
    lat, lon = float(lat), float(lon)
    if datum == "bd09":
        lat, lon = bd_to_gcj(lat, lon)
        datum = "gcj02"
    if datum == "gcj02" and in_mainland(lat, lon):
        lat, lon = gcj_to_wgs(lat, lon)
    return round(lat, 6), round(lon, 6)
