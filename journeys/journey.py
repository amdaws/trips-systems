"""The public journey planner: from one place to another by train, metro, tram, ferry and bus in
Brisbane, Melbourne and Sydney, with live times where the agencies publish them.

It reads each city's full timetable (timetable.py) and live trip updates (realtime.py), and
looks through the next three hours of departures in time order (a "connection scan") for the
earliest arrival, walking to and from stops and between nearby ones. Walks follow the streets
and paths (walking.py, from OpenStreetMap, routed on this server); where a city's map doesn't
reach, a straight-line estimate stands in. A few options are found
by setting out a little later each time. "Near me" lists the next departures from the stops
around a point.

A search can leave out modes (train, metro, tram, ferry, bus), cap the walk to and from stops,
keep to stops and services the timetables mark step-free, and add a drive along the roads
(driving.py, also worked out here) as one more option to compare. With Drive ticked and no stop
within the walk of the start, the options drive to a few nearby stations instead ("park and ride").

Privacy: the places in a request are used for that search only. Nothing here writes to the
database or a log, or sends them anywhere else.
"""

import bisect
import heapq
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from . import driving, realtime, timetable, transit, walking
from .geo import distance_km
from .models import MetroNetwork

FIRST_WINDOW = 90 * 60  # departures searched first (with buses, a city has a lot in three hours)
WINDOW = 3 * 3600  # and, if nothing arrives within the first window, this far ahead
ACCESS_M = 1200  # walk this far to a stop (further if nothing is closer)
ACCESS_FAR_M = 2000
STREET_REACH_M = 2000  # streets loaded around the start and destination
DIRECT_WALK_M = 2500  # offer walking the whole way when it's this close
CORRIDOR = (3000, 0.4, 10000)  # stops considered: within max(3 km, 40% of the distance), at most 10 km, of the straight line's box
OPTIONS = 3
SCANS = 8  # searches, each setting out a little after the last, to find the options
SLACK = 300  # an option this much later but with fewer changes is the better one
NEARBY_M = 600
INF = float("inf")
TRANSIT = set(timetable.MODES)
MODE_WORDS = {"train": "train", "metro": "metro", "tram": "tram", "ferry": "ferry", "bus": "bus"}
WALKS = (5, 10, 15, 20, 30)  # minutes a visitor can choose as their longest walk to or from a stop
PARK_SECS = 300  # parking at the station and walking to the platform
PARK_STATIONS = 6  # nearest stations tried for park and ride
PARK_REACH_KM = 60


class Rules:
    """What a search may use: which modes, the longest walk to or from a stop, step-free only."""

    def __init__(self, modes=None, walk_minutes=None, step_free=False, avoid_tolls=False):
        chosen = set(modes) if modes is not None else set(TRANSIT)  # the page always says; without, public transport
        self.transit = chosen & TRANSIT
        self.drive = "drive" in chosen
        self.walk_minutes = walk_minutes if walk_minutes in WALKS else None
        self.step_free, self.avoid_tolls = bool(step_free), bool(avoid_tolls)

    @property
    def every_mode(self):
        return self.transit == TRANSIT

    def reach_m(self):
        """(nearest stops looked for, the further reach if there are none): the old default without a cap."""
        if self.walk_minutes is None:
            return ACCESS_M, ACCESS_FAR_M
        metres = round(self.walk_minutes * 60 * walking.SPEED)
        return metres, metres

    def words(self):
        """'train or ferry' for messages."""
        names = [MODE_WORDS[m] for m in timetable.MODES if m in self.transit]
        return "public transport" if self.every_mode or not names else " or ".join(names)


class PlanError(Exception):
    """Why a journey can't be planned, in words for the visitor."""


# ---------- shared ----------

_networks = {}  # slug -> (built, network)


def _network(slug):
    """The city's network (names, colours, shapes), kept in memory until the weekly rebuild."""
    status = MetroNetwork.objects.filter(slug=slug, source=MetroNetwork.Source.TIMETABLE).values_list("status", flat=True).first()
    built = (status or {}).get("built")
    cached = _networks.get(slug)
    if cached and cached[0] == built:
        return cached[1]
    network = transit.network(slug)
    _networks[slug] = (built, network)
    return network


