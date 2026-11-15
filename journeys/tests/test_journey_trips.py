"""Leaving at a chosen time, adding a journey to a trip, and China's trains from 12306."""

import json
from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from journeys import china_rail, flight_search, journey, timetable
from journeys.models import Leg, Place, Trip

from .test_journey import NEAR_BOWEN, NEAR_ROMA, NO_LIVE, at, point
from .test_transit import TransitDirMixin, build_brisbane

BRISBANE = ZoneInfo("Australia/Brisbane")
STATIONS = ("@gzn|广州南|IZQ|guangzhounan|gzn|0|0001|广州|||@glb|桂林北|GBZ|guilinbei|glb|1|0002|桂林|||"
            "@gl|桂林|GLZ|guilin|gl|2|0003|桂林|||@xab|西安北|EAY|xianbei|xab|3|0004|西安|||@ys|阳朔|YCZ|yangshuo|ys|4|0005|阳朔|||")


def later(days, hours, minutes):
    return datetime.combine(date.today() + timedelta(days=days), time(hours, minutes), BRISBANE)


def train_row(number, depart, arrive, duration, start="IZQ", end="GBZ", seats=None, status="预订"):
    """A row of 12306's answer: its fields joined by '|' (only those the planner reads are filled)."""
    fields = [""] * 59
    fields[0], fields[1], fields[3] = "secret", status, number
    fields[4], fields[5], fields[6], fields[7] = start, end, start, end
    fields[8], fields[9], fields[10], fields[11] = depart, arrive, duration, "Y"
    for index, value in (seats or {}).items():
        fields[index] = value
    return "|".join(fields)


ANSWER = {"status": True, "httpstatus": 200, "data": {"map": {"IZQ": "广州南", "GBZ": "桂林北"}, "result": [
    train_row("D2966", "06:48", "09:33", "02:45", seats={31: "20", 30: "有", 26: "无"}),
    train_row("G3886", "06:37", "08:55", "02:18", seats={32: "2", 30: "有"}),
    train_row("K1234", "22:10", "08:05", "09:55", seats={28: "有", 29: "无"}),
    train_row("G9999", "24:00", "24:00", "99:59", status="列车停运"),
]}}


class LeaveLaterTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_brisbane()
        quiet = mock.patch("journeys.realtime.updated", return_value=NO_LIVE)
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_the_timetable_day_for_a_later_date(self):
        city = timetable.open_city("brisbane")
        today = date.today()
        self.assertEqual(city.timetable_day(today), today)
        far = today + timedelta(days=100)
        used = city.timetable_day(far)
        self.assertEqual(used.weekday(), far.weekday())  # the same day of the week…
        self.assertLessEqual(used, today + timedelta(days=timetable.SERVICE_DAYS))  # …from the published weeks
        self.assertIsNone(city.timetable_day(today - timedelta(days=30)))

    def test_planning_a_later_day(self):
        result = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=later(100, 7, 55))
        option = result["options"][0]
        self.assertEqual((option["leave"], option["arrive"]), ("07:59", "08:09"))
        self.assertIn("isn't out yet", " ".join(result["notes"]))
        self.assertFalse(result["live"]["live"])
        ride = [leg for leg in option["legs"] if leg["kind"] == "ride"][0]
        self.assertEqual(ride["depart_at"], later(100, 8, 0).isoformat(timespec="minutes"))  # the day asked for, not the one borrowed
        self.assertEqual(ride["from_place"]["name"], "Roma Street")
        self.assertTrue(ride["from_place"]["station"])

    def test_tomorrow_uses_its_own_timetable(self):
        result = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=later(1, 7, 55))
        self.assertEqual(result["notes"], [])

    def test_leave_at_through_the_page(self):
        cache.clear()
        post = lambda body: self.client.post(reverse("core:journey_plan"), json.dumps(body), content_type="application/json")  # noqa: E731
        stamp = later(2, 7, 55).strftime("%Y-%m-%dT%H:%M")
        reply = post({"from": NEAR_ROMA, "to": NEAR_BOWEN, "when": stamp})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["options"][0]["arrive"], "08:09")
        self.assertEqual(post({"from": NEAR_ROMA, "to": NEAR_BOWEN, "when": "2001-01-01T08:00"}).json()["error"], "Choose a time from now on.")
        self.assertEqual(post({"from": NEAR_ROMA, "to": NEAR_BOWEN, "when": "soon"}).status_code, 400)


