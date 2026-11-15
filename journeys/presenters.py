"""Turn a trip into what the pages show: the day-by-day plan, map data, stops, stays and activities.

Who's looking decides what's shown. Transport, stays and activities are each Public or Private.
- Visitors (full=False) see public items only. Flights and trains show the airline or railway
  and times rounded up to the quarter hour, never the number; stays show the city and dates,
  never the hotel; metro and taxi directions are fine (no hotel position). A public journey that
  starts or ends at a hotel (a taxi to the airport, say) reads "Hotel" in its city and isn't drawn
  on their map.
- The secret link, or the owner signed in (full=True), see public and private items with exact
  times, numbers and hotel names, but no booking references, costs or private notes.
- The planner (private=True) sees everything, with edit links.
"""

import copy
import math
from collections import defaultdict
from datetime import datetime, time, timedelta

from .models import BLOCK_ORDER, Block, Item, Leg, Place
from . import metro, nights, transit, weather

MODE_LABELS = dict(Leg.Mode.choices)
BLOCK_LABELS = {
    Block.NIGHT: "Night",
    Block.MORNING: "Morning",
    Block.AFTERNOON: "Afternoon",
    Block.EVENING: "Evening",
}
BLOCK_HOURS = {Block.NIGHT: "00–06", Block.MORNING: "06–12", Block.AFTERNOON: "12–18", Block.EVENING: "18–24"}
ROUGH_MODES = (Leg.Mode.FLIGHT, Leg.Mode.TRAIN)  # shown with rounded times and no number to visitors


def date_label(trip, value, full, fmt="%a %-d %b %Y"):
    if trip.hide_dates and not full:
        return f"Day {(value - trip.start_date).days + 1}"
    return value.strftime(fmt)


def legs_for(trip, full=True):
    legs = trip.legs.select_related("origin", "destination").order_by("depart_date", "depart_time", "id")
    return [show_leg(leg, full) for leg in (legs if full else legs.filter(is_public=True))]


def _quarter_up(moment):
    return moment + timedelta(minutes=(-moment.minute) % 15)


def _duration(minutes):
    hours, minutes = divmod(minutes, 60)
    return " ".join(filter(None, [f"{hours} h" if hours else "", f"{minutes} min" if minutes else ""]))


def visitor_place(place):
    """A place as visitors see it at the end of a journey: a hotel becomes "Hotel" in its city, without
    its name (a stand-in copy: the leg keeps pointing at the same place and is never saved from here)."""
    if place.kind != Place.Kind.HOTEL:
        return place
    hidden = copy.copy(place)
    hidden.name, hidden.name_local, hidden.aliases = "Hotel", "", ""  # city_name is then its city, else "Hotel"
    return hidden


def show_leg(leg, exact):
    """Set what this viewer sees of a leg's times, number, ends and journey time (see the notes above)."""
    leg.hides_hotel = not exact and Place.Kind.HOTEL in (leg.origin.kind, leg.destination.kind)
    if leg.hides_hotel:
        leg.origin, leg.destination = visitor_place(leg.origin), visitor_place(leg.destination)
    leg.rough = not exact and leg.mode in ROUGH_MODES
    depart = leg.departs_at if leg.depart_time else None
    arrive = leg.arrives_at
    if leg.rough:
        depart, arrive = depart and _quarter_up(depart), arrive and _quarter_up(arrive)
    leg.shown_departs_at, leg.shown_arrives_at = depart, arrive
    leg.shown_depart = f"{depart:%H:%M}" if depart else ""
    leg.shown_arrive = f"{arrive:%H:%M}" if arrive else ""
    # Against the day it's listed on: a departure at 23:50 rounded up to ~00:00 still arrives "+1".
    leg.shown_next_day = bool(depart and arrive and arrive.date() > leg.depart_date)
    leg.shown_number = leg.number if exact else ""
    leg.shown_duration = ""
    if leg.depart_time and arrive and leg.arrives_at > leg.departs_at:
        minutes = (leg.arrives_at - leg.departs_at).total_seconds() / 60
        leg.shown_duration = _duration(math.ceil(minutes / 15) * 15 if leg.rough else round(minutes))
    return leg