def _city(point):
    slug = transit.city_for(point)
    if not slug:
        raise PlanError("Journeys can be planned in Brisbane, Melbourne and Sydney.")
    city = timetable.open_city(slug)
    if city is None:
        raise PlanError(f"{transit.CITIES[slug]['name']}'s timetable is still loading. Try again in a few minutes.")
    return slug, city


def _timetable_day(slug, city, zone, day):
    """(the day whose timetable to use, whether live times apply, a note when it isn't the day itself)."""
    found = city.timetable_day(day)
    if found is None:
        raise PlanError("Choose a time from now on.")
    today = timezone.now().astimezone(zone).date()
    if found == day:
        return found, day == today, ""
    name = transit.CITIES[slug]["name"]
    return found, False, (f"{name}'s timetable for {day:%A %-d %B} isn't out yet, so these are the times on "
                          f"{found:%A %-d %B}, the same day of the week. Check again closer to the day.")


def _clock(slug, when):
    """(timezone, the service day, seconds after its midnight, that midnight as a timestamp).
    A when without a time zone is the city's own time."""
    zone = ZoneInfo(transit.CITIES[slug]["tz"])
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=zone)
    local = (when or timezone.now()).astimezone(zone)
    midnight = datetime.combine(local.date(), datetime.min.time(), zone)
    return zone, local.date(), int((local - midnight).total_seconds()), midnight.timestamp()


class _Live:
    """Live delays for one search: which trips have news, and each one's update, read from the
    feeds only when the search meets that trip. A delay counts from its stop onwards until the
    next update along the trip (as GTFS-realtime intends)."""

    _numbers = {}  # slug -> (feeds' version, {trip id: (feed, gtfs id)}), kept until the feeds change

    def __init__(self, slug, city, midnight, enabled=True):
        self.city, self.midnight = city, midnight
        # Live updates are about today's services; another day's journey is timetabled.
        self.handles, self.status = realtime.updated(slug) if enabled else ({}, {"feeds": 0, "of": 0, "age": None, "version": None})
        version = self.status.get("version")
        cached = self._numbers.get(slug)
        if cached and cached[0] == version:
            self.numbers = cached[1]
        else:
            self.numbers = city.trip_numbers(self.handles) if self.handles else {}
            self._numbers[slug] = (version, self.numbers)
        self.updates, self.carried = {}, {}

    def has(self, trip):
        return trip in self.numbers

    def _update(self, trip):
        found = self.updates.get(trip)
        if found is None:
            feed = self.numbers[trip][0]
            update = realtime.read(self.handles[self.numbers[trip]])
            by_seq, by_stop = {}, {}
            for seq, stop_id, arrival, departure, _skipped in update["stops"]:
                if seq is not None:
                    by_seq[seq] = (arrival, departure)
                stop = self.city.stop_ids.get((feed, stop_id))
                if stop:
                    by_stop[stop] = (arrival, departure)
            found = self.updates[trip] = (update["cancelled"], by_seq, sorted(by_seq), by_stop)
        return found

    def _late(self, events, leaving, scheduled):
        arrival, departure = events
        late, at = (departure if departure != (None, None) else arrival) if leaving else (arrival if arrival != (None, None) else departure)
        if late is not None:
            return late
        if at:
            return int(at - (self.midnight + scheduled))
        return None

    def adjust(self, hop, carry=True):
        """The hop at its live times, None if its trip is cancelled, unchanged without news.
        carry: hops arrive in time order, so a trip's last known delay carries on to its next stops."""
        dep, arr, trip, seq, a, b = hop
        if trip not in self.numbers:
            return hop
        cancelled, by_seq, seqs, by_stop = self._update(trip)
        if cancelled:
            return None
        here = by_seq.get(seq) or by_stop.get(a)
        late = self._late(here, True, dep) if here else None
        if late is None and carry:
            late = self.carried.get(trip)
        if late is None and seqs:
            index = bisect.bisect_right(seqs, seq) - 1
            if index >= 0:
                earlier = by_seq[seqs[index]]
                late = (earlier[1][0] if earlier[1][0] is not None else earlier[0][0])
        late = late or 0
        if carry:
            self.carried[trip] = late
        there = by_stop.get(b)
        arriving = self._late(there, False, arr) if there else None
        arriving = late if arriving is None else arriving
        return (dep + late, max(dep + late, arr + arriving), trip, seq, a, b)


