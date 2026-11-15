import io
import shutil
import tempfile
import zipfile
from datetime import date, time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from journeys import metro, transit
from journeys.management.commands import run_jobs
from journeys.models import Leg, MetroNetwork, Place, Stay, Trip

# A tiny Brisbane timetable: a train line Roma Street – Central – Bowen Hills, a ferry from
# Riverside (a short walk from Central) along the river to South Bank, and a bus to leave out.
FEED = {
    "routes.txt": """route_id,route_short_name,route_long_name,route_type,route_color,route_text_color
R1,BRBH,Roma Street - Bowen Hills,2,FFC425,1D1D1D
F1,F1,Riverside / South Bank,4,61C5C9,FFFFFF
B100,100,City - Somewhere,3,000000,FFFFFF
""",
    "stops.txt": """stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station
place_rom,Roma Street station,-27.4656,153.0194,1,
1002,"Roma Street station, platform 2",-27.4656,153.0194,0,place_rom
place_cen,Central station,-27.4661,153.0263,1,
1001,"Central station, platform 1",-27.4661,153.0263,0,place_cen
place_bow,Bowen Hills station,-27.4436,153.0386,1,
1003,"Bowen Hills station, platform 1",-27.4436,153.0386,0,place_bow
2001,Riverside ferry terminal,-27.4661,153.0290,0,
2002,South Bank ferry terminal,-27.4790,153.0230,0,
3001,Bus stop A,-27.4700,153.0300,0,
""",
    "trips.txt": """route_id,service_id,trip_id,direction_id,shape_id
R1,WK,T1,0,S1
R1,WK,T2,1,S2
F1,WK,T3,0,S3
F1,WK,T4,1,S4
B100,WK,T5,0,
""",
    "stop_times.txt": """trip_id,arrival_time,departure_time,stop_id,stop_sequence
T1,08:00:00,08:00:00,1002,1
T1,08:03:00,08:04:00,1001,2
T1,08:09:00,08:09:00,1003,3
T2,09:00:00,09:00:00,1003,1
T2,09:05:00,09:06:00,1001,2
T2,09:09:00,09:09:00,1002,3
T3,10:00:00,10:00:00,2001,1
T3,10:12:00,10:12:00,2002,2
T4,11:00:00,11:00:00,2002,1
T4,11:12:00,11:12:00,2001,2
T5,12:00:00,12:00:00,3001,1
T5,12:05:00,12:05:00,1001,2
""",
    "calendar.txt": """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WK,1,1,1,1,1,1,1,20200101,20991231
""",
    "shapes.txt": """shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence
S1,-27.4656,153.0194,1
S1,-27.4661,153.0263,2
S1,-27.4436,153.0386,3
S2,-27.4436,153.0386,1
S2,-27.4661,153.0263,2
S2,-27.4656,153.0194,3
S3,-27.4661,153.0290,1
S3,-27.4720,153.0330,2
S3,-27.4790,153.0230,3
S4,-27.4790,153.0230,1
S4,-27.4720,153.0330,2
S4,-27.4661,153.0290,3
""",
}


def feed_zip():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, text in FEED.items():
            archive.writestr(name, text)
    data.seek(0)
    return data


def build_brisbane():
    with mock.patch("journeys.transit._download", side_effect=lambda url, key=None: feed_zip()):
        return transit.refresh("brisbane")


class TransitDirMixin:
    """Each test's timetables go in a folder of their own, never the real one."""

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        override = override_settings(TRANSIT_DIR=folder)
        override.enable()
        self.addCleanup(override.disable)
        super().setUp()


class TimetableTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.network = build_brisbane()
        self.slugs = {s["name"]: s["slug"] for s in self.network.stations}

    def test_stations_lines_and_places(self):
        self.assertEqual(set(self.slugs), {"Roma Street", "Central", "Bowen Hills", "Riverside ferry terminal", "South Bank ferry terminal"})
        self.assertNotIn("Bus stop A", self.slugs)  # buses are left out
        line = self.network.lines["bri0:train:Bowen Hills|Roma Street"]
        self.assertEqual((line["name"], line["colour"], line["operator"]), ("Roma Street – Bowen Hills", "#FFC425", "Queensland Rail"))
        central = Place.objects.get(source="timetable", name="Central")
        self.assertEqual((central.kind, central.city, central.tz), (Place.Kind.STATION, "Brisbane", "Australia/Brisbane"))
        self.assertEqual(Place.objects.get(source="timetable", name="Riverside ferry terminal").kind, Place.Kind.PIER)
        build_brisbane()  # next week's refresh updates the same places
        self.assertEqual(Place.objects.filter(source="timetable").count(), 5)

    def test_journeys(self):
        direct = transit.route(self.network, self.slugs["Roma Street"], self.slugs["Bowen Hills"])
        self.assertEqual((direct["duration"], direct["transfers"]), ("9 min", "No changes"))
        self.assertEqual([r["count"] for r in direct["rides"]], ["2 stops"])
        changing = transit.route(self.network, self.slugs["Roma Street"], self.slugs["South Bank ferry terminal"])
        self.assertEqual([(r["line"], r["from"], r["to"]) for r in changing["rides"]], [
            ("Roma Street – Bowen Hills", "Roma Street", "Central"),
            ("Walk", "Central", "Riverside ferry terminal"),
            ("F1", "Riverside ferry terminal", "South Bank ferry terminal"),
        ])
        self.assertEqual(changing["transfers"], "1 change")
        self.assertIsNone(transit.route(self.network, self.slugs["Central"], self.slugs["Central"]))

    def test_rides_follow_the_lines_real_path(self):
        found = transit.route(self.network, self.slugs["Riverside ferry terminal"], self.slugs["South Bank ferry terminal"])
        path = transit.ride_path(self.network, found["rides"][0])
        self.assertIn([-27.472, 153.033], path)  # the river's bend, not a straight line between wharves

    def test_transport_legs_get_line_time_and_route(self):
        trip = Trip.objects.create(title="Brisbane weekend", slug="bne", start_date=date(2027, 3, 6), end_date=date(2027, 3, 7))
        user = get_user_model().objects.create_superuser("aaron", "aaron@example.com", None)
        self.client.force_login(user)
        roma, bowen = (Place.objects.get(source="timetable", name=n) for n in ("Roma Street", "Bowen Hills"))
        hint = self.client.get(reverse("manage:api_transit_journey"), {"from": roma.pk, "to": bowen.pk}).json()
        self.assertEqual(hint, {"found": True, "summary": "Roma Street – Bowen Hills · 9 min · No changes", "minutes": 9, "operator": "Queensland Rail"})
        self.client.post(reverse("manage:leg_create", args=[trip.pk]), {
            "mode": "train", "origin": roma.pk, "destination": bowen.pk, "depart_date": "2027-03-06", "depart_time": "08:00",
            "status": "planned",
        })
        leg = Leg.objects.get()
        self.assertEqual((leg.operator, leg.arrive_time), ("Queensland Rail", time(8, 9)))
        self.assertEqual((leg.path_source, leg.path_status), (Leg.PathSource.TIMETABLE, Leg.PathStatus.OK))
        self.assertGreaterEqual(len(leg.path), 3)
        self.assertEqual(self.client.get(reverse("manage:api_transit_journey"), {"from": roma.pk, "to": "x"}).json(), {"found": False})

    def test_stays_get_directions_from_the_timetable(self):
        trip = Trip.objects.create(title="Brisbane weekend", slug="bne", start_date=date(2027, 3, 6), end_date=date(2099, 3, 7),
                                   visibility=Trip.Visibility.PUBLIC)
        city = Place.objects.create(kind=Place.Kind.CITY, name="Brisbane", lat=-27.4698, lon=153.0251, tz="Australia/Brisbane", country="AU")
        hotel = Place.objects.create(kind=Place.Kind.HOTEL, name="Hotel", lat=-27.4440, lon=153.0380, tz="Australia/Brisbane", country="AU")
        stay = Stay.objects.create(trip=trip, label="Brisbane 1", city=city, location=hotel, check_in_date=date(2027, 3, 6),
                                   check_out_date=date(2027, 3, 7), is_public=True)
        self.assertEqual(metro.network_for(city), self.network)
        with mock.patch("journeys.metro._nearest_exit", return_value=""), mock.patch("journeys.metro._drive", return_value=None):
            info = metro.refresh_stay(stay)
        self.assertEqual((info["network"], info["station"]), ("brisbane", self.slugs["Bowen Hills"]))
        view = metro.for_stays(trip, [stay])[0].metro_view
        self.assertEqual((view["station"]["name"], view["map_label"]), ("Bowen Hills", "Translink journey planner"))

        data = self.client.get(reverse("journeys:map", args=[trip.slug])).json()
        self.assertIn("#FFC425", {line["colour"] for line in data["lines"]})  # the city's lines on the trip map
        page = self.client.get(reverse("journeys:stays", args=[trip.slug]))
        self.assertContains(page, "Timetables: Translink (Queensland Government) (CC BY 4.0)")

    def test_cities_without_a_network(self):
        self.assertIsNone(transit.city_for((22.3, 114.2)))  # Hong Kong: MetroMan as before
        self.assertEqual(transit.city_for((-28.0, 153.43)), "brisbane")  # Gold Coast trams are Translink's
        self.assertEqual(transit.map_lines(transit.networks_near([(22.3, 114.2)])), [])


