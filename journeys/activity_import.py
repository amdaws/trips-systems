"""Read a sight's name and position from a web link, for the planner's activities.

- Map links (OpenStreetMap, Apple Maps, Amap, Baidu) carry the place in the address
  itself, so they're read without fetching anything. Google Maps links are refused:
  the planner uses no Google services for maps or routes.
- Booking.com, Klook, TripAdvisor and Viator links are read from the address too
  (site_links): those sites don't let servers read their pages.
- Other pages (Trip.com and TripAdvisor attraction pages, Wikipedia, a sight's own
  website) are fetched like hotel links (stay_import.fetch_page: public sites only)
  and read for schema.org place data, Wikipedia's coordinates or map meta tags.

Positions on China's offset map systems are converted (see china_coords).
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import china_coords, page_details, site_links
from .stay_import import NO_GOOGLE, PageError, _address, _enclosing_object, _first, _unescape_layers, datum, fetch_page, is_google

PLACE_TYPES = (
    "TouristAttraction", "LandmarksOrHistoricalBuildings", "Museum", "Park", "Zoo", "Aquarium", "AmusementPark",
    "PlaceOfWorship", "BuddhistTemple", "Church", "HinduTemple", "Mosque", "Landform", "Mountain", "BodyOfWater",
    "Beach", "CivicStructure", "MusicVenue", "PerformingArtsTheater", "StadiumOrArena", "ShoppingCenter", "Restaurant",
    "FoodEstablishment", "CafeOrCoffeeShop", "BarOrPub", "TouristDestination", "Place", "LocalBusiness", "Event",
    "Product", "TouristTrip",  # booking sites' tour and ticket pages (their offers give a rough price)
)
_NUMBER = r"(-?\d{1,3}\.\d+)"


def read_link(url):
    """{'name', 'address', 'description', 'lat', 'lon', 'url', 'site'} (whatever was found) for a link."""
    url = url.strip()
    if is_google(url):
        raise PageError(NO_GOOGLE)
    booked = site_links.read(url)
    if booked:
        return {key: booked[key] for key in ("name", "url", "city", "region", "country") if booked.get(key)} | {
            "site": booked["site"], "search": site_links.search_text(booked)}
    found = from_map_link(url)
    if not found:
        found = extract(fetch_page(url), url)
    found.setdefault("url", url)
    found["site"] = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
    return found


def from_map_link(url):
    """The place in a map link's address, or {} if it isn't one."""
    if not url:
        return {}
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    query = {key: values[0] for key, values in urllib.parse.parse_qs(parts.query).items()}
    lat = lon = None
    name = ""
    system = "wgs84"

    if host in ("maps.apple.com", "maps.apple"):
        spot = re.match(rf"^{_NUMBER},{_NUMBER}$", query.get("ll", "") or query.get("coordinate", ""))
        if spot:
            lat, lon = spot.group(1), spot.group(2)
        name = query.get("q", "") or query.get("name", "")
        system = "gcj02"  # Apple's map of mainland China comes from Amap
    elif host.endswith("openstreetmap.org"):
        if query.get("mlat") and query.get("mlon"):
            lat, lon = query["mlat"], query["mlon"]
        else:
            spot = re.search(rf"map=\d+/{_NUMBER}/{_NUMBER}", parts.fragment)
            if spot:
                lat, lon = spot.group(1), spot.group(2)
    elif host.endswith("amap.com"):
        spot = re.match(rf"^{_NUMBER},{_NUMBER}$", query.get("position", "") or query.get("p", ""))  # lon,lat
        if spot:
            lon, lat = spot.group(1), spot.group(2)
        name = query.get("name", "")
        system = "gcj02"
    elif host.endswith("map.baidu.com") or host == "api.map.baidu.com":
        spot = re.match(rf"^(?:latlng:)?{_NUMBER},{_NUMBER}", query.get("location", "") or query.get("latlng", ""))
        if spot:
            lat, lon = spot.group(1), spot.group(2)
        name = query.get("title", "") or query.get("name", "")
        system = "bd09"
    else:
        return {}

    found = {"name": " ".join(name.split())[:200], "source": "map link"}
    if lat is not None:
        found["lat"], found["lon"] = china_coords.to_wgs84(lat, lon, system)
    return {key: value for key, value in found.items() if value not in ("", None)} if (lat is not None or name) else {}


def _place_objects(text):
    pattern = re.compile(r'"@type"\s*:\s*\[?\s*"(%s)"' % "|".join(PLACE_TYPES))
    for match in pattern.finditer(text):
        raw = _enclosing_object(text, match.start())
        if raw:
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def extract(page, url=""):
    """A sight's details from a page's HTML. Keys are only present when found."""
    text = _unescape_layers(page)
    found = {}

    def add(key, value):
        if value and not found.get(key):
            found[key] = value.strip() if isinstance(value, str) else value

    objects = list(_place_objects(text))
    for obj in objects:
        location = obj.get("location") if isinstance(obj.get("location"), dict) else obj  # events have a location
        add("name", obj.get("name") if isinstance(obj.get("name"), str) else "")
        add("address", _address(location.get("address")))
        add("description", obj.get("description") if isinstance(obj.get("description"), str) else "")
        geo = location.get("geo") or {}
        if isinstance(geo, dict) and geo.get("latitude") and geo.get("longitude") and "lat" not in found:
            found["lat"], found["lon"] = geo["latitude"], geo["longitude"]

    # Wikipedia's page data; Trip.com's; then map meta tags.
    wiki = re.search(rf'"wgCoordinates"\s*:\s*\{{\s*"lat"\s*:\s*{_NUMBER}\s*,\s*"lon"\s*:\s*{_NUMBER}', text)
    if wiki and "lat" not in found:
        found["lat"], found["lon"] = wiki.group(1), wiki.group(2)
    lat, lng = _first(r'"lat"\s*:\s*"?(-?\d+\.\d+)', text), _first(r'"lng"\s*:\s*"?(-?\d+\.\d+)', text)
    if lat and lng and "lat" not in found:
        found["lat"], found["lon"] = lat, lng
    for lat_tag, lon_tag in (("place:location:latitude", "place:location:longitude"), ("og:latitude", "og:longitude")):
        lat = _first(rf'<meta[^>]+(?:property|name)="{lat_tag}"[^>]+content="([^"]+)"', page)
        lon = _first(rf'<meta[^>]+(?:property|name)="{lon_tag}"[^>]+content="([^"]+)"', page)
        if lat and lon and "lat" not in found:
            found["lat"], found["lon"] = lat, lon
    position = _first(r'<meta[^>]+name="(?:geo\.position|ICBM)"[^>]+content="([^"]+)"', page)
    spot = re.match(rf"\s*{_NUMBER}\s*[;,]\s*{_NUMBER}", position)
    if spot and "lat" not in found:
        found["lat"], found["lon"] = spot.group(1), spot.group(2)

    title = _first(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page) or _first(r"<title>([^<]+)</title>", page)
    add("name", re.split(r"\s+[|\-–—]\s+", title)[0] if title else "")
    add("description", _first(r'<meta[^>]+(?:name="description"|property="og:description")[^>]+content="([^"]+)"', page))

    # A planned visit's rough price, opening hours and contact details, when the page has them.
    plain = re.sub(r"<[^>]+>", " ", text)
    rough = page_details.price(objects, page, plain)
    if rough:
        add("amount", rough[0])
        add("currency", rough[1])
    add("opening_hours", page_details.opening_hours(objects, plain))
    phone, email = page_details.contact(objects, plain)
    add("phone", phone)
    add("email", email)

    if "lat" in found:
        try:
            found["lat"], found["lon"] = china_coords.to_wgs84(found["lat"], found["lon"], datum(text, url))
        except (TypeError, ValueError):
            found.pop("lat"), found.pop("lon")
    found = {k: (v[:500] if isinstance(v, str) else v) for k, v in found.items() if v not in ("", None)}
    if not any(k in found for k in ("lat", "address")):
        if len(page) < 40000 and re.search(r"challenge|captcha|verify you are human", page, re.I):
            raise PageError("The site showed a robot check instead of the page. Try an OpenStreetMap or Wikipedia link instead.")
        if not found.get("name"):
            raise PageError("Couldn't find a place on that page. Try an OpenStreetMap or Wikipedia link instead.")
    return found
