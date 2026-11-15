import os
from datetime import date, datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from journeys import journey, timetable, walking
from journeys.geo import distance_km

from .test_transit import TransitDirMixin, build_brisbane

NO_LIVE = ({}, {"feeds": 1, "of": 1, "age": 0, "version": ()})


def street_map(folder, ways):
    """A tiny OpenStreetMap file: each way is (highway, [(lat, lon), …]); ways sharing a point join there."""
    ids, nodes, lines = {}, [], []
    for number, (highway, points) in enumerate(ways, start=1):
        refs = []
        for lat, lon in points:
            if (lat, lon) not in ids:
                ids[(lat, lon)] = len(ids) + 1
                nodes.append(f'<node id="{ids[(lat, lon)]}" version="1" lat="{lat}" lon="{lon}"/>')
            refs.append(f'<nd ref="{ids[(lat, lon)]}"/>')
        lines.append(f'<way id="{number}" version="1">{"".join(refs)}<tag k="highway" v="{highway}"/></way>')
    path = os.path.join(folder, "map.osm")
    with open(path, "w") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?><osm version="0.6">' + "".join(nodes) + "".join(lines) + "</osm>")
    return path


def along(lat, west, east, steps=10):
    return [(lat, round(west + (east - west) * n / steps, 6)) for n in range(steps + 1)]


# A river between two streets: the north bank at -27.4661 (Central, Riverside), the south bank at
# -27.4700, joined only by a bridge far to the east. A two-point path on its own is an island.
NORTH = along(-27.4661, 153.0180, 153.0400, 22)
SOUTH = along(-27.4700, 153.0180, 153.0400, 22)
BRIDGE = [(-27.4661, 153.0400), (-27.4680, 153.0400), (-27.4700, 153.0400)]
ISLAND = [(-27.4800, 153.0100), (-27.4801, 153.0101)]


class WalkingTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.map = street_map(timetable.folder(), [("residential", NORTH), ("residential", SOUTH), ("footway", BRIDGE), ("path", ISLAND)])
        self.result = walking.build("brisbane", extract=self.map)
        self.walker = walking.walker("brisbane")

    def test_network_is_built_without_islands(self):
        self.assertEqual(self.result["nodes"], len(NORTH) + len(SOUTH) + 1)  # the bridge's middle point; the island is gone
        area = self.walker.area([ISLAND[0]], 300)
        self.assertEqual(area.snap(ISLAND[0]), [])

    def test_walks_go_round_by_the_streets(self):
        north, south = (-27.4662, 153.0220), (-27.4699, 153.0220)
        area = self.walker.area([north, south], 3000)
        spread = area.spread(north, 6000)
        metres, node = area.reach(spread, south)
        self.assertLess(distance_km(north, south) * 1000, 450)  # just across the river…
        self.assertGreater(metres, 4000)  # …but the bridge is 2 km away each way
        path = area.shape(spread, node, north, south)
        self.assertIn([-27.468, 153.04], path)  # over the bridge
        self.assertEqual((path[0], path[-1]), (list(north), list(south)))
        self.assertIsNone(area.reach(area.spread(north, 1000), south))  # not within a short walk at all

    def test_due_only_for_cities_with_timetables(self):
        self.assertIsNone(walking.due())  # Brisbane is built; the others have no timetable here
        build_brisbane()
        os.utime(walking.path_for("brisbane"), (0, 0))  # a month old (and more)
        self.assertEqual(walking.due(), "brisbane")
        walking.mark_failed("brisbane")
        self.assertIsNone(walking.due())  # tried and failed just now: wait


class StreetPlanningTests(TransitDirMixin, TestCase):
    """The journey planner with a walking network: Central and Riverside (270 m apart in a straight
    line) are on opposite banks here, so the train-then-ferry journey can't change between them."""

    def setUp(self):
        super().setUp()
        quiet = mock.patch("journeys.realtime.updated", return_value=NO_LIVE)
        quiet.start()
        self.addCleanup(quiet.stop)
        west = along(-27.4661, 153.0150, 153.0270, 12)  # Roma Street to Central
        east = along(-27.4661, 153.0285, 153.0320, 5)  # Riverside, across the water, its own bank
        roma_street = along(-27.4656, 153.0150, 153.0230, 8) + [(-27.4661, 153.0230)]
        walking.build("brisbane", extract=street_map(timetable.folder(), [
            ("residential", west), ("residential", roma_street), ("residential", east + along(-27.4700, 153.0320, 153.0320, 1)),
            ("footway", along(-27.4700, 153.0200, 153.0320, 12)), ("footway", [(-27.4700, 153.0200), (-27.4790, 153.0230)]),
        ]))
        build_brisbane()  # its walks between stops follow these streets
        self.city = timetable.open_city("brisbane")
        self.stop = {row[4]: number for number, row in self.city.stops.items()}

    def test_no_walk_across_the_river(self):
        central = [s for s, row in self.city.stops.items() if row[4].startswith("Central")]
        riverside = self.stop["Riverside ferry terminal"]
        self.assertTrue(central)
        for stop in central:
            self.assertNotIn(riverside, [other for other, _ in self.city.walks(stop)])

    def test_walks_to_stops_follow_streets(self):
        when = datetime.combine(date.today(), time(7, 55), ZoneInfo("Australia/Brisbane"))
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network while planning")):
            option = journey.plan((-27.4658, 153.0168), (-27.4440, 153.0390), when=when)["options"][0]
        walk = option["legs"][0]
        self.assertEqual(walk["kind"], "walk")
        self.assertGreater(len(walk["path"]), 2)  # along the street, not a straight line
        self.assertEqual(walk["path"][0], [-27.4658, 153.0168])

    def test_outside_the_streets_falls_back(self):
        # Bowen Hills isn't on this map: the walk there is the straight-line estimate.
        when = datetime.combine(date.today(), time(7, 55), ZoneInfo("Australia/Brisbane"))
        option = journey.plan((-27.4658, 153.0168), (-27.4440, 153.0390), when=when)["options"][0]
        last = option["legs"][-1]
        self.assertEqual((last["kind"], len(last["path"])), ("walk", 2))