def timeline(trip, private=False, full=False, activities=False):
    """Days in order, each with its blocks of transport (and, with activities=True, the day's
    activities). Each flight, train or boat gets leg.transfers: the metro or taxi to the hotel
    after it and from the hotel before it, for the stays this viewer may see."""
    full = full or private
    moves = metro.transfers_by_leg(trip, public_only=not full, exact=full)
    legs_by_date = defaultdict(list)
    for leg in legs_for(trip, full):
        leg.transfers = moves.get(leg.pk)
        legs_by_date[leg.depart_date].append(leg)

    days, all_items = [], []
    night_names = nights.night_names(trip)  # where each night is spent, from the stays
    for day in trip.days.prefetch_related("items__place").order_by("date"):
        entries = defaultdict(list)
        for leg in legs_by_date.pop(day.date, []):
            entries[leg.block].append(("leg", leg.sort_key, leg))
        if activities:
            for item in day.items.all():
                if full or item.is_public:
                    entries[item.block].append(("item", item.sort_key, item))
                    all_items.append(item)
        blocks = [
            {
                "key": block,
                "label": BLOCK_LABELS[block],
                "hours": BLOCK_HOURS[block],
                "entries": [{"type": kind, "obj": obj} for kind, _key, obj in sorted(entries[block], key=lambda e: e[1])],
            }
            for block in sorted(entries, key=lambda b: BLOCK_ORDER[b])
        ]
        days.append({
            "day": day,
            "number": day.number,
            "label": date_label(trip, day.date, full),
            "short_label": date_label(trip, day.date, full, "%a %-d %b"),
            "blocks": blocks,
            "night": night_names.get(day.date),
        })
    if all_items:
        metro.for_activities(trip, all_items, exact=full)  # how to get there from the night's hotel
    return days


MODE_GROUPS = {Leg.Mode.FLIGHT: "flight", Leg.Mode.TRAIN: "rail"}  # anything else: "road" (boats, buses, cars…)
MODE_WORDS = {
    "flight": ("flight", "flights"), "hsr": ("high-speed train", "high-speed trains"), "train": ("train", "trains"),
    "boat": ("boat", "boats"), "ferry": ("ferry", "ferries"), "bus": ("bus", "buses"), "shuttle": ("shuttle", "shuttles"),
    "car": ("car", "cars"), "metro": ("metro ride", "metro rides"), "walk": ("walk", "walks"), "other": ("transfer", "transfers"),
}


def _mode_summary(legs):
    """'2 flights', 'Car, 2 flights' or 'High-speed train, flight' for a day's journeys."""
    counts = {}
    for leg in legs:
        counts[leg.mode_class] = counts.get(leg.mode_class, 0) + 1
    if len(counts) == 1:
        (kind, count), = counts.items()
        return f"{count} {MODE_WORDS[kind][count != 1]}"
    parts = [MODE_WORDS[kind][0] if count == 1 else f"{count} {MODE_WORDS[kind][1]}" for kind, count in counts.items()]
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def _in_travel_order(legs):
    """Timed journeys by time; one without a time goes after the journey that ends where it starts
    (a shuttle from the station the train arrives at), else first."""
    ordered = [leg for leg in legs if leg.depart_time]
    for leg in (leg for leg in legs if not leg.depart_time):
        after = next((i for i, other in enumerate(ordered) if other.destination.city_name == leg.origin.city_name), None)
        ordered.insert(0 if after is None else after + 1, leg)
    return ordered


def _moves_text(transfers):
    """'Metro from Taipei · Metro to Hong Kong 1': the hotel transfers around a journey, in a line."""
    parts = []
    for slot, word in (("before", "from"), ("after", "to")):
        for move in (transfers or {}).get(slot, []):
            kind = "Metro" if move["metro"] else "Walk" if move["taxi"] and move["taxi"].get("walk") else "Taxi"
            parts.append(f"{kind} {word} {move['stay']}")
    return " · ".join(parts)