class _Departures:
    """A window's departures in time order, live times applied, read from the database only as
    far as the searches get, and kept for the next search. A service running late is held back
    until its new time comes round."""

    def __init__(self, city, day, start, end, stops, live, rules=None):
        live.carried = {}
        self.source = city.stream(day, start, end, stops)
        self.live = live
        self.rows, self.held, self.done = [], [], False
        self.modes = None
        if rules and not rules.every_mode:
            self.modes = city.trip_rules()[0]
            self.allowed = {code for code, mode in enumerate(timetable.MODES, start=1) if mode in rules.transit}

    def _pull(self):
        try:
            hop = next(self.source)
        except StopIteration:
            while self.held:
                self.rows.append(heapq.heappop(self.held))
            self.done = True
            return
        while self.held and self.held[0][0] <= hop[0]:
            self.rows.append(heapq.heappop(self.held))
        if self.modes is not None and self.modes[hop[2]] not in self.allowed:
            return
        moved = self.live.adjust(hop)
        if moved is None:
            return
        if moved[0] > hop[0]:
            heapq.heappush(self.held, moved)
        else:
            self.rows.append(moved)

    def after(self, start):
        index = bisect.bisect_left(self.rows, (start,))
        while True:
            if index < len(self.rows):
                yield self.rows[index]
                index += 1
            elif self.done:
                return
            else:
                self._pull()


def _time(midnight, seconds, zone):
    return datetime.fromtimestamp(midnight + seconds, zone)


def _hhmm(midnight, seconds, zone):
    return _time(midnight, seconds, zone).strftime("%H:%M")


def _stop_label(city, network_stations, stop_id):
    """(name, platform): the station's name where the network has one, else the stop's own (a bus
    stop such as "Elizabeth St at Bathurst St, Stand A" keeps all of it)."""
    row = city.stops[stop_id]
    station = network_stations.get(row[3])
    if station:
        return station["name"], row[5]
    return re.sub(r"(?i),?\s*platform\s+\w+$", "", row[4]).strip(), row[5]


def _stop_place(city, stop_id, name):
    """Where a ride starts or ends, for adding it to a trip: the station (a Place of its own) or the stop."""
    row = city.stops[stop_id]
    return {"name": name, "station": row[3], "stop": f"{row[1]}:{row[2]}", "lat": row[6], "lon": row[7]}


# ---------- walking ----------


class _Walks:
    """Walking for one search: along the streets where the city's walking network covers it,
    else the straight-line estimate (timetable.walk_secs)."""

    def __init__(self, slug, points, reach_m):
        self.slug = slug
        found = walking.walker(slug)
        self.area = found.area(points, reach_m) if found else None
        self.spreads, self.nodes, self.changes = {}, {}, {}

    def _spread(self, point, limit_m):
        key = (point, limit_m)
        if key not in self.spreads:
            self.spreads[key] = self.area.spread(point, limit_m) if self.area else None
        return self.spreads[key]

    def to_stops(self, city, point, limit_m):
        """[(stop, seconds, metres)] for the stops within limit_m on foot. Along the streets a stop
        just across a river isn't near at all; off the network, the straight-line estimate."""
        straight = city.near(point, limit_m)
        spread = self._spread(point, limit_m + walking.SNAP_M)
        if not spread:
            return straight
        found = []
        for stop, _, _ in straight:
            row = city.stops[stop]
            reached = self.area.reach(spread, (row[6], row[7]))
            if reached and reached[0] <= limit_m:
                found.append((stop, walking.secs(reached[0]), round(reached[0])))
                self.nodes[(point, stop)] = (spread, reached[1])
        return found

    def stop_path(self, city, point, stop):
        """The walk from a point to a stop as map points, along the streets where known."""
        row = city.stops[stop]
        known = self.nodes.get((point, stop))
        if known:
            return self.area.shape(known[0], known[1], point, (row[6], row[7]))
        return [list(point), [row[6], row[7]]]

    def direct(self, origin, destination, limit_m):
        """(seconds, metres, map points) for walking the whole way, or None if it's further than limit_m."""
        spread = self._spread(origin, limit_m)
        reached = self.area.reach(spread, destination) if spread else None
        if reached:
            return walking.secs(reached[0]), reached[0], self.area.shape(spread, reached[1], origin, destination)
        metres = distance_km(origin, destination) * 1000
        if not spread and metres <= limit_m / timetable.DETOUR:
            return timetable.walk_secs(metres), metres * timetable.DETOUR, [list(origin), list(destination)]
        return None

    def between(self, a, b):
        """(metres, map points) between two stops, along the streets where known: in the streets
        already loaded around the start and destination if they cover it, else a small patch."""
        if (a, b) not in self.changes:
            self.changes[(a, b)] = self._between(a, b)
        return self.changes[(a, b)]

    def _between(self, a, b):
        found = walking.walker(self.slug)
        for area in ([self.area] if self.area else []) + ([None] if found else []):
            area = area or found.area([a, b], 700)
            spread = area.spread(a, 1000)
            reached = area.reach(spread, b) if spread else None
            if reached:
                return reached[0], area.shape(spread, reached[1], a, b)
        return distance_km(a, b) * 1000, [list(a), list(b)]


