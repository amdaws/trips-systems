"""Driving along the roads, the planner's tickboxes (modes, longest walk, step-free) and adding a drive to a trip."""

import io
import json
import os
import zipfile
from datetime import date, time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from journeys import driving, journey, timetable, transit
from journeys.models import Leg, Place, Trip

from .test_journey import NEAR_BOWEN, NEAR_ROMA, NO_LIVE, at, point
from .test_transit import FEED, TransitDirMixin, build_brisbane


def road_map(folder, ways):
    """A tiny OpenStreetMap file: each way is ({tags}, [(lat, lon), …]); ways sharing a point join there."""
    ids, nodes, lines = {}, [], []
    for number, (tags, points) in enumerate(ways, start=1):
        refs = []
        for lat, lon in points:
            if (lat, lon) not in ids:
                ids[(lat, lon)] = len(ids) + 1
                nodes.append(f'<node id="{ids[(lat, lon)]}" version="1" lat="{lat}" lon="{lon}"/>')
            refs.append(f'<nd ref="{ids[(lat, lon)]}"/>')
        tagged = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        lines.append(f'<way id="{number}" version="1">{"".join(refs)}{tagged}</way>')
    path = os.path.join(folder, "roads.osm")
    with open(path, "w") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?><osm version="0.6">' + "".join(nodes) + "".join(lines) + "</osm>")
    return path


def line(start, end, steps=8):
    return [(round(start[0] + (end[0] - start[0]) * n / steps, 6), round(start[1] + (end[1] - start[1]) * n / steps, 6)) for n in range(steps + 1)]


# Ann Street runs east from Roma Street; at its east end a toll tunnel heads straight north to Bowen
# Hills, and a slower free road goes the long way round. Wharf Lane is one way, west to east.
WEST, EAST, NORTH, ROUND = (-27.4660, 153.0180), (-27.4660, 153.0400), (-27.4430, 153.0400), (-27.4545, 153.0550)
ROADS = [
    ({"highway": "primary", "name": "Ann Street", "maxspeed": "60"}, line(WEST, EAST)),
    ({"highway": "motorway", "name": "Clem Tunnel", "ref": "M7", "toll": "yes", "oneway": "no"}, line(EAST, NORTH)),
    ({"highway": "tertiary", "name": "Free Road"}, line(EAST, ROUND) + line(ROUND, NORTH)[1:]),
    ({"highway": "residential", "name": "Bowen Street"}, line(NORTH, (-27.4430, 153.0300), 4)),
    ({"highway": "residential", "name": "Wharf Lane", "oneway": "yes"}, line((-27.4700, 153.0180), (-27.4700, 153.0300), 4)),
    ({"highway": "residential", "name": "Car Park Road", "access": "private"}, line((-27.4600, 153.0180), (-27.4600, 153.0300), 4)),
]
START, FINISH = (-27.4662, 153.0185), (-27.4432, 153.0305)


class DrivingTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.result = driving.build("brisbane", extract=road_map(timetable.folder(), ROADS))
        self.roads = driving.roads("brisbane")

    def test_the_quickest_drive_takes_the_toll_road(self):
        found = self.roads.route(START, FINISH)
        self.assertEqual(found["tolls"], ["Clem Tunnel (M7)"])
        self.assertEqual([step["text"] for step in found["steps"]],
                         ["Head along Ann Street", "Turn left onto Clem Tunnel (M7) (toll)", "Turn left onto Bowen Street"])
        self.assertAlmostEqual(found["metres"], 2200 + 2560 + 990, delta=200)
        self.assertEqual((found["path"][0], found["path"][-1]), (list(START), list(FINISH)))

    def test_avoiding_tolls_goes_the_long_way(self):
        quick, free = self.roads.route(START, FINISH), self.roads.route(START, FINISH, avoid_tolls=True)
        self.assertEqual(free["tolls"], [])
        self.assertIn("Free Road", [step["road"] for step in free["steps"]])
        self.assertGreater(free["secs"], quick["secs"])

    def test_one_way_streets_and_private_roads(self):
        west, east = (-27.4701, 153.0181), (-27.4701, 153.0299)
        self.assertIsNotNone(self.roads.route(west, east))
        self.assertIsNone(self.roads.route(east, west))  # Wharf Lane goes one way, and nothing else joins it
        self.assertEqual(self.roads.snap((-27.4601, 153.0240)), [])  # the private road isn't in the network

    def test_places_away_from_the_road(self):
        middle_of_a_park = (-27.4560, 153.0300)  # over a kilometre from Ann Street and Bowen Street
        self.assertEqual(self.roads.snap(middle_of_a_park), [])
        found = self.roads.route(START, middle_of_a_park)
        self.assertIsNotNone(found)  # driven to the nearest road, then the rest at walking-ish pace
        self.assertIsNone(self.roads.route(START, (-27.3000, 153.3000)))  # nowhere near any road

    def test_roads_from_tiles(self):
        source = road_map(timetable.folder(), ROADS + [({"highway": "primary", "name": "Far Away Road"}, line((-31.0, 140.0), (-31.1, 140.1)))])
        everything = driving.tiles("brisbane")
        self.assertGreater(len(everything), 50)  # Brisbane's planner area reaches the Gold Coast and Sunshine Coast
        self.assertEqual(driving.fetch_due(), "brisbane")
        self.assertIsNone(driving.compose_due())  # nothing to put together until every tile is in

        def answer(slug, name, box):  # Overpass, here: the test map for the tile with Ann Street in it, empty elsewhere
            path = driving.tile_path(slug, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            south, west, north, east = box
            inside = south <= START[0] <= north and west <= START[1] <= east
            path.write_text(open(source).read() if inside else '<?xml version="1.0"?><osm version="0.6"></osm>')
            return path.stat().st_size

        for name, box in everything:
            answer("brisbane", name, box)
        self.assertEqual(driving.tiles_due("brisbane"), [])
        self.assertEqual(driving.compose_due(), "brisbane")
        self.assertEqual(driving.compose("brisbane"), len(ROADS) + 1)  # each road once (Far Away Road too: it's in the tile file)
        self.assertEqual(driving.build_due(), "brisbane")
        driving.build("brisbane")
        self.assertIsNone(driving.build_due())
        self.assertEqual(driving.roads("brisbane").route(START, FINISH)["tolls"], ["Clem Tunnel (M7)"])
        driving.mark_failed("overpass")
        self.assertIsNone(driving.fetch_due())  # failed just now: wait


def feed_with_access():
    """The test timetable with step-free marks: Roma Street and Central are, Bowen Hills isn't."""
    stops = FEED["stops.txt"].strip().splitlines()
    marks = {"place_rom": "1", "place_cen": "1", "place_bow": "2"}
    rows = [stops[0] + ",wheelchair_boarding"] + [row + "," + marks.get(row.split(",")[0], "") for row in stops[1:]]
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, text in dict(FEED, **{"stops.txt": "\n".join(rows) + "\n"}).items():
            archive.writestr(name, text)
    data.seek(0)
    return data


class TickboxTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_brisbane()
        cache.clear()
        quiet = mock.patch("journeys.realtime.updated", return_value=NO_LIVE)
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_only_the_modes_ticked(self):
        trains = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55), rules=journey.Rules(modes={"train"}))
        self.assertEqual(trains["options"][0]["legs"][1]["mode"], "train")
        with self.assertRaisesMessage(journey.PlanError, "No ferry gets there"):
            journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55), rules=journey.Rules(modes={"ferry"}))
        with self.assertRaisesMessage(journey.PlanError, "Tick at least one way"):
            journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), rules=journey.Rules(modes=set()))
        groups = journey.nearby((-27.4700, 153.0300), when=at(11, 58), rules=journey.Rules(modes={"train", "ferry"}))["groups"]
        self.assertNotIn("bus", [group["mode"] for group in groups])

    def test_the_longest_walk(self):
        far = (-27.4500, 153.0000)  # about 2 km from any stop
        with self.assertRaisesMessage(journey.PlanError, "No public transport stop is within a 5-minute walk of the start"):
            journey.plan(far, point(NEAR_BOWEN), when=at(7, 30), rules=journey.Rules(walk_minutes=5))
        self.assertTrue(journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55), rules=journey.Rules(walk_minutes=10))["options"])

    def test_step_free_only(self):
        with mock.patch("journeys.transit._download", side_effect=lambda url, key=None: feed_with_access()):
            transit.refresh("brisbane")
        city = timetable.open_city("brisbane")
        self.assertTrue(city.has_access)
        self.assertEqual(round(city.access_share, 2), 0.5)
        rules = journey.Rules(step_free=True)
        with self.assertRaisesMessage(journey.PlanError, "No public transport"):
            journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55), rules=rules)  # Bowen Hills isn't step-free
        anyway = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55))
        self.assertEqual(anyway["options"][0]["arrive"], "08:09")
        found = journey.plan(point(NEAR_ROMA), (-27.4661, 153.0262), when=at(7, 55), rules=rules)
        self.assertIn("marks 50% of stops", " ".join(found["notes"]))

    def test_without_step_free_marks(self):
        found = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55), rules=journey.Rules(step_free=True))
        self.assertIn("doesn't say which stops are step-free", " ".join(found["notes"]))
        self.assertEqual(found["options"][0]["arrive"], "08:09")  # nothing to check against, so nothing left out


class DrivePlanningTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_brisbane()
        driving.build("brisbane", extract=road_map(timetable.folder(), ROADS))
        cache.clear()
        quiet = mock.patch("journeys.realtime.updated", return_value=NO_LIVE)
        quiet.start()
        self.addCleanup(quiet.stop)

    def post(self, body):
        return self.client.post(reverse("core:journey_plan"), json.dumps(body), content_type="application/json")

    def test_a_drive_beside_public_transport(self):
        found = journey.plan(START, FINISH, when=at(7, 50), rules=journey.Rules(modes=set(journey.TRANSIT) | {"drive"}))
        kinds = [option["kind"] for option in found["options"]]
        self.assertEqual(kinds[-1], "drive")  # after the public transport options
        self.assertIn("transit", kinds)
        drive = found["options"][-1]
        self.assertEqual((drive["leave"], drive["tolls"]), ("07:50", ["Clem Tunnel (M7)"]))
        self.assertEqual(drive["legs"][0]["depart_at"][11:16], "07:50")

    def test_only_driving(self):
        with mock.patch("journeys.journey.timezone.now", return_value=at(7, 50)):
            reply = self.post({"from": {"lat": START[0], "lon": START[1]}, "to": {"lat": FINISH[0], "lon": FINISH[1]},
                               "modes": ["drive"], "avoid_tolls": True})
        options = reply.json()["options"]
        self.assertEqual([o["kind"] for o in options], ["drive"])
        self.assertEqual(options[0]["tolls"], [])

    def test_no_road_network_yet(self):
        os.remove(driving.path_for("brisbane"))
        found = journey.plan(START, FINISH, when=at(7, 50), rules=journey.Rules(modes=set(journey.TRANSIT) | {"drive"}))
        self.assertIn("still being set up", " ".join(found["notes"]))

    def test_adding_a_drive_to_a_trip(self):
        trip = Trip.objects.create(title="Brisbane weekend", slug="bne", start_date=date.today(), end_date=date.today())
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        drive = journey.plan(START, FINISH, when=at(7, 50), rules=journey.Rules(modes={"drive"}))["options"][0]["legs"][0]
        drive = dict(drive, from_place={"name": "My location", "lat": START[0], "lon": START[1]},
                     to_place={"name": "Bowen Hills station", "lat": FINISH[0], "lon": FINISH[1]})
        reply = self.client.post(reverse("core:journey_add"), json.dumps({"trip": trip.pk, "legs": [drive]}), content_type="application/json")
        self.assertEqual(reply.status_code, 200, reply.content)
        leg = Leg.objects.get()
        self.assertEqual((leg.mode, leg.depart_time, leg.is_public), (Leg.Mode.CAR, time(7, 50), False))
        self.assertEqual((leg.path_source, leg.path_status), (Leg.PathSource.DRIVE, Leg.PathStatus.OK))
        self.assertEqual(leg.destination.name, "Bowen Hills station")
        self.assertTrue(leg.origin.name.startswith("Point in"))  # "My location" isn't a name worth keeping
        self.assertIn("Toll roads: Clem Tunnel (M7)", leg.notes)
        self.assertEqual(Place.objects.filter(source="planner").count(), 2)

    def test_no_stop_nearby_drives_to_a_station(self):
        far_from_stops = (-27.4662, 153.0395)  # on Ann Street, over a kilometre from any stop
        rules = journey.Rules(modes=set(journey.TRANSIT) | {"drive"}, walk_minutes=5)
        found = journey.plan(far_from_stops, point(NEAR_BOWEN), when=at(7, 50), rules=rules)
        self.assertIn("driving to a nearby station may suit you better", " ".join(found["notes"]))
        park_and_ride = next(o for o in found["options"] if o["kind"] == "transit")
        kinds = [leg["kind"] for leg in park_and_ride["legs"]]
        self.assertEqual(kinds[:3], ["drive", "park", "ride"])
        self.assertTrue(park_and_ride["legs"][0]["park"])
        self.assertEqual(park_and_ride["legs"][0]["to"], park_and_ride["legs"][2]["from"])  # parks where the train leaves
        self.assertEqual(park_and_ride["leave"], park_and_ride["legs"][0]["depart"])
        self.assertGreater(park_and_ride["drive_minutes"], 0)
        self.assertIn("drive", [o["kind"] for o in found["options"]])  # and the drive all the way

        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        trip = Trip.objects.create(title="Brisbane weekend", slug="bne", start_date=date.today(), end_date=date.today())
        legs = [dict(leg, from_place={"name": "My location", "lat": far_from_stops[0], "lon": far_from_stops[1]}) if leg["kind"] == "drive" else leg
                for leg in park_and_ride["legs"]]
        reply = self.client.post(reverse("core:journey_add"), json.dumps({"trip": trip.pk, "legs": legs}), content_type="application/json")
        self.assertEqual(reply.status_code, 200, reply.content)
        car, train = Leg.objects.order_by("depart_time", "id")
        self.assertEqual((car.mode, train.mode), (Leg.Mode.CAR, Leg.Mode.TRAIN))
        self.assertEqual(car.destination, train.origin)  # the station's own place

    def test_no_stop_nearby_without_drive(self):
        out_east = (-27.4546, 153.0549)  # on Free Road: 2 km from Bowen Hills, too far to walk to Roma Street
        with self.assertRaisesMessage(journey.PlanError, "Tick Drive to see a drive"):
            journey.plan(out_east, point(NEAR_ROMA), when=at(8, 40), rules=journey.Rules(walk_minutes=5))
        found = journey.plan(out_east, point(NEAR_ROMA), when=at(8, 40), rules=journey.Rules(modes=set(journey.TRANSIT) | {"drive"}, walk_minutes=5))
        ride = next(o for o in found["options"] if o["kind"] == "transit")
        self.assertEqual([leg["kind"] for leg in ride["legs"]][:3], ["drive", "park", "ride"])
        self.assertEqual(ride["arrive"], "09:09")