class PlaceSearchTests(TestCase):
    def test_searching_every_city_when_none_is_known(self):
        cache.clear()
        reply = mock.MagicMock()
        reply.__enter__.return_value = reply
        reply.read.return_value = json.dumps({"features": [
            {"geometry": {"coordinates": [151.2743, -33.8915]}, "properties": {"name": "Bondi Beach", "osm_value": "beach"}},
            {"geometry": {"coordinates": [149.13, -35.28]}, "properties": {"name": "Bondi Café, Canberra", "osm_value": "cafe"}},
        ]}).encode()
        with mock.patch("journeys.journey_views.urllib.request.urlopen", return_value=reply) as fetch:
            results = self.client.post(reverse("core:journey_places"), json.dumps({"q": "bondi"}), content_type="application/json").json()["results"]
        self.assertEqual([r["name"] for r in results], ["Bondi Beach"])  # Canberra isn't one of the cities
        sent = fetch.call_args.args[0].full_url
        self.assertIn("bbox=", sent)
        self.assertNotIn("lat=", sent)


class AddToTripTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_brisbane()
        cache.clear()
        quiet = mock.patch("journeys.realtime.updated", return_value=NO_LIVE)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.trip = Trip.objects.create(title="Brisbane weekend", slug="bne", start_date=date.today(), end_date=date.today() + timedelta(days=2))

    def add(self, body):
        return self.client.post(reverse("core:journey_add"), json.dumps(body), content_type="application/json")

    def sign_in(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))

    def test_the_page_offers_trips_to_staff(self):
        self.sign_in()
        page = self.client.get(reverse("core:journey"))
        self.assertContains(page, 'id="journey-trips"')
        self.assertContains(page, "Brisbane weekend")

    def test_a_journeys_rides_become_private_transport(self):
        self.sign_in()
        option = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55))["options"][0]
        reply = self.add({"trip": self.trip.pk, "legs": option["legs"]})
        self.assertEqual(reply.status_code, 200, reply.content)
        self.assertEqual(reply.json()["added"], 1)
        leg = Leg.objects.get()
        roma = Place.objects.get(source="timetable", name="Roma Street")
        self.assertEqual((leg.mode, leg.origin, leg.destination.name), (Leg.Mode.TRAIN, roma, "Bowen Hills"))
        self.assertEqual((leg.depart_date, leg.depart_time, leg.arrive_time), (date.today(), time(8, 0), time(8, 9)))
        self.assertEqual((leg.is_public, leg.path_source, leg.path_status), (False, Leg.PathSource.TIMETABLE, Leg.PathStatus.OK))
        self.assertGreaterEqual(len(leg.path), 2)
        self.assertIn("journey planner", leg.notes)

    def test_a_bus_stop_becomes_a_place(self):
        self.sign_in()
        ride = {"kind": "ride", "mode": "bus", "line": "100", "operator": "Brisbane Transport", "headsign": "Somewhere",
                "depart_at": at(12, 0).isoformat(), "arrive_at": at(12, 5).isoformat(),
                "from_place": {"name": "Bus stop A", "station": "", "stop": "0:3001", "lat": -27.47, "lon": 153.03},
                "to_place": {"name": "Central", "station": "", "stop": "0:1001", "lat": -27.4661, "lon": 153.0263}, "path": "not a path"}
        self.assertEqual(self.add({"trip": self.trip.pk, "legs": [ride]}).status_code, 200)
        self.assertEqual(self.add({"trip": self.trip.pk, "legs": [ride]}).status_code, 200)
        self.assertEqual(Place.objects.filter(source="gtfs", source_id="brisbane:0:3001").count(), 1)  # made once, then reused
        leg = Leg.objects.first()
        self.assertEqual((leg.mode, leg.number, leg.path), (Leg.Mode.BUS, "100", [[-27.47, 153.03], [-27.4661, 153.0263]]))

    def test_only_staff_can_add(self):
        option = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55))["options"][0]
        self.assertEqual(self.add({"trip": self.trip.pk, "legs": option["legs"]}).status_code, 403)
        self.client.force_login(get_user_model().objects.create_user("visitor", password="x" * 16))
        self.assertEqual(self.add({"trip": self.trip.pk, "legs": option["legs"]}).status_code, 403)
        self.assertFalse(Leg.objects.exists())

    def test_bad_journeys_are_refused(self):
        self.sign_in()
        self.assertEqual(self.add({"trip": 999, "legs": []}).json()["error"], "Choose a trip.")
        self.assertEqual(self.add({"trip": self.trip.pk, "legs": [{"kind": "ride", "mode": "rocket"}]}).status_code, 400)
        outside = {"kind": "ride", "mode": "train", "depart_at": at(8, 0).isoformat(), "arrive_at": at(9, 0).isoformat(),
                   "from_place": {"lat": 22.3, "lon": 114.2}, "to_place": {"lat": 22.4, "lon": 114.1}}
        self.assertIn("Brisbane, Melbourne and Sydney", self.add({"trip": self.trip.pk, "legs": [outside]}).json()["error"])
        self.assertFalse(Leg.objects.exists())


class ChinaRailTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        Place.objects.create(name="Guangzhou South", name_local="广州南", kind=Place.Kind.STATION, country="CN", lat=22.99, lon=113.26, source="osm", source_id="n1")
        Place.objects.create(name="Xi'An North", name_local="西安北", kind=Place.Kind.STATION, country="CN", lat=34.38, lon=108.94, source="osm", source_id="n2")
        china_rail._stations_path().write_text(json.dumps(china_rail.parse_stations(STATIONS)))

    def test_english_names(self):
        self.assertEqual(china_rail.english_name("广州南", "广州"), "Guangzhou South")
        self.assertEqual(china_rail.english_name("上海虹桥", "上海"), "Shanghai Hongqiao")
        self.assertEqual(china_rail.english_name("西安北", "西安"), "Xi'an North")
        self.assertEqual(china_rail.english_name("江北", "重庆"), "Jiangbei")
        names = {s["code"]: s["name"] for s in china_rail.stations()}
        self.assertEqual((names["GBZ"], names["EAY"]), ("Guilin North", "Xi'an North"))  # OpenStreetMap's "Xi'An" tidied

    def test_finding_stations(self):
        self.assertEqual([s["code"] for s in china_rail.find_stations("guilin")], ["GLZ", "GBZ"])  # the city's main station first
        self.assertEqual([s["code"] for s in china_rail.find_stations("广州南")], ["IZQ"])
        self.assertEqual([s["code"] for s in china_rail.find_stations("Guangzhou South station")], ["IZQ"])
        self.assertEqual(china_rail.find_stations("gzn")[0]["lat"], 22.99)
        reply = self.client.post(reverse("core:journey_china_stations"), json.dumps({"q": "yangshuo"}), content_type="application/json")
        self.assertEqual(reply.json()["results"][0]["name"], "Yangshuo")

    def test_trains_on_a_day(self):
        today = date(2026, 10, 26)
        with mock.patch("journeys.china_rail._fetch", return_value=ANSWER) as fetch:
            found = china_rail.trains("IZQ", "GBZ", date(2026, 10, 31), today=today)
            china_rail.trains("IZQ", "GBZ", date(2026, 10, 31), today=today)
        self.assertEqual(fetch.call_count, 1)  # the second search is answered from the cache
        self.assertIn("leftTicketDTO.train_date=2026-10-31", fetch.call_args.args[0])
        self.assertEqual([t["number"] for t in found["trains"]], ["G3886", "D2966", "K1234"])  # in time order, the cancelled one left out
        sleeper = found["trains"][2]
        self.assertEqual((sleeper["days"], sleeper["duration"], sleeper["high_speed"]), (1, "9 h 55 min", False))
        self.assertEqual(found["trains"][1]["seats"], [{"class": "First class", "left": "20 left"}, {"class": "Second class", "left": "Available"},
                                                      {"class": "Standing", "left": "Sold out"}])
        self.assertFalse(found["projected"])
        self.assertEqual(found["book"], "https://au.trip.com/trains/china/route/guangzhou-to-guilin/")

    def test_a_day_before_tickets_are_sold(self):
        with mock.patch("journeys.china_rail._fetch", return_value=ANSWER) as fetch:
            found = china_rail.trains("IZQ", "GBZ", date(2026, 12, 25), today=date(2026, 10, 26))
        self.assertTrue(found["projected"])
        shown = date.fromisoformat(found["shown"])
        self.assertEqual(shown.weekday(), date(2026, 12, 25).weekday())
        self.assertLessEqual(shown, date(2026, 10, 26) + timedelta(days=china_rail.SALE_DAYS - 1))
        self.assertIn(f"leftTicketDTO.train_date={shown}", fetch.call_args.args[0])
        self.assertIn("go on sale on Friday 11 December", found["note"])
        with self.assertRaisesMessage(china_rail.RailError, "from today on"):
            china_rail.trains("IZQ", "GBZ", date(2026, 10, 1), today=date(2026, 10, 26))

    def test_12306_renaming_its_search(self):
        answers = [{"status": False, "c_url": "leftTicket/queryZ"}, ANSWER]
        with mock.patch("journeys.china_rail._fetch", side_effect=answers), mock.patch.dict(china_rail._session):
            found = china_rail.trains("IZQ", "GBZ", date(2026, 10, 31), today=date(2026, 10, 26))
            self.assertEqual(china_rail._session["endpoint"], "leftTicket/queryZ")
        self.assertEqual(len(found["trains"]), 3)

    def test_when_12306_is_down(self):
        with mock.patch("journeys.china_rail._fetch", return_value=None), \
                mock.patch("journeys.china_rail.china_today", return_value=date.today()):
            reply = self.client.post(reverse("core:journey_china_trains"), json.dumps({"from": "IZQ", "to": "GBZ", "date": date.today().isoformat()}),
                                     content_type="application/json")
        self.assertEqual(reply.status_code, 422)
        self.assertIn("isn't answering", reply.json()["error"])
        self.assertEqual(self.client.post(reverse("core:journey_china_trains"), json.dumps({"from": "IZQ", "to": "GBZ"}),
                                          content_type="application/json").status_code, 400)

    def test_adding_a_train_to_a_trip(self):
        trip = Trip.objects.create(title="China 2027", slug="china", start_date=date(2027, 1, 1), end_date=date(2027, 1, 20))
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        found = {"lat": 25.31, "lon": 110.29, "kind": Place.Kind.STATION, "name": "Guilin North", "source_id": "node/9"}
        train = {"number": "K1234", "from_code": "IZQ", "to_code": "GBZ", "depart": "22:10", "arrive": "08:05", "days": 1,
                 "day": "2027-01-08", "shown": "2026-11-06"}
        with mock.patch("journeys.planner_legs.geocode.search", return_value=[found]) as search:
            reply = self.client.post(reverse("core:journey_add"), json.dumps({"trip": trip.pk, "train": train}), content_type="application/json")
        self.assertEqual(reply.status_code, 200, reply.content)
        search.assert_called_once()  # Guangzhou South is already a place; Guilin North is found on the map
        leg = Leg.objects.get()
        self.assertEqual((leg.mode, leg.number, leg.operator, leg.origin.name), (Leg.Mode.TRAIN, "K1234", "China Railway", "Guangzhou South"))
        self.assertEqual((leg.depart_date, leg.depart_time, leg.arrive_date, leg.arrive_time), (date(2027, 1, 8), time(22, 10), date(2027, 1, 9), time(8, 5)))
        self.assertEqual((leg.destination.name_local, leg.destination.source), ("桂林北", "12306"))
        self.assertFalse(leg.is_public)
        self.assertIn("same day of the week", leg.notes)
        self.assertEqual(leg.path_status, Leg.PathStatus.PENDING)  # traced along the railway by the background job
        bad = dict(train, number="DROP TABLE")
        self.assertEqual(self.client.post(reverse("core:journey_add"), json.dumps({"trip": trip.pk, "train": bad}), content_type="application/json").status_code, 400)