class RefreshTests(TransitDirMixin, TestCase):
    @override_settings(TFNSW_API_KEY="")
    def test_schedule_and_failures(self):
        self.assertEqual(transit.needs_key("sydney"), "TFNSW_API_KEY")
        self.assertEqual(transit.due(), "brisbane")
        build_brisbane()
        self.assertEqual(transit.due(), "melbourne")
        transit.mark_failed("melbourne", RuntimeError("down"))
        self.assertIsNone(transit.due())  # Melbourne waits before trying again; Sydney needs its key
        self.assertEqual(MetroNetwork.objects.get(slug="melbourne").status["error"], "down")
        with override_settings(TFNSW_API_KEY="key"):
            self.assertEqual(transit.due(), "sydney")

    def test_job_rebuilds_and_stays_look_again(self):
        trip = Trip.objects.create(title="Later", slug="later", start_date=date(2099, 1, 1), end_date=date(2099, 1, 3))
        city = Place.objects.create(kind=Place.Kind.CITY, name="Brisbane", lat=-27.4698, lon=153.0251, tz="Australia/Brisbane", country="AU")
        stay = Stay.objects.create(trip=trip, label="Brisbane 1", city=city, check_in_date=date(2099, 1, 1), check_out_date=date(2099, 1, 2),
                                   metro={"message": "Brisbane's train, tram and ferry timetables aren't loaded yet."})
        out = io.StringIO()
        with mock.patch("journeys.transit._download", side_effect=lambda url, key=None: feed_zip()):
            run_jobs.Command(stdout=out).refresh_transit(deadline=float("inf"))
        self.assertIn("Timetables for Brisbane: 5 stations, 2 lines", out.getvalue())
        stay.refresh_from_db()
        self.assertTrue(stay.metro["pending"])
        with mock.patch("journeys.transit._download", side_effect=OSError("offline")):
            run_jobs.Command(stdout=out).refresh_transit(deadline=float("inf"))  # Melbourne fails: noted, not raised
        self.assertEqual(MetroNetwork.objects.get(slug="melbourne").status["error"], "offline")

    @override_settings(TFNSW_API_KEY="key")
    def test_sydney_needs_only_its_trains(self):
        def download(url, key=None):
            if url.endswith("/sydneytrains"):
                self.assertEqual(key, "TFNSW_API_KEY")
                return feed_zip()
            raise OSError("404")
        with mock.patch("journeys.transit._download", side_effect=download) as fetch:
            network = transit.refresh("sydney")
        self.assertEqual(network.status["lines"], 2)
        self.assertEqual(len(network.status["skipped"]), 7)  # metro (both addresses tried), light rail, ferries and buses noted
        self.assertIn("https://api.transport.nsw.gov.au/v1/gtfs/schedule/metro", [call.args[0] for call in fetch.call_args_list])
        with mock.patch("journeys.transit._download", side_effect=OSError("401")), self.assertRaises(OSError):
            transit.refresh("sydney")  # no trains, no network

    def test_sydney_leaves_out_empty_trains(self):
        self.assertIsNone(transit._nsw({"route_type": "2", "route_short_name": "Non Revenue", "route_long_name": "", "route_id": "x"}, "train"))
        self.assertIsNone(transit._nsw({"route_type": "714", "route_short_name": "T1", "route_long_name": "Bus", "route_id": "x"}, "train"))
        self.assertEqual(transit._nsw({"route_type": "2", "route_short_name": "T1", "route_long_name": "North Shore", "route_id": "x"}, "train")["name"], "T1")

    def test_names(self):
        self.assertEqual(transit._clean_name("Glenferrie Rd/Wattletree Rd #45", "tram"), ("Glenferrie Rd/Wattletree Rd", "45"))
        self.assertEqual(transit._clean_name("Central station, platform 3", "train"), ("Central", ""))
        self.assertEqual(transit._clean_name("King George Square bus station", "metro"), ("King George Square", ""))
        self.assertEqual(transit._line("x", "ferry", "F1", "", {"route_color": "zz"}, "op")["colour"], "#0086c0")  # a bad colour gets the mode's
