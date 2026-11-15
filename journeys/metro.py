"""Metro help for stays: the station nearest the hotel, and MetroMan metro-map links.

MetroMan (metroman.cn) has metro maps for about 50 cities in mainland China, Hong
Kong, Macau and Taiwan. Its map page takes ?from=<station>&to=<station> and draws
that journey, so each stay links the station you arrive at (by train, plane or
boat) to the station nearest the hotel, and the hotel's station to where you
leave from. MetroMan's route search gives the journey itself (lines, changes,
stops, time and fare), which the itinerary lists and the map draws stop by stop.
Where there's no metro to take (no MetroMan map of the city, or no
station near the airport or train station), the transfer gets a taxi drive time
from the OSRM demo server instead. Activities get the same directions from that
night's hotel.

MetroMan's station list has no map positions, so its stations are matched by name
to OpenStreetMap's (ODbL) to find the one nearest the hotel. A city's stations are
fetched by the background job and kept for a month (MetroNetwork); pages only
read what's stored.

Brisbane, Melbourne and Sydney use their official timetables instead (transit.py): the same
stations-and-journeys shape, with trains, trams and ferries too, worked out on this server.
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import time, timedelta

from django.db.models import Q
from django.utils import timezone

from . import geocode, transit
from .geo import distance_km
from .http import USER_AGENT, get_json, overpass
from .models import Item, Leg, MetroNetwork, Place, Stay
from .routing import OSRM_URL

log = logging.getLogger(__name__)

SITE = "https://www.metroman.cn"
REFRESH_DAYS = 30
CITY_BOX = (0.4, 0.45)  # degrees of latitude and longitude around the city centre (about 45 km) searched for stations
HOTEL_KM = 3  # a station further than this from the hotel isn't suggested
HUB_KM = 2  # a railway station or pier this close to a metro station counts as connected
AIRPORT_KM = 4  # airports are placed mid-field, further from their terminal stations
EXIT_M = 500  # how far around the hotel's station to look for its entrances
NEARBY_KM = 60  # a train or flight ending this close to the hotel is the way you arrive
WALK_KM = 0.8  # closer than this, the transfer is a walk rather than a taxi ride
LONG_MODES = {Leg.Mode.FLIGHT, Leg.Mode.TRAIN, Leg.Mode.BOAT, Leg.Mode.FERRY, Leg.Mode.BUS, Leg.Mode.SHUTTLE}
COLOUR = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def city_slug(name):
    """'Hong Kong' -> 'hongkong', "Xi'an" -> 'xian': MetroMan's city addresses."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def map_url(slug, start=None, end=None):
    params = "&".join(f"{key}={value}" for key, value in (("from", start), ("to", end)) if value)
    return f"{SITE}/en/maps/{slug}" + (f"?{params}" if params else "")


# ---- Fetching a city's stations (background job only) ----


def network_for(city):
    """The city's MetroNetwork: the official timetables in Brisbane, Melbourne and Sydney (built
    weekly by the job), elsewhere MetroMan's, fetched if missing or a month old. None if there's none."""
    timetable_city = transit.city_for(city.coords)
    if timetable_city:
        return transit.network(timetable_city)
    slug = city_slug(city.city_name)
    if not slug:
        return None
    network = MetroNetwork.objects.filter(slug=slug).first()
    if network is None or network.fetched_at < timezone.now() - timedelta(days=REFRESH_DAYS):
        stations, lines = fetch_network(slug, city.coords)
        network, _ = MetroNetwork.objects.update_or_create(
            slug=slug, defaults={"name": city.city_name, "stations": stations, "lines": lines}
        )
    return network if network.stations else None


