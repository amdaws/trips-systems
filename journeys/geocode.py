"""Place search with Nominatim (OpenStreetMap data, ODbL).

Only used for explicit searches: Nominatim's usage policy allows at most one
request per second and forbids search-as-you-type, so live autocomplete uses
the local place tables instead.
"""

import threading
import time

from .http import get_json
from .models import Place

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_lock = threading.Lock()
_last_call = [0.0]

KINDS = {
    ("aeroway", "aerodrome"): Place.Kind.AIRPORT,
    ("railway", "station"): Place.Kind.STATION,
    ("railway", "halt"): Place.Kind.STATION,
    ("amenity", "ferry_terminal"): Place.Kind.PIER,
    ("tourism", "hotel"): Place.Kind.HOTEL,
    ("tourism", "hostel"): Place.Kind.HOTEL,
    ("tourism", "guest_house"): Place.Kind.HOTEL,
    ("tourism", "apartment"): Place.Kind.HOTEL,
}
CITY_TYPES = {"city", "town", "village", "suburb", "county", "municipality", "administrative", "district", "hamlet", "quarter"}


def search(query, limit=6, country=""):
    """Places matching the words (optionally only in one country, by its two-letter code)."""
    params = {"q": query, "format": "jsonv2", "namedetails": 1, "addressdetails": 1, "limit": limit, "accept-language": "en"}
    if country:
        params["countrycodes"] = country.lower()
    with _lock:
        wait = 1.1 - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        results = get_json(NOMINATIM_URL, params, timeout=20)
    return [_describe(r) for r in results]


def _describe(result):
    names = result.get("namedetails") or {}
    local = names.get("name:zh") or names.get("name:zh-Hans") or names.get("name:zh-Hant") or names.get("name", "")
    english = names.get("name:en") or result.get("name") or result["display_name"].split(",")[0]
    category, kind_type = result.get("category", ""), result.get("type", "")
    if (category, kind_type) in KINDS:
        kind = KINDS[(category, kind_type)]
    elif category in ("place", "boundary") or kind_type in CITY_TYPES:
        kind = Place.Kind.CITY
    elif category in ("tourism", "leisure", "historic", "natural"):
        kind = Place.Kind.ATTRACTION
    else:
        kind = Place.Kind.OTHER
    return {
        "source_id": f"{result['osm_type']}/{result['osm_id']}",
        "name": english[:200],
        "name_local": local[:200] if local != english else "",
        "kind": kind,
        "lat": float(result["lat"]),
        "lon": float(result["lon"]),
        "country": (result.get("address") or {}).get("country_code", "").upper()[:2],
        "city": _city_from_address(result.get("address") or {}, kind, english),
        "label": result["display_name"],
    }


def _city_from_address(address, kind, name):
    if kind == Place.Kind.CITY:
        return name[:120]
    for key in ("city", "town", "village", "municipality", "county", "state"):
        if address.get(key):
            return address[key][:120]
    return ""


def save_result(result, kind=None, name=None, name_local=None):
    """Create (or reuse) a Place from a search result."""
    kind = kind or result["kind"]
    name = name or result["name"]
    place, _ = Place.objects.get_or_create(
        source="nominatim",
        source_id=result["source_id"],
        defaults={
            "kind": kind,
            "name": name,
            "name_local": result["name_local"] if name_local is None else name_local,
            "lat": result["lat"],
            "lon": result["lon"],
            "country": result["country"],
            "city": name if kind == Place.Kind.CITY else result.get("city", ""),
        },
    )
    return place
