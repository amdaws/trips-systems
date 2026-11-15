"""Work out the city each airport and station serves, so legs read 'Brisbane → Hong Kong'."""

import re

from .geo import distance_km

DIRECTIONS_ZH = "东西南北東"


CITY, AIRPORT, STATION, METRO = "city", "airport", "station", "metro"

# Airports named after a suburb or neighbouring town rather than the city travellers mean.
AIRPORT_CITIES = {
    "TPE": "Taipei", "NRT": "Tokyo", "KUL": "Kuala Lumpur", "DPS": "Denpasar", "CGK": "Jakarta",
    "FCO": "Rome", "MXP": "Milan", "EWR": "New York", "LGW": "London", "STN": "London", "LTN": "London",
    "ORY": "Paris", "BVA": "Paris", "IAD": "Washington", "YYZ": "Toronto", "OOL": "Gold Coast",
}


def derive_city(place):
    """(English city, local city) for a place, from its own data; '' when it needs a lookup (metro, other)."""
    if place.kind == CITY:
        return place.name, place.name_local
    if place.country == "HK" and place.kind in (STATION, METRO, AIRPORT):
        return "Hong Kong", "香港"
    if place.country == "MO" and place.kind in (STATION, METRO, AIRPORT):
        return "Macau", "澳門"
    if place.kind == AIRPORT:
        if place.code in AIRPORT_CITIES:
            return AIRPORT_CITIES[place.code], ""
        # OurAirports keeps the municipality in aliases, e.g. "Shanghai (Pudong)" or "Kuta, Badung".
        municipality = place.aliases if place.source == "ourairports" else ""
        return re.sub(r"\s*\(.*?\)", "", municipality).split(",")[0].strip(), ""
    if place.kind == STATION:
        name = place.name
        bracket = re.search(r"\(([^)]+)\)", name)  # "Xianggelila (Shangri-La)" -> "Shangri-La"
        if bracket:
            name = bracket.group(1)
        name = re.sub(r"\s+(Main Station|Railway Station|Station|HSR)$", "", name, flags=re.I)
        name = re.sub(r"\s+(North|South|East|West)$", "", name)
        local = re.sub(r"(火车站|車站|车站|站)$", "", place.name_local or "")
        if len(local) >= 3 and local[-1] in DIRECTIONS_ZH:
            local = local[:-1]
        return name.strip(), local
    return "", ""


def fill_related_cities(place_model=None):
    """Second pass for what a place can't work out alone:
    - metro stations take the city of the nearest railway station (within 40 km);
    - airports in China, Hong Kong, Taiwan and Macau get the Chinese city name.
    Returns how many places were updated. (place_model lets migrations pass their historical model.)"""
    if place_model is None:
        from .models import Place as place_model
    Place = place_model
    fields = ["lat", "lon", "city", "city_local", "country", "kind"]

    stations = list(Place.objects.filter(kind=STATION).exclude(city="").only(*fields))
    grid = {}
    for s in stations:
        grid.setdefault((round(s.lat * 2), round(s.lon * 2)), []).append(s)
    local_names = {}
    for p in Place.objects.filter(kind__in=[CITY, STATION]).exclude(city_local="").only(*fields):
        local_names.setdefault(p.city.lower(), p.city_local)

    changed = []
    for metro in Place.objects.filter(kind=METRO, city="").only(*fields):
        key = (round(metro.lat * 2), round(metro.lon * 2))
        here = (metro.lat, metro.lon)
        nearby = [s for dx in (-1, 0, 1) for dy in (-1, 0, 1) for s in grid.get((key[0] + dx, key[1] + dy), [])
                  if s.country == metro.country]
        if nearby:
            nearest = min(nearby, key=lambda s: distance_km((s.lat, s.lon), here))
            if distance_km((nearest.lat, nearest.lon), here) <= 40:
                metro.city, metro.city_local = nearest.city, nearest.city_local
                changed.append(metro)
    for airport in Place.objects.filter(kind=AIRPORT, country__in=["CN", "HK", "TW", "MO"], city_local="").exclude(city="").only(*fields):
        if airport.city.lower() in local_names:
            airport.city_local = local_names[airport.city.lower()]
            changed.append(airport)
    Place.objects.bulk_update(changed, ["city", "city_local"], batch_size=500)
    return len(changed)