# ---------- planning ----------


def _scan(departures, access, egress, city, start, step_free=False):
    """The earliest arrival leaving at start, or None: the arrival, the stops where the journey
    joins and leaves public transport, and its steps (rides with every hop they make, and walks).
    step_free: board and get off only at stops marked step-free, on services not marked otherwise."""
    trip_access = city.trip_rules()[1] if step_free else None
    stops = city.stops
    reached, via = {}, {}
    for stop, secs, metres in access:
        reached[stop] = start + secs
        via[stop] = ("start", secs, metres)
    best, best_stop = INF, None
    for stop in reached:
        if stop in egress and reached[stop] + egress[stop][0] < best:
            best, best_stop = reached[stop] + egress[stop][0], stop
    boarded, rides = {}, {}
    for hop in departures.after(start):
        dep, arr, trip, seq, a, b = hop
        if dep > best:
            break
        if trip not in boarded:
            arrived = reached.get(a, INF)
            if arrived == INF or arrived + (timetable.CHANGE_SECS if via[a][0] == "ride" else 0) > dep:
                continue
            if step_free and (stops[a][8] != 1 or trip_access[trip] == 2):
                continue
            boarded[trip], rides[trip] = hop, []
        rides[trip].append(hop)
        if step_free and stops[b][8] != 1:
            continue  # the service goes on, but you can't get off here
        if arr < reached.get(b, INF):
            reached[b], via[b] = arr, ("ride", boarded[trip], hop)
            if b in egress and arr + egress[b][0] < best:
                best, best_stop = arr + egress[b][0], b
            for c, secs in city.walks(b):
                if arr + secs < reached.get(c, INF):
                    reached[c], via[c] = arr + secs, ("walk", b, secs)
                    if c in egress and arr + secs + egress[c][0] < best:
                        best, best_stop = arr + secs + egress[c][0], c
    if best_stop is None:
        return None
    steps, stop = [], best_stop
    while via[stop][0] != "start":
        how = via[stop]
        if how[0] == "ride":
            board, alight = how[1], how[2]
            steps.append(("ride", [hop for hop in rides[board[2]] if board[3] <= hop[3] <= alight[3]]))
            stop = board[4]
        else:
            steps.append(("walk", how[1], stop, how[2]))
            stop = how[1]
    steps.reverse()
    return {"arrive": best, "first": stop, "access": via[stop], "last": best_stop, "egress": egress[best_stop], "steps": steps}


