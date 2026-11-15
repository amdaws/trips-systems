"""The public Journey page and its small APIs: journey.py plans trips in Brisbane, Melbourne and
Sydney; under International routes, china_rail.py finds China's trains and, for signed-in staff,
flight_search.py finds flights. Staff can add a journey, train or flight to a trip (planner_legs.py).

Privacy: a visitor's position, and the places they search for, arrive in the body of a POST
(never in the address, so not in any access log), are used for that one answer, and are
never written to the database or a log. Replies are marked not to be stored. Each visitor
gets a fair-use allowance per minute, counted in memory under a one-way hash of their
address that's forgotten after a minute, so the free services behind the page aren't overloaded.
"""

import hashlib
import json
import logging
import math
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from core.net import client_ip

from . import china_rail, flight_search, journey, planner_legs, transit
from .http import USER_AGENT
from .models import Trip

log = logging.getLogger(__name__)

LIMITS = {"plan": 15, "nearby": 20, "places": 40, "stations": 60, "trains": 12, "airports": 60, "flights": 10, "live": 12}  # requests per visitor per minute
MAX_BODY = 4096
MAX_ADD_BODY = 600_000  # a journey's rides with their map paths
PHOTON_URL = "https://photon.komoot.io/api/"
NOT_DESTINATIONS = {"bus_stop", "platform", "stop_position", "stop"}  # clutter in a "where to?" list


def _allowed(request, bucket):
    who = hashlib.sha256(f"{settings.SECRET_KEY[:12]}|{client_ip(request) or ''}".encode()).hexdigest()[:24]
    key = f"journey:{bucket}:{who}:{int(time.time() // 60)}"
    count = cache.get_or_set(key, 0, 70)
    if count >= LIMITS[bucket]:
        return False
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, 70)
    return True