def departure(number, to, leave, land, leave_utc, land_utc, airline="Cathay Pacific", **extra):
    """One of AeroDataBox's departures."""
    return dict({
        "departure": {"scheduledTime": {"local": leave, "utc": leave_utc}, "terminal": "I"},
        "arrival": {"airport": {"iata": to, "name": to}, "scheduledTime": {"local": land, "utc": land_utc}, "terminal": "1"},
        "number": number, "status": "Expected", "codeshareStatus": "IsOperator", "isCargo": False,
        "aircraft": {"model": "Airbus A350-900"}, "airline": {"name": airline, "iata": number[:2]},
    }, **extra)


MORNING = [
    flight_search._trim(departure("CX 156", "HKG", "2027-01-08 00:50+10:00", "2027-01-08 07:20+08:00", "2027-01-07 14:50Z", "2027-01-07 23:20Z")),
    flight_search._trim(departure("QF 51", "SIN", "2027-01-08 11:00+10:00", "2027-01-08 16:55+08:00", "2027-01-08 01:00Z", "2027-01-08 08:55Z", airline="Qantas")),
    flight_search._trim(departure("QF 9999", "HKG", "2027-01-08 09:00+10:00", "2027-01-08 15:00+08:00", "2027-01-07 23:00Z", "2027-01-08 07:00Z",
                                  codeshareStatus="IsCodeshared")),
]
EVENING = [
    flight_search._trim(departure("CX 138", "HKG", "2027-01-08 23:55+10:00", "2027-01-09 06:30+08:00", "2027-01-08 13:55Z", "2027-01-08 22:30Z")),
]