def itinerary(trip, full=False, stop_list=None):
    """The trip page's day list and journeys. Rows are travel days (with their journeys) and, between
    them, a band for each run of quiet days ("2–4 · Taipei · 4 nights"). Journeys carry what the
    details panel shows: the times and numbers this viewer may see, transfers and fare links."""
    stop_on = {}
    for stop in stop_list if stop_list is not None else stops(trip):
        for night in range(stop["nights"]):
            stop_on[stop["first"] + timedelta(days=night)] = stop
    rows, quiet, journeys = [], [], []

    def close_band():
        if not quiet:
            return
        first, last = quiet[0]["number"], quiet[-1]["number"]
        places = list(dict.fromkeys(day["night"]["name"] for day in quiet if day["night"]))
        text = ", ".join(places) if places else "No travel"
        stop = stop_on.get(quiet[0]["day"].date)
        if len(places) == 1 and stop:
            text += f" · {stop['nights']} night{'s' if stop['nights'] != 1 else ''}"
        rows.append({"band": True, "range": f"{first}–{last}" if last != first else str(first), "text": text})
        quiet.clear()

    for day in timeline(trip, full=full):
        legs = _in_travel_order([entry["obj"] for block in day["blocks"] for entry in block["entries"] if entry["type"] == "leg"])
        if not legs:
            quiet.append(day)
            continue
        close_band()
        cities = []
        for leg in legs:
            leg.group = MODE_GROUPS.get(leg.mode, "road")
            leg.moves = _moves_text(leg.transfers)
            leg.day_number, leg.day_label = day["number"], day["short_label"]
            journeys.append(leg)
            for city in (leg.origin.city_name, leg.destination.city_name):
                if not cities or cities[-1] != city:
                    cities.append(city)
        rows.append({
            "band": False, "number": day["number"], "date": day["day"].date, "hide_date": trip.hide_dates and not full,
            "title": " → ".join(cities), "summary": _mode_summary(legs), "legs": legs,
            "kinds": list(dict.fromkeys(leg.mode_class for leg in legs)),
            "groups": " ".join(sorted({leg.group for leg in legs})),
        })
    close_band()
    return rows, journeys


def stay_list(trip, full=False):
    """The stays this viewer may see, with their metro and taxi transfers (stay.metro_view)."""
    stays = trip.stays.select_related("city", "location")
    stays = metro.for_stays(trip, stays if full else stays.filter(is_public=True), exact=full)
    for stay in stays:
        stay.day_number = (stay.check_in_date - trip.start_date).days + 1
    return stays


def weather_text(entry):
    """'Typical 15–19 °C · rain 42% of days', or '' without weather."""
    found = (entry.data or {}).get("summary") if entry else None
    if not found:
        return ""
    text = f"{'Forecast' if entry.kind == 'forecast' else 'Typical'} {found['min']}–{found['max']} °C"
    return text + (f" · rain {found['rain_share']}% of days" if found.get("rain_share") is not None else "")


def stay_extras(trip, stays, full=False):
    """Add to each stay what the Stays pages show beside it: the trip days it covers (first_day,
    last_day), the weather for its city on those nights, whether its check-in and check-out times
    may be shown, and the activities planned on its days (only the public ones for visitors)."""
    weather_for = {}
    for stop in stops(trip):
        for stay in stop["stays"]:
            weather_for[stay.pk] = stop["weather"]
    items = Item.objects.filter(day__trip=trip).select_related("day").order_by("day__date", "order", "start_time", "id")
    items = list(items if full else items.filter(is_public=True))
    for stay in stays:
        stay.first_day = (stay.check_in_date - trip.start_date).days + 1
        stay.last_day = (stay.check_out_date - trip.start_date).days + 1
        stay.weather = weather_for.get(stay.pk)
        stay.weather_text = weather_text(stay.weather)
        stay.show_times = full or stay.is_public
        stay.planned = [item for item in items if stay.check_in_date <= item.day.date < stay.check_out_date]
    return stays


def stay_cards(trip, full=False):
    """The public Stays page: every stay this viewer may see in full (with its metro or taxi
    transfers), and for visitors the private stays too, with only their city, days and weather
    (never the hotel, its station or the way there)."""
    shown = {stay.pk: stay for stay in stay_list(trip, full)}
    cards = []
    for stay in trip.stays.select_related("city", "location").order_by("check_in_date", "id"):
        card = shown.get(stay.pk) or stay
        card.city_only = stay.pk not in shown
        card.city_label, card.city_local = nights.city_of(stay)[:2]
        cards.append(card)
    return stay_extras(trip, cards, full)