def plan(origin, destination, when=None, rules=None):
    """Up to three ways from origin to destination ((lat, lon) each) by public transport, soonest
    arrival first, then a drive and a walk where they're asked for or close enough."""
    rules = rules or Rules()
    if not rules.transit and not rules.drive:
        raise PlanError("Tick at least one way to travel.")
    slug, city = _city(origin)
    if transit.city_for(destination) != slug:
        raise PlanError("Both places need to be in the same city.")
    zone, day, now, midnight = _clock(slug, when)
    service_day, live_today, note = _timetable_day(slug, city, zone, day)
    notes = [note] if note and rules.transit else []
    walks = _Walks(slug, [origin, destination], STREET_REACH_M)
    near_m, far_m = rules.reach_m()
    access = egress_list = []
    too_far, parked = "", {}
    if rules.transit:
        access = walks.to_stops(city, origin, near_m) or walks.to_stops(city, origin, far_m)
        egress_list = walks.to_stops(city, destination, near_m) or walks.to_stops(city, destination, far_m)
        if rules.step_free:
            notes.append(_step_free_note(slug, city))
        walk_words = f"a {rules.walk_minutes}-minute walk" if rules.walk_minutes else "walking distance"
        if not access and egress_list and rules.drive:
            # Not a station by the destination: driving there is just driving.
            access, parked = _drive_to_stations(slug, city, origin, rules, leave_out={stop for stop, _, _ in egress_list})
            if access:
                notes.append(f"No public transport stop is within {walk_words} of the start, so driving to a nearby station may suit "
                             "you better: these options drive there and park (allowing 5 minutes), or drive all the way.")
        if not (access and egress_list):
            end = "start" if not access else "destination"
            too_far = f"No public transport stop is within {walk_words} of the {end}. " + (
                "Here's the drive instead." if rules.drive else "Tick Drive to see a drive, or choose a longer walk.")
            notes.append(too_far)
    direct_m = distance_km(origin, destination) * 1000
    options = []
    live = _Live(slug, city, midnight, enabled=live_today and bool(rules.transit))
    if access and egress_list:
        egress = {stop: (secs, metres) for stop, secs, metres in egress_list}
        network = _network(slug)
        stations = {s["slug"]: s for s in network.stations} if network else {}
        margin = min(CORRIDOR[2], max(CORRIDOR[0], CORRIDOR[1] * direct_m))
        corridor = city.within(origin, destination, margin) | {stop for stop, _, _ in access}
        for window in (FIRST_WINDOW, WINDOW):
            departures = _Departures(city, service_day, now, now + window, corridor, live, rules)
            candidates, start = [], now
            for _ in range(SCANS):
                found = _scan(departures, access, egress, city, start, rules.step_free and city.has_access and city.access_share > 0)
                if not found or not found["steps"] or found["arrive"] > now + window:
                    break
                candidates.append(_describe(found, live, city, network, stations, origin, destination, midnight, zone, walks, parked))
                start = candidates[-1]["leave_secs"] + 60
                if len({option["arrive_secs"] // 60 for option in candidates}) > OPTIONS + 1:
                    break
            options = _best(candidates)
            if len(options) >= OPTIONS or window == WINDOW:
                break
    options.sort(key=lambda option: (option["arrive_secs"], option["changes"]))
    options = options[:OPTIONS]
    if rules.transit and access and egress_list and not options and rules.drive:
        within = "in the next three hours" if when is None else "in the three hours from then"
        notes.append(f"No {rules.words()} gets there {within}, so here's the drive.")
    if rules.drive:
        drive = _drive(slug, origin, destination, now, midnight, zone, rules.avoid_tolls)
        if isinstance(drive, str):
            notes.append(drive)
        else:
            options.append(drive)
    on_foot = walks.direct(origin, destination, DIRECT_WALK_M * timetable.DETOUR) if direct_m <= DIRECT_WALK_M else None
    if on_foot:
        secs, metres, path = on_foot
        options.append({
            "summary": "Walk", "kind": "walk", "leave": _hhmm(midnight, now, zone), "arrive": _hhmm(midnight, now + secs, zone), "minutes": math.ceil(secs / 60),
            "changes": 0, "walk_minutes": math.ceil(secs / 60), "live": False, "arrive_secs": now + secs, "leave_secs": now,
            "legs": [{"kind": "walk", "minutes": math.ceil(secs / 60), "metres": round(metres), "from": "Start", "to": "Destination", "path": path}],
        })
    if not options:
        if too_far:
            raise PlanError(too_far.replace("Here's the drive instead.", "There's no drive between these places either."))
        within = "in the next three hours" if when is None else f"in the three hours from {_time(midnight, now, zone):%-I:%M %p}".replace("AM", "am").replace("PM", "pm")
        raise PlanError(f"No {rules.words()} gets there {within}. Try somewhere nearer a station or stop, or tick more ways to travel.")
    for option in options:  # full times too, for a calendar entry
        option["leave_at"] = _time(midnight, option.pop("leave_secs"), zone).isoformat(timespec="minutes")
        option["arrive_at"] = _time(midnight, option.pop("arrive_secs"), zone).isoformat(timespec="minutes")
    return {"city": transit.CITIES[slug]["name"], "slug": slug, "zone": _time(midnight, now, zone).strftime("%Z"),
            "day": f"{day:%A %-d %B %Y}", "notes": [n for n in notes if n], "options": options, "live": _live_note(live.status, slug)}


def _drive_to_stations(slug, city, origin, rules, leave_out=()):
    """Park and ride, for a start too far from any stop to walk: the nearest few stations by road,
    each reached at the drive's time plus PARK_SECS. ([(stop, seconds, 0)] for the search,
    {stop: the drive}); nothing if the roads don't reach."""
    roads = driving.roads(slug)
    if roads is None:
        return [], {}
    step_free = rules.step_free and city.has_access and city.access_share > 0
    nearest = {}  # station -> (km, stop): one platform a station, the nearest
    for stop, row in city.stops.items():
        if not row[3] or stop in leave_out or (step_free and row[8] != 1):
            continue
        km = distance_km(origin, (row[6], row[7]))
        if km <= PARK_REACH_KM and (row[3] not in nearest or km < nearest[row[3]][0]):
            nearest[row[3]] = (km, stop)
    access, parked = [], {}
    for km, stop in sorted(nearest.values())[:PARK_STATIONS]:
        row = city.stops[stop]
        found = roads.route(origin, (row[6], row[7]), avoid_tolls=rules.avoid_tolls)
        if found:
            parked[stop] = found
            access.append((stop, found["secs"] + PARK_SECS, 0))
    return access, parked


def _step_free_note(slug, city):
    name = transit.CITIES[slug]["name"]
    if not city.has_access:
        return f"Step-free only takes effect for {name} with this week's timetable update; until then these routes don't check it."
    if city.access_share == 0:
        return f"{name}'s timetable doesn't say which stops are step-free, so these routes can't check it."
    if city.access_share < 0.6:
        return f"Step-free only: {name}'s timetable marks {round(city.access_share * 100)}% of stops, so routes using the rest are left out."
    return "Step-free only: using stops and services the timetable marks as accessible. Check lifts are working before you travel."


def _drive(slug, origin, destination, now, midnight, zone, avoid_tolls):
    """The drive as an option, or a note saying why there isn't one."""
    roads = driving.roads(slug)
    if roads is None:
        return f"Driving in {transit.CITIES[slug]['name']} is still being set up. Try again later."
    found = roads.route(origin, destination, avoid_tolls=avoid_tolls)
    if not found:
        return "There's no drive between these places" + (" without toll roads." if avoid_tolls else " (one of them is too far from a road).")
    secs = found["secs"]
    leg = {"kind": "drive", "minutes": math.ceil(secs / 60), "metres": found["metres"], "from": "Start", "to": "Destination",
           "path": found["path"], "steps": found["steps"], "tolls": found["tolls"],
           "depart_at": _time(midnight, now, zone).isoformat(timespec="minutes"),
           "arrive_at": _time(midnight, now + secs, zone).isoformat(timespec="minutes")}
    return {"summary": "Drive", "kind": "drive", "leave": _hhmm(midnight, now, zone), "arrive": _hhmm(midnight, now + secs, zone),
            "minutes": math.ceil(secs / 60), "changes": 0, "walk_minutes": 0, "live": False, "metres": found["metres"],
            "tolls": found["tolls"], "arrive_secs": now + secs, "leave_secs": now, "legs": [leg]}


def _best(candidates):
    """The options worth showing: drop any that leaves earlier, arrives no sooner (or only a few
    minutes sooner) and changes more often than another; then the soonest arrivals."""
    kept = []
    for option in sorted(candidates, key=lambda o: (o["changes"], -o["leave_secs"], o["arrive_secs"])):
        if any(other["leave_secs"] >= option["leave_secs"] - 60 and other["arrive_secs"] <= option["arrive_secs"] + SLACK
               and other["changes"] <= option["changes"] for other in kept):
            continue
        kept.append(option)
    return sorted(kept, key=lambda o: o["arrive_secs"])[:OPTIONS]


def _describe(found, live, city, network, stations, origin, destination, midnight, zone, walks, parked=None):
    """One option as the page shows it: when to leave and arrive, and each walk, drive and ride."""
    legs = []
    trips = city.trips({step[1][0][2] for step in found["steps"] if step[0] == "ride"})
    access_secs, access_m = found["access"][1], found["access"][2]
    first_name = _stop_label(city, stations, found["first"])[0]
    drive = (parked or {}).get(found["first"])
    if drive:
        legs.append({"kind": "drive", "minutes": math.ceil(drive["secs"] / 60), "metres": drive["metres"], "from": "Start", "to": first_name,
                     "path": drive["path"], "steps": drive["steps"], "tolls": drive["tolls"], "park": True,
                     "to_place": _stop_place(city, found["first"], first_name)})
        legs.append({"kind": "park", "minutes": math.ceil(PARK_SECS / 60), "metres": 0, "from": first_name, "to": first_name, "path": []})
    else:
        legs.append({"kind": "walk", "minutes": math.ceil(access_secs / 60), "metres": access_m, "from": "Start",
                     "to": first_name, "path": walks.stop_path(city, origin, found["first"])})
    leave = None
    for step in found["steps"]:
        if step[0] == "walk":
            a, b = city.stops[step[1]], city.stops[step[2]]
            metres, path = walks.between((a[6], a[7]), (b[6], b[7]))
            legs.append({"kind": "walk", "minutes": math.ceil(step[3] / 60), "metres": round(metres),
                         "from": _stop_label(city, stations, step[1])[0], "to": _stop_label(city, stations, step[2])[0],
                         "path": path, "change": True})
            continue
        ride = step[1]
        board, alight = ride[0], ride[-1]
        feed, trip_gtfs, line_key, headsign = trips.get(board[2], (0, "", "", ""))
        meta = city.lines.get(line_key, {})
        stop_ids = [ride[0][4]] + [hop[5] for hop in ride]
        station_slugs = [city.stops[s][3] for s in stop_ids]
        if network and all(station_slugs):
            path = transit.ride_path(network, {"key": line_key, "stops": station_slugs})
        else:
            path = [[city.stops[s][6], city.stops[s][7]] for s in stop_ids]
        from_name, platform = _stop_label(city, stations, board[4])
        if leave is None:
            leave = board[0] - access_secs
        to_name = _stop_label(city, stations, alight[5])[0]
        legs.append({
            "kind": "ride", "trip": f"{feed}:{trip_gtfs}", "line": meta.get("name", ""), "long": meta.get("long", ""),
            "colour": meta.get("colour", ""), "text": meta.get("text", ""), "mode": meta.get("mode", ""), "operator": meta.get("operator", ""),
            "headsign": headsign, "from": from_name, "platform": platform, "to": to_name,
            "depart": _hhmm(midnight, board[0], zone), "arrive": _hhmm(midnight, alight[1], zone), "stops": len(ride), "live": live.has(board[2]),
            "depart_at": _time(midnight, board[0], zone).isoformat(timespec="minutes"),
            "arrive_at": _time(midnight, alight[1], zone).isoformat(timespec="minutes"),
            "from_place": _stop_place(city, board[4], from_name), "to_place": _stop_place(city, alight[5], to_name),
            "path": path, "points": [[city.stops[s][6], city.stops[s][7]] for s in stop_ids],
        })
    legs.append({"kind": "walk", "minutes": math.ceil(found["egress"][0] / 60), "metres": found["egress"][1],
                 "from": _stop_label(city, stations, found["last"])[0], "to": "Destination",
                 "path": walks.stop_path(city, destination, found["last"])[::-1]})
    legs = [leg for leg in legs if not (leg["kind"] == "walk" and leg["metres"] < 30 and not leg.get("change"))]
    rides = [leg for leg in legs if leg["kind"] == "ride"]
    arrive = found["arrive"]
    if drive:  # the drive's clock times, now that the departure is known
        legs[0]["depart_at"] = _time(midnight, leave, zone).isoformat(timespec="minutes")
        legs[0]["arrive_at"] = _time(midnight, leave + drive["secs"], zone).isoformat(timespec="minutes")
        legs[0]["depart"], legs[0]["arrive"] = _hhmm(midnight, leave, zone), _hhmm(midnight, leave + drive["secs"], zone)
    return {
        "kind": "transit", "summary": ("Drive → " if drive else "") + " → ".join(leg["line"] or leg["mode"].title() for leg in rides),
        "drive_minutes": legs[0]["minutes"] if drive else 0,
        "leave": _hhmm(midnight, leave, zone), "arrive": _hhmm(midnight, arrive, zone),
        "minutes": math.ceil((arrive - leave) / 60), "changes": max(0, len(rides) - 1),
        "walk_minutes": sum(leg["minutes"] for leg in legs if leg["kind"] == "walk"),
        "live": any(leg["live"] for leg in rides), "legs": legs, "leave_secs": leave, "arrive_secs": arrive,
    }


def _live_note(status, slug):
    if not status["of"]:
        return {"live": False, "text": "Timetabled times."}
    if status["feeds"] == 0:
        missing = "Live times for Melbourne need the Transport Victoria feed; these are timetabled." if slug == "melbourne" and not settings.TRANSPORT_VIC_API_KEY else "Live times aren't available right now; these are timetabled."
        return {"live": False, "text": missing}
    return {"live": True, "text": "Live times" + ("" if status["feeds"] == status["of"] else " where available") + "."}


# ---------- departures near a point ----------


def nearby(point, when=None, rules=None):
    """The next departures from the stops around a point, grouped by line and direction."""
    rules = rules or Rules()
    if not rules.transit:
        raise PlanError("Tick a kind of public transport to see departures.")
    slug, city = _city(point)
    zone, day, now, midnight = _clock(slug, when)
    service_day, live_today, note = _timetable_day(slug, city, zone, day)
    modes, trip_access = city.trip_rules() if not rules.every_mode or rules.step_free else (None, None)
    allowed = {code for code, mode in enumerate(timetable.MODES, start=1) if mode in rules.transit}
    step_free = rules.step_free and city.has_access and city.access_share > 0
    walks = _Walks(slug, [point], NEARBY_M * 2)
    stops = sorted(walks.to_stops(city, point, round(NEARBY_M * timetable.DETOUR)), key=lambda stop: stop[2])[:15]
    if not stops:
        return {"city": transit.CITIES[slug]["name"], "groups": [], "live": {"live": False, "text": ""},
                "message": f"No public transport stop within {NEARBY_M} m."}
    live, hops = _Live(slug, city, midnight, enabled=live_today), []
    for stop_id, _, _ in stops:
        if step_free and city.stops[stop_id][8] != 1:
            continue
        for hop in city.hops(service_day, now - 600, now + 3600, stop=stop_id):
            if modes is not None and (modes[hop[2]] not in allowed or (step_free and trip_access[hop[2]] == 2)):
                continue
            moved = live.adjust(hop, carry=False)
            if moved:
                hops.append(moved)
    trips = city.trips({hop[2] for hop in hops})
    network = _network(slug)
    stations = {s["slug"]: s for s in network.stations} if network else {}
    distance = {stop_id: metres for stop_id, _, metres in stops}
    seconds = {stop_id: secs for stop_id, secs, _ in stops}
    groups = {}
    for dep, _, trip, _, a, _ in hops:
        if dep < now - 30 or dep > now + 3600:
            continue
        feed, _, line_key, headsign = trips.get(trip, (0, "", "", ""))
        name, platform = _stop_label(city, stations, a)
        key = (line_key, headsign, name)
        meta = city.lines.get(line_key, {})
        group = groups.setdefault(key, {
            "line": meta.get("name", ""), "colour": meta.get("colour", ""), "text": meta.get("text", ""), "mode": meta.get("mode", ""),
            "headsign": headsign, "stop": name, "platform": platform, "metres": distance[a],
            "walk_minutes": math.ceil(seconds[a] / 60), "times": [],
        })
        group["metres"] = min(group["metres"], distance[a])
        group["times"].append({"time": _hhmm(midnight, dep, zone), "minutes": max(0, math.floor((dep - now) / 60)), "live": live.has(trip)})
    for group in groups.values():
        group["times"] = sorted(group["times"], key=lambda t: t["minutes"])[:3]
    ordered = sorted(groups.values(), key=lambda g: (g["metres"] // 100, g["times"][0]["minutes"]))[:20]
    return {"city": transit.CITIES[slug]["name"], "zone": _time(midnight, now, zone).strftime("%Z"), "groups": ordered,
            "notes": [n for n in (note, _step_free_note(slug, city) if rules.step_free else "") if n], "live": _live_note(live.status, slug)}
