"""Bulk loaders for the open datasets behind place search.

- Airports: OurAirports (public domain), https://ourairports.com/data/
  plus Chinese names from OpenStreetMap.
- Railway and metro stations: OpenStreetMap via the Overpass API
  (© OpenStreetMap contributors, ODbL).
"""

import csv
import io
import logging
import urllib.request

from .geo import tidy_station_name
from .http import USER_AGENT, overpass
from .models import Place

log = logging.getLogger(__name__)

OURAIRPORTS_CSV = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OPENFLIGHTS_AIRLINES = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
STATION_COUNTRIES = ["CN", "HK", "TW", "MO"]
METRO_STATION_TYPES = {"subway", "light_rail", "monorail", "tram"}


def _upsert(source, places):
    """Create or update Place rows keyed by (source, source_id). Returns (created, updated)."""
    existing = {p.source_id: p for p in Place.objects.filter(source=source)}
    fields = ["kind", "name", "name_local", "code", "country", "lat", "lon", "tz", "aliases", "search", "city", "city_local"]
    new, changed = [], []
    for place in places:
        place.source = source
        current = existing.get(place.source_id)
        if current:
            for field in fields:
                if field != "tz":
                    setattr(current, field, getattr(place, field))
            current.refresh_derived()
            changed.append(current)
        else:
            place.refresh_derived()
            new.append(place)
    Place.objects.bulk_create(new, batch_size=500)
    Place.objects.bulk_update(changed, fields, batch_size=500)
    from .cities import fill_related_cities

    fill_related_cities()
    return len(new), len(changed)