def activity_days(trip, full=False):
    """The activities this viewer may see, by day (with the city that night's stay is in), with
    directions from that night's hotel."""
    items = Item.objects.filter(day__trip=trip).select_related("day", "place").order_by("day__date", "order", "start_time", "id")
    items = metro.for_activities(trip, items if full else items.filter(is_public=True), exact=full)
    night_names = nights.night_names(trip)
    days = []
    for item in items:
        if not days or days[-1]["day"] != item.day:
            days.append({"day": item.day, "label": date_label(trip, item.day.date, full, "%a %-d %b"),
                         "night": night_names.get(item.day.date), "items": []})
        days[-1]["items"].append(item)
    return days


def stops(trip):
    """Overnight stops in order, from the stays: consecutive nights in one city become one stop,
    named after the city (every viewer sees it, never the hotel), with weather where there's a city."""
    result = []
    for index, stop in enumerate(nights.stops(trip), start=1):
        place, first, last = stop["place"], stop["first"], stop["last"]
        result.append({
            "index": index,
            "name": stop["name"],
            "local": stop["local"],
            "place": place,
            "first": first,
            "last": last,
            "nights": (last - first).days + 1,
            "weather": weather.for_stop(place, first, last) if place else None,
            "stays": stop["stays"],
        })
    return result


def leg_times(leg):
    """'00:50–07:20', or 'about 01:00–07:30' for visitors (call show_leg first)."""
    if not leg.shown_depart:
        return ""
    text = ("about " if leg.rough else "") + leg.shown_depart
    if leg.shown_arrive:
        text += "–" + leg.shown_arrive + (" (+1)" if leg.shown_next_day else "")
    return text


def map_data(trip, private=False, full=False):
    """JSON for the trip map: the legs this viewer may see with their drawn paths, the overnight
    stops, and the metro routes to and from the stays they may see (hotels pinned for full viewers)."""
    full = full or private
    legs = []
    for leg in legs_for(trip, full):
        if leg.hides_hotel:
            continue  # its path would start or end at the hotel
        entry = {
            "id": leg.pk,
            "mode": leg.mode,
            "mode_label": leg.mode_label,
            "high_speed": leg.is_high_speed,
            "group": MODE_GROUPS.get(leg.mode, "road"),
            "path": leg.path or [leg.origin.coords, leg.destination.coords],
            "approx": leg.path_status != Leg.PathStatus.OK,
            "from": leg.origin.name,
            "from_local": leg.origin.name_local,
            "from_pinyin": leg.origin.pinyin,
            "from_city": leg.origin.city_name,
            "to": leg.destination.name,
            "to_local": leg.destination.name_local,
            "to_pinyin": leg.destination.pinyin,
            "to_city": leg.destination.city_name,
            "day": (leg.depart_date - trip.start_date).days + 1,
            "date": date_label(trip, leg.depart_date, full, "%a %-d %b"),
            "times": leg_times(leg),
            "duration": ("about " if leg.rough else "") + leg.shown_duration if leg.shown_duration else "",
            "operator": leg.operator,
            "note": leg.public_note,
        }
        if full:
            entry["number"] = leg.number
        if private:
            entry.update({"status": leg.get_status_display(), "seat": leg.seat_class})
        legs.append(entry)

    stop_list, pins = [], hotel_pins(trip, full)
    placed = {pin["id"] for pin in pins if pin.get("lat") is not None}
    stop_of = {}
    for stop in stops(trip):
        for stay in stop["stays"]:
            stop_of[stay.pk] = stop
    for pin in pins:  # full viewers: the hotel pin carries its city's number and name, instead of a marker of its own
        stop = stop_of.get(pin["id"])
        if stop and pin.get("lat") is not None:
            pin.update({"index": stop["index"], "city": stop["name"], "city_local": stop["local"], "nights": stop["nights"],
                        "weather": weather_text(stop["weather"])})
    for stop in stops(trip):
        place = stop["place"]
        if not place:
            continue  # a stay without a city: the hotel's own position is never a map pin for its city
        if any(stay.pk in placed for stay in stop["stays"]):
            continue  # labelled on the hotel pin
        stop_list.append({
            "index": stop["index"],
            "name": stop["name"],
            "local": stop["local"],
            "lat": place.lat,
            "lon": place.lon,
            "nights": stop["nights"],
            "dates": (
                date_label(trip, stop["first"], full, "%-d %b")
                + ("" if stop["first"] == stop["last"] else " – " + date_label(trip, stop["last"] + timedelta(days=1), full, "%-d %b"))
            ),
            "days": [(stop["first"] - trip.start_date).days + 1 + i for i in range(stop["nights"])],
        })
    return {"legs": legs, "stops": stop_list, "stays": pins, "lines": transit.map_lines(timetable_networks(trip, full))}


