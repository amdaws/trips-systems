"""Turning a journey found on the Journey page into a trip's transport, for signed-in staff.

- A journey in Brisbane, Melbourne or Sydney: one leg per ride (train, metro, tram, ferry or
  bus), between the stations' own Places (or a Place for the bus stop), at the ride's times,
  drawn along the route the planner showed. A drive is one car leg between the two places.
- A China train from 12306: one train leg between the stations, matched to the OpenStreetMap
  stations already loaded where possible (else found on the map), traced along the railway by
  the background job like any other train.
- A flight from the flight search: one flight leg between the airports, its times read again
  from the departures board the search kept (not from the page), drawn like any other flight.

Legs start private, as transport added on the trip's own pages does.
"""

import math
import re
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from . import china_rail, flight_search, geocode, metro, routing, transit
from .models import Airline, Leg, Place

MODES = {"train": Leg.Mode.TRAIN, "metro": Leg.Mode.METRO, "tram": Leg.Mode.METRO, "ferry": Leg.Mode.FERRY, "bus": Leg.Mode.BUS}
STOP_KIND = {"train": Place.Kind.STATION, "metro": Place.Kind.METRO, "tram": Place.Kind.TRAM, "ferry": Place.Kind.PIER}
MAX_RIDES = 8
MAX_PATH = 5000
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class AddError(Exception):
    """Why the journey can't be added, in words for the page."""


def _text(value, most):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:most]