def _reply(data, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _body(request, limit=MAX_BODY):
    try:
        size = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return {}
    if size > limit or len(request.body) > limit:  # the header first, so an oversized body is never read
        return {}
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _point(value):
    if not isinstance(value, dict):
        return None
    try:
        lat, lon = float(value["lat"]), float(value["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def _failed(exc):
    """Log where it went wrong, never what was asked (the request holds the visitor's places)."""
    frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
    log.warning("Journey planner error %s at %s:%s", type(exc).__name__, frame.filename if frame else "?", frame.lineno if frame else "?")
    return _reply({"error": "Something went wrong working that out. Please try again."}, 500)


BUSY = "That's a lot of searches in a minute. Please wait a moment and try again."
MODES = [  # the page's Travel by tickboxes
    {"key": "train", "label": "Train", "title": "Trains"},
    {"key": "metro", "label": "Metro", "title": "Brisbane Metro and Sydney Metro"},
    {"key": "tram", "label": "Tram", "title": "Trams and light rail"},
    {"key": "ferry", "label": "Ferry", "title": "Ferries"},
    {"key": "bus", "label": "Bus", "title": "Buses"},
    {"key": "drive", "label": "Drive", "title": "A drive along the roads, to compare (no live traffic)"},
]


def _when(data, point):
    """(when, error): the time to leave from the page's "Leave at" (the city's own time), None for now."""
    text = str(data.get("when") or "").strip()
    slug = transit.city_for(point)
    if not text or not slug:
        return None, ""
    try:
        when = datetime.fromisoformat(text[:16]).replace(tzinfo=ZoneInfo(transit.CITIES[slug]["tz"]))
    except ValueError:
        return None, "That date and time isn't valid."
    now = timezone.now()
    if when < now - timedelta(minutes=10):
        return None, "Choose a time from now on."
    if when > now + timedelta(days=400):
        return None, "Choose a date within the next year."
    return (when if when > now + timedelta(minutes=2) else None), ""


def _rules(data):
    """The search's choices from the page: ticked modes, longest walk, step-free, avoid tolls."""
    modes = data.get("modes")
    chosen = None
    if isinstance(modes, list):
        chosen = {str(mode) for mode in modes[:10]} & (journey.TRANSIT | {"drive"})
    walk = data.get("walk")
    return journey.Rules(modes=chosen, walk_minutes=walk if isinstance(walk, int) else None,
                         step_free=data.get("step_free") is True, avoid_tolls=data.get("avoid_tolls") is True)


def _is_staff(request):
    return request.user.is_active and request.user.is_staff


def _trip_choices():
    """Trips to add a journey to: those still to come (soonest first), then the rest (latest first)."""
    today = timezone.localdate()
    trips = list(Trip.objects.filter(end_date__gte=today).order_by("start_date")) + list(Trip.objects.filter(end_date__lt=today).order_by("-start_date"))
    return [{"id": trip.pk, "title": trip.title, "start": trip.start_date.isoformat(), "end": trip.end_date.isoformat(),
             "dates": f"{trip.start_date:%-d %b} – {trip.end_date:%-d %b %Y}"} for trip in trips]


@require_GET
def journey_page(request):
    """The planner page. Visitors get no cookie from it (as /legals/ promises): the token for the
    staff-only APIs is only in the page for signed-in staff."""
    cities = [{"slug": slug, "name": city["name"], "lat": city["centre"][0], "lon": city["centre"][1], "radius_km": city["radius_km"], "tz": city["tz"]}
              for slug, city in transit.CITIES.items() if transit.network(slug)]
    credits = ", ".join(transit.CITIES[c["slug"]]["credit"] for c in cities)
    staff = _is_staff(request)
    context = {"cities": cities, "credits": credits, "trips": _trip_choices() if staff else None,
               "flight_search": staff and flight_search.enabled(), "modes": MODES, "walks": journey.WALKS}
    response = render(request, "journeys/journey.html", context)
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt  # public and read-only: no cookie, nothing stored (see journey_page)
@sensitive_variables("data", "origin", "destination")
@require_POST
def api_plan(request):
    if not _allowed(request, "plan"):
        return _reply({"error": BUSY}, 429)
    data = _body(request)
    origin, destination = _point(data.get("from")), _point(data.get("to"))
    if not origin or not destination:
        return _reply({"error": "Choose where you're starting from and where you're going."}, 400)
    when, problem = _when(data, origin)
    if problem:
        return _reply({"error": problem}, 400)
    try:
        return _reply(journey.plan(origin, destination, when, _rules(data)))
    except journey.PlanError as exc:
        return _reply({"error": str(exc)}, 422)
    except Exception as exc:
        return _failed(exc)


@csrf_exempt  # public and read-only: no cookie, nothing stored (see journey_page)
@sensitive_variables("data", "point")
@require_POST
def api_nearby(request):
    if not _allowed(request, "nearby"):
        return _reply({"error": BUSY}, 429)
    data = _body(request)
    point = _point(data.get("at"))
    if not point:
        return _reply({"error": "Choose a place to see departures near it."}, 400)
    when, problem = _when(data, point)
    if problem:
        return _reply({"error": problem}, 400)
    try:
        return _reply(journey.nearby(point, when, _rules(data)))
    except journey.PlanError as exc:
        return _reply({"error": str(exc)}, 422)
    except Exception as exc:
        return _failed(exc)


def _describe(properties):
    kind = (properties.get("osm_value") or properties.get("type") or "").replace("_", " ")
    street = " ".join(part for part in (properties.get("housenumber"), properties.get("street")) if part)
    area = properties.get("district") or properties.get("locality") or properties.get("city") or ""
    bits = [kind.capitalize() if kind and kind not in ("yes", "house") else "", street, area]
    return " · ".join(dict.fromkeys(bit for bit in bits if bit))


@csrf_exempt  # public and read-only: no cookie, nothing stored (see journey_page)
@sensitive_variables("data", "query")
@require_POST
def api_places(request):
    """Places matching what the visitor typed (parks, beaches, addresses…) from Photon
    (OpenStreetMap data): in one city when the page knows which (the other place chosen, or the
    map's), else in any of them. Only the words typed and a city's centre or the cities' area are sent."""
    if not _allowed(request, "places"):
        return _reply({"error": BUSY}, 429)
    data = _body(request)
    query = str(data.get("q", "")).strip()[:100]
    city = transit.CITIES.get(str(data.get("city", "")))
    if len(query) < 3:
        return _reply({"results": []})
    if city:
        lat, lon = city["centre"]
        reach = city["radius_km"] / 111
        params = {
            "q": query, "limit": 8, "lang": "en", "lat": lat, "lon": lon,
            "bbox": f"{lon - reach * 1.2:.3f},{lat - reach:.3f},{lon + reach * 1.2:.3f},{lat + reach:.3f}",
        }
    else:
        boxes = [(c["centre"], c["radius_km"] / 111) for c in transit.CITIES.values()]
        west, east = min(c[1] - r * 1.2 for c, r in boxes), max(c[1] + r * 1.2 for c, r in boxes)
        south, north = min(c[0] - r for c, r in boxes), max(c[0] + r for c, r in boxes)
        params = {"q": query, "limit": 15, "lang": "en", "bbox": f"{west:.3f},{south:.3f},{east:.3f},{north:.3f}"}
    try:
        request_out = urllib.request.Request(f"{PHOTON_URL}?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request_out, timeout=8) as response:
            features = json.load(response).get("features", [])
    except Exception as exc:
        log.warning("Place search failed: %s", type(exc).__name__)
        return _reply({"results": [], "error": "Place search isn't responding. Try again, or tap the map."}, 502)
    results = []
    for feature in features:
        properties, coords = feature.get("properties", {}), (feature.get("geometry") or {}).get("coordinates") or []
        if properties.get("osm_value") in NOT_DESTINATIONS:
            continue
        if len(coords) == 2 and properties.get("name") and (city or transit.city_for((coords[1], coords[0]))):
            results.append({"name": properties["name"][:100], "detail": _describe(properties)[:120], "lat": coords[1], "lon": coords[0]})
    return _reply({"results": results[:8]})


# ---------- China trains ----------


@csrf_exempt  # public and read-only: no cookie, nothing stored (see journey_page)
@require_POST
def api_china_stations(request):
    """China's stations matching what was typed (English, pinyin or Chinese), from 12306's list."""
    if not _allowed(request, "stations"):
        return _reply({"error": BUSY}, 429)
    query = str(_body(request).get("q", "")).strip()[:40]
    try:
        return _reply({"results": china_rail.find_stations(query) if query else []})
    except Exception as exc:
        return _failed(exc)


@csrf_exempt  # public and read-only: no cookie, nothing stored (see journey_page)
@require_POST
def api_china_trains(request):
    """The trains between two stations on a day, from 12306."""
    if not _allowed(request, "trains"):
        return _reply({"error": BUSY}, 429)
    data = _body(request)
    day = china_rail.parse_day(data.get("date"))
    if not day:
        return _reply({"error": "Choose the day you're travelling."}, 400)
    try:
        return _reply(china_rail.trains(str(data.get("from", ""))[:3], str(data.get("to", ""))[:3], day))
    except china_rail.RailError as exc:
        return _reply({"error": str(exc)}, 422)
    except Exception as exc:
        return _failed(exc)


# ---------- flights (staff: the flight data's allowance is small) ----------


@require_POST
def api_airports(request):
    if not _is_staff(request):
        return _reply({"error": "Sign in to search flights."}, 403)
    if not _allowed(request, "airports"):
        return _reply({"error": BUSY}, 429)
    query = str(_body(request).get("q", "")).strip()[:40]
    return _reply({"results": flight_search.find_airports(query) if len(query) >= 2 else []})


@require_POST
def api_flights(request):
    """Direct flights between two airports on a day."""
    if not _is_staff(request):
        return _reply({"error": "Sign in to search flights."}, 403)
    if not _allowed(request, "flights"):
        return _reply({"error": BUSY}, 429)
    data = _body(request)
    day = china_rail.parse_day(data.get("date"))
    if not day:
        return _reply({"error": "Choose the day you're flying."}, 400)
    try:
        return _reply(flight_search.search(str(data.get("from", ""))[:3], str(data.get("to", ""))[:3], day))
    except flight_search.FlightError as exc:
        return _reply({"error": str(exc)}, 422)
    except Exception as exc:
        return _failed(exc)


# ---------- adding to a trip (staff) ----------


@require_POST
def api_add_to_trip(request):
    """Add a planned journey's rides, or a China train, to one of the trips as private transport."""
    if not _is_staff(request):
        return _reply({"error": "Sign in to add journeys to a trip."}, 403)
    data = _body(request, MAX_ADD_BODY)
    trip = Trip.objects.filter(pk=data.get("trip") if str(data.get("trip", "")).isdigit() else None).first()
    if not trip:
        return _reply({"error": "Choose a trip."}, 400)
    try:
        if "train" in data:
            legs = [planner_legs.add_china_train(trip, data["train"])]
        elif "flight" in data:
            legs = [planner_legs.add_flight(trip, data["flight"])]
        else:
            legs = planner_legs.add_rides(trip, data.get("legs"))
    except planner_legs.AddError as exc:
        return _reply({"error": str(exc)}, 400)
    count = len(legs)
    return _reply({"added": count, "trip": trip.title, "url": reverse("manage:legs", args=[trip.pk]),
                   "message": f"Added {count} transport leg{'s' if count != 1 else ''} to {trip.title} (private until you share them)."})