def timetable_networks(trip, full=True):
    """Brisbane, Melbourne or Sydney, where the trip stays or travels on the ground (not just flies).
    Every viewer sees each night's city, so all stays count; visitors' journeys are the public ones."""
    points = [stay.city.coords for stay in trip.stays.select_related("city") if stay.city]
    legs = trip.legs.exclude(mode=Leg.Mode.FLIGHT)
    for leg in (legs if full else legs.filter(is_public=True)).select_related("origin", "destination"):
        points += [leg.origin.coords, leg.destination.coords]
    return transit.networks_near(points)


def hotel_pins(trip, full=True):
    """Each stay this viewer may see, with its nearest metro station and metro routes. Full viewers
    also get the hotel itself (name, address and map position); visitors never do."""
    pins = []
    for stay in stay_list(trip, full):
        view = stay.metro_view or {}
        pin = {
            "id": stay.pk,
            "label": stay.label,  # never the hotel's name: Stay.save() sees to that
            "public": stay.is_public,
            "dates": date_label(trip, stay.check_in_date, full, "%-d %b") + " – " + date_label(trip, stay.check_out_date, full, "%-d %b"),
            "days": [(stay.check_in_date - trip.start_date).days + 1 + i for i in range(stay.nights)],
            "metro": view.get("station"),
            "arrival": view.get("arrival"),
            "departure": view.get("departure"),
            "network": view.get("network", ""),
            "map_url": view.get("map_url", ""),
            "map_label": view.get("map_label", ""),
        }
        if full and stay.location:
            pin.update({"name": stay.name, "address": stay.address, "lat": stay.location.lat, "lon": stay.location.lon})
        elif not (pin["metro"] or pin["arrival"] or pin["departure"]):
            continue
        pins.append(pin)
    return pins


def summary(trip, full=True):
    legs = legs_for(trip, full)
    by_mode = defaultdict(int)
    for leg in legs:
        by_mode[leg.mode] += 1
    cities = {stop["place"].pk if stop["place"] else stop["name"].lower() for stop in stops(trip)}  # stays without a city count by name
    return {
        "days": trip.duration_days,
        "cities": len(cities),
        "flights": by_mode[Leg.Mode.FLIGHT],
        "trains": by_mode[Leg.Mode.TRAIN],
        "other_legs": sum(n for mode, n in by_mode.items() if mode not in (Leg.Mode.FLIGHT, Leg.Mode.TRAIN)),
    }


def countdown(trip, full, now=None):
    """The countdown above the itinerary: the time left until it ends (by default the first
    departure), then which day of the trip it is, and nothing once the trip is over. Visitors
    don't get it on trips that hide their dates: it would give the date away."""
    from django.utils import timezone

    if not trip.show_countdown or (trip.hide_dates and not full):
        return None
    now = now or timezone.now()
    zone, target = trip.countdown_zone(public_only=not full), trip.countdown_target(public_only=not full)
    rough = not full and not trip.countdown_to  # visitors see departures rounded up to the quarter hour, as on the itinerary
    if rough:
        target = _quarter_up(target.astimezone(zone)).replace(second=0, microsecond=0)
    end = datetime.combine(trip.end_date + timedelta(days=1), time(0), tzinfo=zone)
    if now >= end:
        return None
    if now >= target:
        day = (now.astimezone(zone).date() - trip.start_date).days + 1
        return {"underway": True, "day": max(1, min(day, trip.duration_days)), "of": trip.duration_days}
    left = int((target - now).total_seconds())
    first = trip.first_departure(public_only=not full)
    local = target.astimezone(zone)
    return {
        "target": target.isoformat(), "days": left // 86400, "hours": left % 86400 // 3600, "minutes": left % 3600 // 60, "seconds": left % 60,
        "label": trip.countdown_label or "until the trip starts",
        "when": local.strftime("%a %-d %b %Y, ") + ("about " if rough else "") + local.strftime("%H:%M")
                + (f" {(first.origin.city_name if full else first.origin.visitor_city_name)} time" if first else f" {local:%Z}"),
    }