def _point(value):
    try:
        lat, lon = float(value["lat"]), float(value["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _moment(value):
    try:
        found = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if found.tzinfo is None or abs(found - timezone.now()) > timedelta(days=800):
        return None
    return found


def _path(value, start, end):
    """The ride's map points as sent, if they're sound; else a straight line between its ends."""
    if isinstance(value, list) and 2 <= len(value) <= MAX_PATH:
        points = [_point({"lat": p[0], "lon": p[1]}) if isinstance(p, (list, tuple)) and len(p) == 2 else None for p in value]
        if all(points):
            return [[round(lat, 6), round(lon, 6)] for lat, lon in points]
    return [list(start), list(end)]


def _stop_place(slug, mode, info, point):
    """The station's own Place (made from the timetable) where there is one, else one for the stop."""
    station = _text(info.get("station"), 60)
    if station:
        place = Place.objects.filter(source="timetable", source_id=station).first()
        if place:
            return place
    stop = _text(info.get("stop"), 50)
    source_id = f"{slug}:{stop}"[:60] if stop else ""
    if source_id:
        place = Place.objects.filter(source="gtfs", source_id=source_id).first()
        if place:
            return place
    return Place.objects.create(
        source="gtfs" if source_id else "manual", source_id=source_id, kind=STOP_KIND.get(mode, Place.Kind.OTHER),
        name=_text(info.get("name"), 200) or "Stop", lat=point[0], lon=point[1], country="AU",
        tz=transit.CITIES[slug]["tz"], city=transit._area(slug, point),
    )


def _ride(leg):
    """A ride from the planner's answer, checked: (mode, details, start, end, from, to, path)."""
    if not isinstance(leg, dict) or leg.get("kind") != "ride" or leg.get("mode") not in MODES:
        raise AddError("Only train, metro, tram, ferry and bus rides can be added.")
    a, b = leg.get("from_place"), leg.get("to_place")
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise AddError("That journey is missing its stops. Plan it again and try once more.")
    start, end = _point(a), _point(b)
    depart, arrive = _moment(leg.get("depart_at")), _moment(leg.get("arrive_at"))
    if not start or not end or not depart or not arrive or arrive < depart:
        raise AddError("That journey's stops or times aren't right. Plan it again and try once more.")
    slug = transit.city_for(start)
    if not slug or transit.city_for(end) != slug:
        raise AddError("Rides can be added in Brisbane, Melbourne and Sydney.")
    return slug, leg, start, end, a, b, depart, arrive


def _point_place(slug, info, point):
    """A Place for one end of a drive: a searched place keeps its name; your location or a tap on
    the map is named after the area."""
    area = transit._area(slug, point)
    name = _text(info.get("name"), 200)
    if not name or name in ("My location", "Point on the map"):
        name = f"Point in {area or transit.CITIES[slug]['name']}"
    place, _ = Place.objects.get_or_create(source="planner", source_id=f"{point[0]:.5f},{point[1]:.5f}", defaults={
        "kind": Place.Kind.OTHER, "name": name, "lat": point[0], "lon": point[1], "country": "AU",
        "tz": transit.CITIES[slug]["tz"], "city": area})
    return place


def add_drive(trip, leg):
    """A private car leg for a drive from the planner, drawn along the roads it showed."""
    a, b = leg.get("from_place"), leg.get("to_place")
    start, end = _point(a) if isinstance(a, dict) else None, _point(b) if isinstance(b, dict) else None
    depart, arrive = _moment(leg.get("depart_at")), _moment(leg.get("arrive_at"))
    if not start or not end or not depart or not arrive or arrive < depart:
        raise AddError("That drive's places or times aren't right. Plan it again and try once more.")
    slug = transit.city_for(start)
    if not slug or transit.city_for(end) != slug:
        raise AddError("Drives can be added in Brisbane, Melbourne and Sydney.")
    tolls = [_text(name, 80) for name in (leg.get("tolls") or [])[:6] if isinstance(name, str)]
    roads = [_text(step.get("road"), 80) for step in (leg.get("steps") or [])[:200] if isinstance(step, dict)]
    main = sorted({r for r in roads if r and r != "an unnamed road"}, key=roads.index)[:4]
    with transaction.atomic():
        origin = _point_place(slug, a, start)
        # A drive to a station (park and ride) ends at the station's own Place.
        destination = _stop_place(slug, "train", b, end) if b.get("station") or b.get("stop") else _point_place(slug, b, end)
        depart, arrive = depart.astimezone(origin.zone), arrive.astimezone(destination.zone)
        made = Leg.objects.create(
            trip=trip, mode=Leg.Mode.CAR, origin=origin, destination=destination,
            depart_date=depart.date(), depart_time=depart.time().replace(second=0, microsecond=0),
            arrive_date=arrive.date() if arrive.date() != depart.date() else None, arrive_time=arrive.time().replace(second=0, microsecond=0),
            is_public=False, status=Leg.Status.PLANNED,
            notes=(f"Via {', '.join(main)}. " if main else "") + (f"Toll roads: {', '.join(tolls)}. " if tolls else "") +
                  f"About {round(float(leg.get('metres') or 0) / 1000, 1)} km, timed for clear roads. Added from the journey planner.",
            path=_path(leg.get("path"), start, end), path_source=Leg.PathSource.DRIVE, path_status=Leg.PathStatus.OK,
            path_message=f"Drive · {round((arrive - depart).total_seconds() / 60)} min (from the journey planner)", path_updated=timezone.now(),
        )
    metro.legs_changed(trip, made.depart_date)
    return made


def add_rides(trip, legs):
    """One private transport leg per ride of a planned journey (or one car leg for a drive). Returns the legs made."""
    if not isinstance(legs, list) or not legs:
        raise AddError("Choose a journey to add.")
    drives = [leg for leg in legs if isinstance(leg, dict) and leg.get("kind") == "drive"]
    rides = [_ride(leg) for leg in legs if isinstance(leg, dict) and leg.get("kind") not in ("walk", "park", "drive")]
    if not (rides or drives) or len(rides) > MAX_RIDES or len(drives) > 1:
        raise AddError("Choose a journey to add.")
    with transaction.atomic():  # all of the journey, or none of it
        made = [add_drive(trip, leg) for leg in drives]  # driving to the station comes first
        for slug, leg, start, end, a, b, depart, arrive in rides:
            origin, destination = _stop_place(slug, leg["mode"], a, start), _stop_place(slug, leg["mode"], b, end)
            depart, arrive = depart.astimezone(origin.zone), arrive.astimezone(destination.zone)
            line, headsign, platform = _text(leg.get("line"), 60), _text(leg.get("headsign"), 100), _text(leg.get("platform"), 10)
            notes = [f"{_text(leg.get('long'), 120) or line}" + (f" towards {headsign}" if headsign else ""),
                     f"Platform {platform}" if platform else "", f"{int(leg.get('stops') or 0)} stops" if leg.get("stops") else "",
                     "Added from the journey planner."]
            path = _path(leg.get("path"), start, end)
            minutes = round((arrive - depart).total_seconds() / 60)
            made.append(Leg.objects.create(
                trip=trip, mode=MODES[leg["mode"]], number=line if len(line) <= 20 else "", operator=_text(leg.get("operator"), 100),
                origin=origin, destination=destination, depart_date=depart.date(), depart_time=depart.time().replace(second=0, microsecond=0),
                arrive_date=arrive.date() if arrive.date() != depart.date() else None, arrive_time=arrive.time().replace(second=0, microsecond=0),
                is_public=False, status=Leg.Status.PLANNED, notes=". ".join(note for note in notes if note),
                path=path, path_source=Leg.PathSource.TIMETABLE, path_status=Leg.PathStatus.OK,
                path_message=f"{line or leg['mode'].title()} · {minutes} min (from the journey planner)"[:300], path_updated=timezone.now(),
            ))
    metro.legs_changed(trip, *{leg.depart_date for leg in made})
    return made


def _china_place(found):
    """The station's Place: the OpenStreetMap one loaded for place search, else one made from the map."""
    local = found["local"]
    place = (Place.objects.filter(kind=Place.Kind.STATION, country__in=["CN", "HK"], name_local__in=[local, f"{local}站"]).order_by("id").first()
             or Place.objects.filter(source="12306", source_id=found["code"]).first())
    if place:
        return place
    hong_kong = local.startswith("香港")
    hit = {"lat": found["lat"], "lon": found["lon"]} if found.get("lat") is not None else None
    if not hit:
        try:
            results = geocode.search(f"{local}站", limit=3, country="hk" if hong_kong else "cn")
        except Exception:
            results = []
        hit = next((r for r in results if r["kind"] == Place.Kind.STATION), results[0] if results else None)
    if not hit:
        raise AddError(f"Couldn't find {found['name']} station on the map. Add this train on the trip's transport page instead.")
    return Place.objects.create(source="12306", source_id=found["code"], kind=Place.Kind.STATION, name=found["name"][:200],
                                name_local=local, country="HK" if hong_kong else "CN", lat=hit["lat"], lon=hit["lon"], city=found["city"][:120])


def add_china_train(trip, train):
    """A private train leg for a train from the China trains search."""
    if not isinstance(train, dict):
        raise AddError("Choose a train to add.")
    number = _text(train.get("number"), 8).upper()
    a, b = china_rail.station(_text(train.get("from_code"), 3)), china_rail.station(_text(train.get("to_code"), 3))
    depart, arrive = _text(train.get("depart"), 5), _text(train.get("arrive"), 5)
    day, shown = china_rail.parse_day(train.get("day")), china_rail.parse_day(train.get("shown"))
    days = train.get("days") if isinstance(train.get("days"), int) else -1
    if not (china_rail.NUMBER.match(number) and a and b and HHMM.match(depart) and HHMM.match(arrive) and day and 0 <= days <= 3):
        raise AddError("That train's details aren't right. Search again and try once more.")
    origin, destination = _china_place(a), _china_place(b)
    notes = [f"{a['name']} {a['local']} → {b['name']} {b['local']}.", "Times from 12306 (China Railway)"]
    if shown and shown != day:
        notes[-1] += (f" for {shown:%A %-d %B}, the same day of the week, as tickets for {day:%-d %B} weren't on sale yet: "
                      f"check them when sales open on {china_rail.sale_day(day):%-d %B}.")
    else:
        notes[-1] += "."
    with transaction.atomic():
        leg = Leg.objects.create(
            trip=trip, mode=Leg.Mode.TRAIN, number=number, operator="China Railway", origin=origin, destination=destination,
            depart_date=day, depart_time=time.fromisoformat(depart), arrive_date=day + timedelta(days=days) if days else None,
            arrive_time=time.fromisoformat(arrive), is_public=False, status=Leg.Status.PLANNED,
            notes=" ".join(notes) + " Added from the journey planner.",
        )
    routing.refresh_path(leg, allow_network=False)  # traced along the railway by the background job
    metro.legs_changed(trip, leg.depart_date, leg.arrive_date)
    return leg


def add_flight(trip, wanted):
    """A private flight leg for a flight from the flight search."""
    if not isinstance(wanted, dict):
        raise AddError("Choose a flight to add.")
    number = re.sub(r"\s+", "", _text(wanted.get("number"), 10)).upper()
    day = china_rail.parse_day(wanted.get("day"))
    origin, destination = flight_search.airport(wanted.get("from")), flight_search.airport(wanted.get("to"))
    if not (flight_search.NUMBER.match(number) and day and origin and destination):
        raise AddError("That flight's details aren't right. Search again and try once more.")
    try:
        found = flight_search.find(origin.code, destination.code, day, number)
    except flight_search.FlightError as exc:
        raise AddError(str(exc))
    if not found:
        raise AddError(f"{number} isn't on {origin.code}'s departures that day any more. Search again and try once more.")
    depart_day, arrive_day = china_rail.parse_day(found["depart_day"]), china_rail.parse_day(found["arrive_day"])
    airline = found["airline"] or (Airline.objects.filter(code=number[:2]).values_list("name", flat=True).first() or "")
    details = [f"Aircraft: {found['aircraft']}" if found["aircraft"] else "",
               f"Terminal {found['terminal']}" if found["terminal"] else "",
               f"arrives at terminal {found['arrive_terminal']}" if found["arrive_terminal"] else ""]
    with transaction.atomic():
        leg = Leg.objects.create(
            trip=trip, mode=Leg.Mode.FLIGHT, number=number, operator=airline[:100], origin=origin, destination=destination,
            depart_date=depart_day, depart_time=time.fromisoformat(found["depart"]),
            arrive_date=arrive_day if arrive_day != depart_day else None, arrive_time=time.fromisoformat(found["arrive"]),
            is_public=False, status=Leg.Status.PLANNED,
            notes=" · ".join(d for d in details if d) + (". " if any(details) else "") + "Times from AeroDataBox's schedule. Added from the journey planner.",
        )
    routing.refresh_path(leg, allow_network=False)  # an arc now; the real track later, as for other flights
    metro.legs_changed(trip, leg.depart_date, leg.arrive_date)
    return leg