def fetch_network(slug, centre):
    """MetroMan's stations and lines for a city, with map positions from OpenStreetMap. ([], {}) if there's no map."""
    try:
        page = _fetch_page(f"{SITE}/en/maps/{slug}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return [], {}
        raise
    found = re.search(r'class="mm-map-options">(.*?)</script>', page, re.S)
    if not found:
        raise RuntimeError("MetroMan's map page has changed; no station list found.")
    options = json.loads(found.group(1))
    lines = {
        line["slug"]: {
            "name": line.get("name", ""),
            "colour": line.get("color", "") if COLOUR.match(line.get("color", "")) else "",
            "text": line.get("textColor", "") if COLOUR.match(line.get("textColor", "")) else "",
        }
        for line in options.get("lines", [])
    }
    positions = _osm_positions(centre)
    stations = []
    for station in options.get("stations", []):
        keys = _variants(station.get("name"), station.get("secondaryName"), *station.get("search", "").split("|"))
        candidates = [spot for key in keys for spot in positions.get(key, ())]
        # Prefer OpenStreetMap's metro stations to railway stations of the same name, then the one nearest the centre.
        best = min(candidates, key=lambda spot: (not spot[2], distance_km(spot, centre)), default=None)
        stations.append({
            "slug": station["slug"],
            "name": station.get("name", station["slug"]),
            "local": station.get("secondaryName", ""),
            "lines": station.get("lines", []),
            "url": station.get("url", ""),
            "lat": best[0] if best else None,
            "lon": best[1] if best else None,
        })
    return stations, lines


def _fetch_page(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def _osm_positions(centre):
    """Name variant -> [(lat, lon, is_metro)] for the stations around a city centre."""
    lat, lon = centre
    box = f"({lat - CITY_BOX[0]:.4f},{lon - CITY_BOX[1]:.4f},{lat + CITY_BOX[0]:.4f},{lon + CITY_BOX[1]:.4f})"
    data = overpass(
        "[out:json][timeout:120];("
        f'nw["railway"~"^(station|halt)$"]{box};'
        f'nw["public_transport"="station"]["subway"="yes"]{box};'
        f'nw["public_transport"="station"]["light_rail"="yes"]{box};'
        ");out center tags qt;"
    )
    positions = {}
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        lat = element.get("lat", element.get("center", {}).get("lat"))
        lon = element.get("lon", element.get("center", {}).get("lon"))
        if lat is None or lon is None:
            continue
        metro = tags.get("station") in ("subway", "light_rail", "monorail") or "yes" in (tags.get("subway"), tags.get("light_rail"))
        names = [tags.get(key) for key in ("name", "name:zh", "name:zh-Hans", "name:zh-Hant", "name:en", "official_name", "alt_name")]
        for key in _variants(*names):
            positions.setdefault(key, []).append((lat, lon, metro))
    return positions


def _normal(text):
    text = re.sub(r"\s*(站|車站|车站|station|metro station|mtr station)$", "", text.strip().lower())
    return re.sub(r"[\s\-_/·・,.'’()（）]", "", text)


def _variants(*names):
    """Comparable forms of station names: whole, and each part of names like '金鐘 Admiralty' (Hong Kong)."""
    keys = set()
    for name in names:
        if not name:
            continue
        keys.add(_normal(name))
        for part in re.split(r"\s+(?=[^\x00-\x7f])|(?<=[^\x00-\x7f])\s+|;", name):
            keys.add(_normal(part))
    return {key for key in keys if len(key) >= 2}


# ---- Working out a stay's metro (background job only) ----


def reset(stay, address_changed=False, location_changed=False):
    """Have the background job look again after the stay is edited (the caller saves it)."""
    geocoded = bool((stay.metro or {}).get("geocoded")) and not location_changed
    if address_changed and geocoded:
        stay.location = None  # it was placed on the map from the old address
        geocoded = False
    stay.metro = {"pending": True, "geocoded": geocoded}


def refresh_stay(stay):
    """Find the hotel's metro station and save it as stay.metro."""
    info = _lookup(stay)
    Stay.objects.filter(pk=stay.pk).update(metro=info, location=stay.location)
    stay.metro = info
    return info


def _lookup(stay):
    info = {"geocoded": bool((stay.metro or {}).get("geocoded"))}
    if not stay.city:
        return dict(info, message="Set the stay's city to see its metro.")
    network = network_for(stay.city)
    if network:
        info["network"] = network.slug

    station = match_name(network, stay.nearest_metro) if network and stay.nearest_metro else None
    if stay.location is None and stay.address:
        stay.location = _place_address(stay, near=station)
        info["geocoded"] = stay.location is not None
    hotel = stay.location.coords if stay.location else None

    if network is None and transit.city_for(stay.city.coords):
        info["message"] = f"{stay.city.city_name}'s train, tram and ferry timetables aren't loaded yet."
        info["retry"] = (timezone.now() + timedelta(hours=3)).isoformat()
    elif network is None:
        info["message"] = f"MetroMan has no metro map for {stay.city.city_name}."
    elif station:
        info["manual"] = True
    elif hotel:
        found = nearest(network, hotel, HOTEL_KM)
        station = found[1] if found else None
    if network and station is None:
        if hotel:
            info["message"] = f"No metro station within {HOTEL_KM} km of the hotel."
        elif not stay.address:
            info["message"] = "Add the hotel's address (or its link) to find its nearest station."
        else:
            info["message"] = "Couldn't place the address on the map; pick the hotel's map location on the stay."

    if station:
        info["station"] = station["slug"]
        if hotel and station.get("lat") is not None:
            info["distance_m"] = round(distance_km(hotel, (station["lat"], station["lon"])) * 1000)
            exit_ref = _exit_from_text(stay.nearest_metro) or _nearest_exit(station, hotel)
        else:
            exit_ref = _exit_from_text(stay.nearest_metro)
        if exit_ref:
            info["exit"] = exit_ref
    info["taxi"], info["routes"] = _transfer_details(stay, network, station)
    return info


def _transfer_details(stay, network, station):
    """For the flights, trains or boats that arrive and leave: MetroMan's metro journey between
    the station there and the hotel's, or where the metro doesn't go, a taxi drive time."""
    legs = long_legs(stay.trip)
    target = _target(stay)
    estimates, routes = {}, {}
    for kind, leg in (("arrival", arrival_leg(stay, legs)), ("departure", departure_leg(stay, legs))):
        if leg is None:
            continue
        place = leg.destination if kind == "arrival" else leg.origin
        hub = nearest(network, place.coords, _hub_km(place)) if network else None
        if hub and station:
            if hub[1]["slug"] != station["slug"]:
                start, end = (hub[1], station) if kind == "arrival" else (station, hub[1])
                try:
                    route = find_route(network, start["slug"], end["slug"])
                except Exception as exc:
                    log.warning("Metro route %s -> %s failed: %s", start["slug"], end["slug"], exc)
                    route = None
                if route:
                    routes[kind] = route
            continue  # the metro goes there
        estimate = {"place": place.pk, "centre": stay.location is None}
        if distance_km(place.coords, target) < WALK_KM:
            estimate["walk_m"] = round(distance_km(place.coords, target) * 1000)
        else:
            drive = _drive(*((place.coords, target) if kind == "arrival" else (target, place.coords)))
            if not drive:
                continue
            estimate.update(drive)
        estimates[kind] = estimate
    return estimates, routes


def find_route(network, start, end):
    """The quickest journey between two of the network's stations: from MetroMan, or worked out
    here from a city's timetable."""
    if transit.is_timetable(network):
        return transit.route(network, start, end)
    return fetch_route(network.slug, start, end)


def map_link(network, start=None, end=None):
    """(address, label) of the network's own map or journey planner."""
    if transit.is_timetable(network):
        return transit.planner_link(network)
    return map_url(network.slug, start, end), f"{network.name} metro map"


def fetch_route(slug, start, end):
    """MetroMan's quickest metro journey between two stations: time, fare, changes, and the
    stations along each ride (walks between platforms left out)."""
    query = urllib.parse.urlencode({"from": start, "to": end})
    plans = json.loads(_fetch_page(f"{SITE}/en/maps/{slug}/routes?{query}")).get("plans") or []
    if not plans:
        return None
    plan = plans[0]
    rides = []
    for ride in plan.get("legs", []):
        if not ride.get("ride"):
            continue
        rides.append({
            "line": (ride.get("name") or "")[:60],
            "colour": ride.get("color", "") if COLOUR.match(ride.get("color") or "") else "",
            "text": ride.get("textColor", "") if COLOUR.match(ride.get("textColor") or "") else "",
            "from": (ride.get("from") or "")[:100],
            "to": (ride.get("to") or "")[:100],
            "stops": [point["slug"] for point in ride.get("points", []) if point.get("slug")],
            "count": (ride.get("stopsText") or "")[:20],
        })
    return {"from": start, "to": end, "duration": (plan.get("duration") or "")[:20], "fare": (plan.get("fare") or "")[:20],
            "transfers": (plan.get("transfers") or "")[:30], "rides": rides}


def _drive(a, b):
    """{'minutes', 'km'} by road (free-flowing traffic), from the OSRM demo server."""
    try:
        data = get_json(OSRM_URL.format(a_lat=a[0], a_lon=a[1], b_lat=b[0], b_lon=b[1]), {"overview": "false"}, timeout=30)
    except Exception as exc:
        log.warning("Drive time lookup failed: %s", exc)
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    route = data["routes"][0]
    return {"minutes": max(1, round(route["duration"] / 60)), "km": round(route["distance"] / 1000, 1)}


def _place_address(stay, near=None):
    """The hotel's map position from its address, as a Place. With `near` (the station the stay
    names), a position further than HOTEL_KM from it is taken to be wrong."""
    for query in address_queries(stay.address, stay.city.city_name):
        try:
            results = geocode.search(query, limit=1)
        except Exception as exc:
            log.warning("Geocoding stay %s failed: %s", stay.pk, exc)
            return None
        if not results:
            continue
        spot = (results[0]["lat"], results[0]["lon"])
        if distance_km(spot, stay.city.coords) > NEARBY_KM:
            continue
        if near and near.get("lat") is not None and distance_km(spot, (near["lat"], near["lon"])) > HOTEL_KM:
            continue
        return geocode.save_result(results[0], kind=Place.Kind.HOTEL, name=(stay.name or results[0]["name"])[:200])
    return None


UNIT = re.compile(r"(?i)^(flat|unit|room|rm|suite|shop|floor|fl|block|blk)\b|^\d+\s*/?\s*f$|^\d+(st|nd|rd|th)\s+floor$|^[a-z]?\d+[a-z]?$|[楼樓室]$")
REGION = re.compile(r"(?i)\b(district|province|county|prefecture|city|china|taiwan|prc|sar)\b|[区區省市县縣]$")


def address_queries(address, city):
    """Ways to ask the geocoder for an address, most exact first. Flat and floor numbers, and
    building names next to street numbers, often stop it matching anything."""
    parts = [" ".join(part.split()) for part in re.split(r"[,，\n]", address or "") if part.strip()]
    kept = []
    for part in parts:
        if UNIT.search(part):
            continue
        part = re.sub(r"(?i)^no\.?\s*", "", part)
        kept.append(re.sub(r"^(\d+)\s*-\s*\d+\b", r"\1", part))  # "58-64 Nathan Road" -> "58 Nathan Road"
    local = [part for part in kept if not REGION.search(part) and part.lower() != city.lower()]
    queries = [" ".join(address.split()) if address else "", ", ".join(kept)]
    queries += [f"{part}, {city}" for part in local if not re.search(r"\d", part)]  # building names
    queries += [f"{part}, {city}" for part in local if re.search(r"\d", part) and re.search(r"[A-Za-z一-鿿]{3,}", part)]  # "58 Nathan Road"
    return [query for query in dict.fromkeys(queries) if query][:6]


def match_name(network, text):
    """The station named in free text such as 'Taipei 101/ World Trade Center Station - Exit 1'."""
    wanted = _normal(re.sub(r"(?i)\b(exit|gate|出口).*$", "", text))
    best = None
    for station in network.stations:
        for key in _variants(station["name"], station["local"]):
            if len(key) >= 3 and key in wanted and (best is None or len(key) > best[0]):
                best = (len(key), station)
    return best[1] if best else None


def _exit_from_text(text):
    found = re.search(r"(?i)\b(?:exit|gate)\s*([A-Z]?\d{0,2}[A-Z]?)\b", text or "")
    return found.group(1).upper() if found and found.group(1) else ""


def _nearest_exit(station, hotel):
    try:
        data = overpass(
            f'[out:json][timeout:60];node["railway"="subway_entrance"]'
            f'(around:{EXIT_M},{station["lat"]:.5f},{station["lon"]:.5f});out qt;'
        )
    except Exception as exc:
        log.warning("Station exits lookup failed: %s", exc)
        return ""
    exits = [
        (distance_km(hotel, (node["lat"], node["lon"])), node.get("tags", {}).get("ref", ""))
        for node in data.get("elements", [])
        if node.get("tags", {}).get("ref")
    ]
    return min(exits)[1][:10] if exits else ""


def nearest(network, point, within_km):
    """(km, station) for the network's station nearest the point, if within the distance."""
    best = min(
        ((distance_km(point, (s["lat"], s["lon"])), s) for s in network.stations if s.get("lat") is not None),
        key=lambda pair: pair[0],
        default=None,
    )
    return best if best and best[0] <= within_km else None


# ---- What the planner shows (reads stored data only) ----


def long_legs(trip):
    return [leg for leg in trip.legs.select_related("origin", "destination") if leg.mode in LONG_MODES]


def _target(stay):
    return stay.location.coords if stay.location else stay.city.coords


def arrival_leg(stay, legs):
    """The flight, train or boat that brings you to the stay (arriving on check-in day, or overnight)."""
    target = _target(stay)
    arriving = [
        leg for leg in legs
        if (leg.arrive_date or leg.depart_date) in (stay.check_in_date, stay.check_in_date - timedelta(days=1))
        and distance_km(leg.destination.coords, target) < NEARBY_KM
    ]
    return max(arriving, key=lambda l: (l.arrive_date or l.depart_date, l.arrive_time or l.depart_time or time.min), default=None)


def departure_leg(stay, legs):
    """The flight, train or boat you leave on, on check-out day."""
    target = _target(stay)
    leaving = [leg for leg in legs if leg.depart_date == stay.check_out_date and distance_km(leg.origin.coords, target) < NEARBY_KM]
    return min(leaving, key=lambda l: l.depart_time or time.max, default=None)


def for_stays(trip, stays, exact=True):
    """Attach .metro_view (station, line colours, MetroMan links and taxi times) to each stay.
    Without exact (visitors), the animated routes stop at the hotel's station: no hotel position."""
    stays = list(stays)
    slugs = {(stay.metro or {}).get("network") for stay in stays} - {None}
    networks = {network.slug: network for network in MetroNetwork.objects.filter(slug__in=slugs)}
    legs = long_legs(trip) if exact else [leg for leg in long_legs(trip) if leg.is_public]
    for stay in stays:
        stay.metro_view = _view(stay, networks.get((stay.metro or {}).get("network")), legs, exact) if stay.city else None
    return stays


def transfers_by_leg(trip, public_only=False, exact=True):
    """For the itinerary: leg id -> {"after": [...], "before": [...]}, the metro or taxi between
    each stay and the flight, train or boat that brings you there or takes you on (only the
    public stays, for visitors)."""
    moves = defaultdict(lambda: {"after": [], "before": []})
    stays = trip.stays.select_related("city", "location")
    for stay in for_stays(trip, stays.filter(is_public=True) if public_only else stays, exact=exact):
        view = stay.metro_view or {}
        for kind, slot in (("arrival", "after"), ("departure", "before")):
            by_metro, by_taxi = view.get(kind), view.get(f"{kind}_taxi")
            if by_metro or by_taxi:
                moves[(by_metro or by_taxi)["leg_id"]][slot].append({
                    "kind": kind, "stay": stay.label, "public": stay.is_public, "station": view.get("station"),
                    "metro": by_metro, "taxi": by_taxi,
                })
    return dict(moves)


def _view(stay, network, legs, exact=True):
    info = stay.metro or {}
    if info.get("pending") or not info:
        return {"message": "Finding the nearest metro station…"}
    view = {"message": info.get("message", "")}
    station = None
    if network:
        link, label = map_link(network)
        view.update(network=network.name, map_url=link, map_label=label)
        station = {s["slug"]: s for s in network.stations}.get(info.get("station"))
    if station:
        view["station"] = {
            "name": station["name"],
            "local": station["local"] if station["local"] != station["name"] else "",
            "url": SITE + station["url"] if station["url"].startswith("/") else "",
            "lines": [network.lines[slug] for slug in station["lines"] if slug in network.lines],
            "exit": info.get("exit", ""),
            "distance": _distance(info.get("distance_m")),
            "lat": station["lat"],
            "lon": station["lon"],
        }
    taxi, routes = info.get("taxi") or {}, info.get("routes") or {}
    for kind, leg in (("arrival", arrival_leg(stay, legs)), ("departure", departure_leg(stay, legs))):
        if leg is None:
            continue
        place = leg.destination if kind == "arrival" else leg.origin
        hub = nearest(network, place.coords, _hub_km(place)) if network else None
        if hub:
            start, end = (hub[1], station) if kind == "arrival" else (station, hub[1])
            view[kind] = _hub_view(network, leg, place, hub[1], start=start, end=end, exact=exact)
            route = routes.get(kind)
            if route and start and end and (route["from"], route["to"]) == (start["slug"], end["slug"]):
                view[kind]["route"] = _route_view(network, route)
                hotel = stay.location.coords if stay.location and exact else None
                walk = [[station["lat"], station["lon"]], hotel] if hotel and station.get("lat") is not None else None
                view[kind]["segments"] = _segments(view[kind]["route"], walk_after=walk if kind == "arrival" else None,
                                                   walk_before=walk[::-1] if walk and kind == "departure" else None)
        estimate = taxi.get(kind)
        if estimate and estimate.get("place") == place.pk:
            view[f"{kind}_taxi"] = _taxi_view(leg, place, estimate, stay, exact)
    return view


def _hub_km(place):
    return AIRPORT_KM if place.kind == Place.Kind.AIRPORT else HUB_KM


def _leg_label(leg, exact):
    """'Flight CX461', or for visitors just 'Flight' (numbers aren't public)."""
    return f"{leg.mode_label} {leg.number}".strip() if exact else leg.mode_label


def _hub_view(network, leg, place, hub, start, end, exact=True):
    return {
        "leg_id": leg.pk,
        "station": hub["name"],
        "place": place.name,
        "leg": _leg_label(leg, exact),
        "url": map_link(network, start and start["slug"], end and end["slug"])[0],
        "same": start is end,
    }


def _route_view(network, route):
    """The stored journey, with each ride as map points (along the line's real path, from a timetable)."""
    if transit.is_timetable(network):
        spots = {s["slug"]: [s["lat"], s["lon"]] for s in network.stations}
        rides = [dict(ride, path=transit.ride_path(network, ride), dots=[spots[slug] for slug in ride["stops"] if slug in spots])
                 for ride in route["rides"]]
        return {"duration": route["duration"], "fare": route["fare"], "transfers": route["transfers"], "rides": rides}
    spots = {s["slug"]: [s["lat"], s["lon"]] for s in network.stations if s.get("lat") is not None}
    rides = [dict(ride, path=[spots[slug] for slug in ride["stops"] if slug in spots]) for ride in route["rides"]]
    return {"duration": route["duration"], "fare": route["fare"], "transfers": route["transfers"], "rides": rides}


def _segments(route, walk_before=None, walk_after=None):
    """The journey as map pieces for the animation: walks, then each ride in its line colour (JSON)."""
    pieces = [{"path": walk_before, "walk": True}] if walk_before else []
    pieces += [{"path": ride["path"], "walk": True} if ride.get("walk") else {"path": ride["path"], "colour": ride["colour"], "line": ride["line"]}
               for ride in route["rides"] if len(ride["path"]) > 1]
    pieces += [{"path": walk_after, "walk": True}] if walk_after else []
    return json.dumps(pieces)


def _taxi_view(leg, place, estimate, stay, exact=True):
    return {
        "leg_id": leg.pk,
        "leg": _leg_label(leg, exact),
        "place": place.name,
        "to": f"{stay.city.city_name} centre" if estimate.get("centre") else "the hotel",
        "minutes": estimate.get("minutes"),
        "km": estimate.get("km"),
        "walk": _distance(estimate["walk_m"]) if "walk_m" in estimate else "",
    }


# ---- Getting to the day's sights ----


def stay_for_day(stays, day):
    """The hotel you set out from on a day: that night's, or else the one you check out of."""
    for stay in stays:
        if stay.check_in_date <= day < stay.check_out_date:
            return stay
    return next((stay for stay in stays if stay.check_out_date == day), None)


def activities_waiting(trip):
    """(item, stay, basis) for each placed activity whose directions are missing or out of date
    (the hotel, its station or the sight has changed since they were worked out)."""
    stays = list(trip.stays.select_related("city", "location"))
    for item in Item.objects.filter(day__trip=trip, place__isnull=False).select_related("day", "place"):
        stay = stay_for_day(stays, item.day.date)
        basis = [stay.pk, stay.location_id, (stay.metro or {}).get("station")] if stay else [None, None, None]
        basis.append(item.place_id)
        info = item.directions or {}
        if info.get("pending") or info.get("basis") != basis:
            yield item, stay, basis


def refresh_activity(item, stay, basis):
    """Work out how to get to the activity and save it as item.directions."""
    info = dict(_activity_directions(item, stay), basis=basis)
    Item.objects.filter(pk=item.pk).update(directions=info)
    item.directions = info
    return info


def _activity_directions(item, stay):
    if stay is None:
        return {"message": "No stay that night to set out from."}
    if stay.location is None:
        return {"stay": stay.pk, "message": f"Add {stay.label}'s address (or link) to work out how to get there."}
    hotel, sight = stay.location.coords, item.place.coords
    km = distance_km(hotel, sight)
    info = {"stay": stay.pk}
    if km > NEARBY_KM:
        return dict(info, message=f"About {km:.0f} km from {stay.label}: further than a metro or taxi ride.")
    if km < WALK_KM:
        return dict(info, walk_m=round(km * 1000))
    network = MetroNetwork.objects.filter(slug=(stay.metro or {}).get("network")).first()
    start = {s["slug"]: s for s in network.stations}.get((stay.metro or {}).get("station")) if network else None
    end = nearest(network, sight, HOTEL_KM) if network and start else None
    if end and end[1]["slug"] != start["slug"]:
        try:
            route = find_route(network, start["slug"], end[1]["slug"])
        except Exception as exc:
            log.warning("Metro route for activity %s failed: %s", item.pk, exc)
            route = None
        if route:
            return dict(info, network=network.slug, route=route, walk_m=round(end[0] * 1000), exit=_nearest_exit(end[1], sight))
    drive = _drive(hotel, sight)
    return dict(info, taxi=drive) if drive else dict(info, message="Couldn't work out a route there.")


def for_activities(trip, items, exact=True):
    """Attach .directions_view (metro journey, taxi time or walk from the night's hotel) to each item.
    Visitors (not exact) only get directions that set out from a public stay, without its position."""
    items = list(items)
    stays = {stay.pk: stay for stay in trip.stays.select_related("city", "location")}
    slugs = {(item.directions or {}).get("network") for item in items} - {None}
    networks = {network.slug: network for network in MetroNetwork.objects.filter(slug__in=slugs)}
    for item in items:
        item.directions_view = _activity_view(item, stays, networks, exact)
    return items


def _activity_view(item, stays, networks, exact=True):
    if not item.place:
        return None
    info = item.directions or {}
    stay = stays.get(info.get("stay"))
    if not exact and not (stay and stay.is_public):
        return None
    if not info or info.get("pending"):
        return {"message": "Working out how to get there…"}
    view = {"stay": stay.label if stay else "", "message": info.get("message", "")}
    network = networks.get(info.get("network"))
    if network and info.get("route"):
        by_slug = {s["slug"]: s for s in network.stations}
        route = info["route"]
        start, end = by_slug.get(route["from"]), by_slug.get(route["to"])
        if start and end:
            route_view = _route_view(network, route)
            hotel, sight = (stay.location.coords if stay and stay.location and exact else None), item.place.coords
            view["metro"] = {
                "from": start["name"], "to": end["name"], "route": route_view,
                "url": map_link(network, start["slug"], end["slug"])[0],
                "exit": info.get("exit", ""), "walk": _distance(info.get("walk_m")),
                "segments": _segments(
                    route_view,
                    walk_before=[hotel, [start["lat"], start["lon"]]] if hotel and start.get("lat") is not None else None,
                    walk_after=[[end["lat"], end["lon"]], sight] if end.get("lat") is not None else None,
                ),
            }
    elif info.get("taxi"):
        view["taxi"] = info["taxi"]
    elif "walk_m" in info:
        view["walk"] = _distance(info["walk_m"])
    return view


def legs_changed(trip, *dates):
    """A flight, train or boat was added, moved or removed: look again at the stays around it."""
    days = {day for day in dates if day}
    days |= {day + timedelta(days=1) for day in days}
    for stay in trip.stays.filter(Q(check_in_date__in=days) | Q(check_out_date__in=days)):
        reset(stay)
        stay.save(update_fields=["metro", "location"])


def _distance(metres):
    if metres is None:
        return ""
    rounded = max(50, round(metres / 50) * 50)
    return f"{rounded} m" if rounded < 1000 else f"{metres / 1000:.1f} km"