def boards(code, day, half):
    return ([f for f in (MORNING, EVENING)[half] if f], {"left": 380, "of": 400, "resets": 1_800_000_000})


@override_settings(AERODATABOX_RAPIDAPI_KEY="test-key")
class FlightSearchTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        for code, name, lat, lon, tz in (("BNE", "Brisbane", -27.38, 153.12, "Australia/Brisbane"), ("HKG", "Hong Kong", 22.31, 113.91, "Asia/Hong_Kong")):
            Place.objects.create(name=f"{name} International Airport", code=code, kind=Place.Kind.AIRPORT, lat=lat, lon=lon, tz=tz, city=name, country="AU" if code == "BNE" else "HK")
        self.trip = Trip.objects.create(title="China 2027", slug="china", start_date=date(2027, 1, 1), end_date=date(2027, 1, 20))

    def post(self, name, body):
        return self.client.post(reverse(name), json.dumps(body), content_type="application/json")

    def sign_in(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))

    def test_flights_between_two_airports(self):
        with mock.patch("journeys.flight_search._fetch", side_effect=boards) as fetch, \
                mock.patch("journeys.flight_search.timezone.localdate", return_value=date(2026, 10, 26)):
            found = flight_search.search("bne", "HKG", date(2027, 1, 8))
            again = flight_search.search("BNE", "HKG", date(2027, 1, 8))
        self.assertEqual(fetch.call_count, 2)  # the day's two halves, once: the second search reads the kept board
        self.assertEqual(found, again)
        self.assertEqual([f["number"] for f in found["flights"]], ["CX156", "CX138"])  # codeshares and other destinations left out
        late = found["flights"][1]
        self.assertEqual((late["depart"], late["arrive"], late["days"], late["duration"]), ("23:55", "06:30", 1, "8 h 35 min"))
        self.assertEqual((found["allowance"]["left"], found["allowance"]["of"]), (380, 400))
        self.assertEqual(found["book"], "https://au.trip.com/flights/brisbane-to-hong-kong/airfares-bne-hkg/")

    def test_only_for_staff(self):
        self.assertEqual(self.post("core:journey_flights", {"from": "BNE", "to": "HKG", "date": "2027-01-08"}).status_code, 403)
        self.assertEqual(self.post("core:journey_airports", {"q": "bris"}).status_code, 403)
        self.sign_in()
        page = self.client.get(reverse("core:journey"))
        self.assertContains(page, "data-flights-url")
        self.assertEqual(self.post("core:journey_airports", {"q": "hkg"}).json()["results"][0]["code"], "HKG")
        with self.settings(AERODATABOX_RAPIDAPI_KEY=""):
            self.assertNotContains(self.client.get(reverse("core:journey")), "data-flights-url")

    def test_adding_a_flight_reads_it_again_from_the_board(self):
        self.sign_in()
        with mock.patch("journeys.flight_search._fetch", side_effect=boards):
            reply = self.post("core:journey_add", {"trip": self.trip.pk, "flight": {"number": "CX138", "from": "BNE", "to": "HKG", "day": "2027-01-08",
                                                                                   "depart": "01:00"}})  # the page's own times are ignored
            missing = self.post("core:journey_add", {"trip": self.trip.pk, "flight": {"number": "CX999", "from": "BNE", "to": "HKG", "day": "2027-01-08"}})
        self.assertEqual(reply.status_code, 200, reply.content)
        leg = Leg.objects.get()
        self.assertEqual((leg.mode, leg.number, leg.operator, leg.origin.code, leg.destination.code), (Leg.Mode.FLIGHT, "CX138", "Cathay Pacific", "BNE", "HKG"))
        self.assertEqual((leg.depart_date, leg.depart_time, leg.arrive_date, leg.arrive_time), (date(2027, 1, 8), time(23, 55), date(2027, 1, 9), time(6, 30)))
        self.assertFalse(leg.is_public)
        self.assertIn("A350", leg.notes)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("isn't on BNE's departures", missing.json()["error"])