def load_airports():
    req = urllib.request.Request(OURAIRPORTS_CSV, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as response:
        rows = csv.DictReader(io.TextIOWrapper(response, encoding="utf-8"))
        places = []
        for row in rows:
            code = row["iata_code"].strip()
            if not code or len(code) != 3:
                continue
            if row["type"] not in ("large_airport", "medium_airport") and row["scheduled_service"] != "yes":
                continue
            places.append(
                Place(
                    kind=Place.Kind.AIRPORT,
                    name=row["name"].strip(),
                    code=code,
                    country=row["iso_country"],
                    lat=float(row["latitude_deg"]),
                    lon=float(row["longitude_deg"]),
                    aliases=row["municipality"].strip(),
                    source_id=row["ident"],
                )
            )
    _add_local_airport_names(places)
    return _upsert("ourairports", places)


# OpenFlights hasn't caught up with some reassigned codes; these win over its entries.
AIRLINE_OVERRIDES = {
    "GT": ("CGH", "Air Guilin", "China"),
}


def load_airlines():
    """Active airlines with a two-letter IATA code; the first active entry wins for duplicate codes."""
    from .models import Airline

    req = urllib.request.Request(OPENFLIGHTS_AIRLINES, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as response:
        rows = csv.reader(io.TextIOWrapper(response, encoding="utf-8"))
        airlines = {}
        for row in rows:
            if len(row) < 8:
                continue
            _id, name, _alias, iata, icao, _callsign, country, active = row[:8]
            iata, icao = iata.strip().upper(), icao.strip().upper()
            if active == "Y" and len(iata) == 2 and iata.isalnum() and iata not in airlines and name != "\\N":
                airlines[iata] = Airline(code=iata, icao=icao if len(icao) == 3 and icao.isalpha() else "",
                                         name=name.strip()[:200], country=country.strip()[:100])
    for code, (icao, name, country) in AIRLINE_OVERRIDES.items():
        airlines[code] = Airline(code=code, icao=icao, name=name, country=country)
    Airline.objects.all().delete()
    Airline.objects.bulk_create(airlines.values(), batch_size=500)
    return len(airlines)


def _add_local_airport_names(places):
    """OurAirports has English names only; take Chinese names for airports in China, HK, Taiwan and Macau from OSM."""
    areas = "".join(f'area["ISO3166-1"="{cc}"][admin_level=2];' for cc in STATION_COUNTRIES)
    query = f'[out:json][timeout:180];({areas})->.a;nwr["aeroway"="aerodrome"]["iata"](area.a);out tags qt;'
    try:
        data = overpass(query)
    except RuntimeError as exc:
        log.warning("Skipping Chinese airport names: %s", exc)
        return
    local = {}
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        name = tags.get("name:zh") or tags.get("name:zh-Hans") or tags.get("name:zh-Hant") or tags.get("name", "")
        if any("一" <= ch <= "鿿" for ch in name):
            local[tags["iata"].strip().upper()] = name
    for place in places:
        place.name_local = local.get(place.code, "")


def load_stations(countries=STATION_COUNTRIES):
    total_created = total_updated = 0
    for cc in countries:
        # No admin_level filter: Hong Kong and Macau are level 3 in OpenStreetMap (inside China's level-2 area).
        query = (
            f'[out:json][timeout:300];area["ISO3166-1"="{cc}"]["boundary"="administrative"]->.a;'
            f'nwr["railway"~"^(station|halt)$"](area.a);out center tags qt;'
        )
        places = []
        seen = set()
        for element in overpass(query, timeout=320).get("elements", []):
            place = _station_from_osm(element, cc)
            if place and place.source_id not in seen:
                seen.add(place.source_id)
                places.append(place)
        places = _drop_duplicates(places)
        created, updated = _upsert("osm", places)
        _remove_stale(cc, {p.source_id for p in places})
        log.info("%s: %d stations (%d new)", cc, len(places), created)
        total_created += created
        total_updated += updated
    return total_created, total_updated


def _remove_stale(country, current_ids):
    """Delete stations no longer in the data (or dropped as duplicates), unless a trip uses them."""
    from .models import Day, Item, Leg, Stay

    used = set(Leg.objects.values_list("origin_id", flat=True)) | set(Leg.objects.values_list("destination_id", flat=True))
    used |= set(Day.objects.values_list("overnight_id", flat=True)) | set(Item.objects.values_list("place_id", flat=True))
    used |= set(Stay.objects.values_list("city_id", flat=True)) | set(Stay.objects.values_list("location_id", flat=True))
    stale = Place.objects.filter(source="osm", country=country).exclude(source_id__in=current_ids).exclude(pk__in=used)
    stale.delete()


def _drop_duplicates(places):
    """Stations are sometimes mapped twice (a point and an outline); keep one per name within 1 km, preferring points."""
    from .geo import distance_km

    kept = {}
    for place in sorted(places, key=lambda p: not p.source_id.startswith("node/")):
        key = (place.kind, place.name_local or place.name)
        if any(distance_km(other.coords, place.coords) < 1 for other in kept.get(key, [])):
            continue
        kept.setdefault(key, []).append(place)
    return [place for group in kept.values() for place in group]


def _station_from_osm(element, country):
    tags = element.get("tags", {})
    if any(key in tags for key in ("disused", "abandoned", "disused:railway", "construction")):
        return None
    if tags.get("station") in ("funicular", "miniature") or tags.get("usage") == "tourism":
        return None
    local = tags.get("name:zh") or tags.get("name:zh-Hans") or tags.get("name:zh-Hant") or tags.get("name", "")
    english_raw = tags.get("name:en", "")
    if not local and not english_raw:
        return None
    centre = element.get("center", element)
    if "lat" not in centre:
        return None
    is_metro = tags.get("station") in METRO_STATION_TYPES or (tags.get("subway") == "yes" and tags.get("train") != "yes")
    name = tidy_station_name(english_raw, local) or local
    aliases = {english_raw, tags.get("name", ""), tags.get("name:zh_pinyin", ""), tags.get("name:zh-Hant", "")}
    aliases.discard(name)
    aliases.discard(local)
    return Place(
        kind=Place.Kind.METRO if is_metro else Place.Kind.STATION,
        name=name[:200],
        name_local=local[:200] if local != name else "",
        country=country,
        lat=round(centre["lat"], 6),
        lon=round(centre["lon"], 6),
        aliases=" ".join(sorted(a for a in aliases if a))[:300],
        source_id=f"{element['type']}/{element['id']}",
    )
