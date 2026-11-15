import json
from datetime import date, datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from journeys import journey

from .test_transit import TransitDirMixin, build_brisbane

BRISBANE = ZoneInfo("Australia/Brisbane")
NEAR_ROMA = {"lat": -27.4658, "lon": 153.0190}
NEAR_BOWEN = {"lat": -27.4438, "lon": 153.0390}
NEAR_SOUTH_BANK = {"lat": -27.4792, "lon": 153.0228}
NO_LIVE = ({}, {"feeds": 1, "of": 1, "age": 0, "version": ()})


def live(updates):
    """Patch the live feeds to report these updates ({(feed, trip id): update})."""
    status = {"feeds": 1, "of": 1, "age": 10, "version": (("test", len(updates)),)}
    return mock.patch.multiple("journeys.realtime", updated=mock.Mock(return_value=({key: key for key in updates}, status)),
                               read=mock.Mock(side_effect=lambda key: updates[key]))


def at(hours, minutes):
    return datetime.combine(date.today(), time(hours, minutes), BRISBANE)


def point(place):
    return (place["lat"], place["lon"])


class PlannerTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_brisbane()
        quiet = mock.patch("journeys.realtime.updated", return_value=NO_LIVE)
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_a_train_journey(self):
        result = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55))
        option = result["options"][0]
        self.assertEqual((option["leave"], option["arrive"], option["changes"]), ("07:59", "08:09", 0))
        ride = [leg for leg in option["legs"] if leg["kind"] == "ride"][0]
        self.assertEqual((ride["line"], ride["from"], ride["to"], ride["depart"], ride["stops"]), ("Roma Street – Bowen Hills", "Roma Street", "Bowen Hills", "08:00", 2))
        self.assertEqual([leg["kind"] for leg in option["legs"]], ["walk", "ride", "walk"])
        self.assertEqual(result["zone"], "AEST")
        self.assertEqual((option["leave_at"], option["arrive_at"]), (at(7, 59).isoformat(timespec="minutes"), at(8, 9).isoformat(timespec="minutes")))  # for calendars

    def test_changing_from_train_to_ferry(self):
        # From Bowen Hills the only way to South Bank is the train to Central, a walk to Riverside, then the ferry.
        options = journey.plan(point(NEAR_BOWEN), (-27.4800, 153.0228), when=at(8, 55))["options"]
        option = options[0]
        self.assertEqual([(leg["kind"], leg.get("line", "")) for leg in option["legs"]],
                         [("walk", ""), ("ride", "Roma Street – Bowen Hills"), ("walk", ""), ("ride", "F1"), ("walk", "")])
        self.assertTrue(option["legs"][2].get("change"))
        self.assertEqual((option["leave"], option["arrive"], option["changes"]), ("08:59", "10:13", 1))

    def test_live_delays_and_cancellations(self):
        late = {(0, "T1"): {"cancelled": False, "start_date": "", "stops": [(1, "1002", (None, None), (180, None), False)]}}
        with live(late):
            result = journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55))
        ride = [leg for leg in result["options"][0]["legs"] if leg["kind"] == "ride"][0]
        self.assertEqual((ride["depart"], ride["arrive"], ride["live"]), ("08:03", "08:12", True))
        self.assertTrue(result["live"]["live"])
        gone = {(0, "T1"): {"cancelled": True, "start_date": "", "stops": []}}
        with live(gone):
            with self.assertRaisesMessage(journey.PlanError, "No public transport"):
                journey.plan(point(NEAR_ROMA), point(NEAR_BOWEN), when=at(7, 55))

    def test_pointless_options_are_dropped(self):
        def option(leave, arrive, changes):
            return {"leave_secs": leave * 60, "arrive_secs": arrive * 60, "changes": changes, "summary": f"{leave}"}
        kept = journey._best([option(985, 1040, 1), option(989, 1040, 1), option(995, 1040, 0), option(1010, 1055, 0), option(986, 1036, 1)])
        # Leaving earlier with a change to arrive at the same time (or 4 minutes sooner) isn't worth showing.
        self.assertEqual([o["summary"] for o in kept], ["995", "1010"])

    def test_short_trips_can_be_walked(self):
        options = journey.plan(point(NEAR_ROMA), (-27.4661, 153.0263), when=at(7, 55))["options"]
        self.assertIn("Walk", [option["summary"] for option in options])

    def test_departures_nearby(self):
        result = journey.nearby((-27.4661, 153.0262), when=at(7, 58))
        group = result["groups"][0]
        self.assertEqual((group["stop"], group["line"]), ("Central", "Roma Street – Bowen Hills"))
        self.assertEqual(group["times"][0], {"time": "08:04", "minutes": 6, "live": False})

    def test_buses_are_in_the_planner(self):
        groups = journey.nearby((-27.4700, 153.0300), when=at(11, 58))["groups"]
        bus = next(group for group in groups if group["mode"] == "bus")
        self.assertEqual((bus["line"], bus["stop"], bus["times"][0]["time"]), ("100", "Bus stop A", "12:00"))

    def test_outside_the_cities(self):
        with self.assertRaisesMessage(journey.PlanError, "Brisbane, Melbourne and Sydney"):
            journey.plan((22.30, 114.17), (22.28, 114.16))
        with self.assertRaisesMessage(journey.PlanError, "same city"):
            journey.plan(point(NEAR_ROMA), (-33.87, 151.21))


class JourneyPageTests(TransitDirMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_brisbane()
        cache.clear()
        quiet = mock.patch("journeys.realtime.updated", return_value=NO_LIVE)
        quiet.start()
        self.addCleanup(quiet.stop)

    def post(self, name, body):
        return self.client.post(reverse(name), json.dumps(body), content_type="application/json")

    def test_page(self):
        page = self.client.get(reverse("core:journey"))
        self.assertContains(page, "Journey planner")
        self.assertNotContains(page, "data-city=")  # no city to choose: it comes from the places
        self.assertContains(page, "International routes")
        self.assertNotContains(page, "data-flights-url")  # flight search is for signed-in staff
        self.assertNotContains(page, "journey-trips")  # adding to trips is for signed-in staff
        self.assertContains(page, "Your location stays private")
        self.assertContains(self.client.get("/"), 'href="/journey/"')

    def test_planning_stores_and_logs_nothing(self):
        with CaptureQueriesContext(connection) as queries, self.assertNoLogs("journeys", "WARNING"), \
                mock.patch("journeys.journey.timezone.now", return_value=at(7, 55)):
            response = self.post("core:journey_plan", {"from": NEAR_ROMA, "to": NEAR_BOWEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json()["options"][0]["arrive"], "08:09")
        writes = [q["sql"] for q in queries.captured_queries if not q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(writes, [])  # nothing about the visitor is written anywhere

    def test_locations_only_by_post(self):
        self.assertEqual(self.client.get(reverse("core:journey_plan"), {"lat": 1}).status_code, 405)
        self.assertEqual(self.client.get(reverse("core:journey_nearby")).status_code, 405)
        self.assertEqual(self.post("core:journey_plan", {"from": NEAR_ROMA}).status_code, 400)
        self.assertEqual(self.post("core:journey_plan", {"from": {"lat": "nan", "lon": 1}, "to": NEAR_BOWEN}).status_code, 400)
        outside = self.post("core:journey_plan", {"from": {"lat": 22.3, "lon": 114.2}, "to": {"lat": 22.28, "lon": 114.16}})
        self.assertEqual(outside.status_code, 422)
        self.assertIn("Brisbane, Melbourne and Sydney", outside.json()["error"])

    def test_errors_never_log_the_place(self):
        with mock.patch("journeys.journey.plan", side_effect=RuntimeError("at -27.4658, 153.0190")), self.assertLogs("journeys", "WARNING") as logs:
            response = self.post("core:journey_plan", {"from": NEAR_ROMA, "to": NEAR_BOWEN})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("27.4658", " ".join(logs.output))
        self.assertNotIn("27.4658", response.content.decode())

    def test_fair_use_limit(self):
        with mock.patch("journeys.journey_views.LIMITS", {"plan": 2, "nearby": 2, "places": 2}):
            codes = [self.post("core:journey_nearby", {"at": NEAR_ROMA}).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_place_search_sends_only_the_words_and_city(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value = reply
        reply.read.return_value = json.dumps({"features": [{
            "geometry": {"coordinates": [153.0289, -27.4747]},
            "properties": {"name": "Roma Street Parkland", "osm_value": "park", "city": "Brisbane"},
        }]}).encode()
        with mock.patch("journeys.journey_views.urllib.request.urlopen", return_value=reply) as fetch:
            results = self.post("core:journey_places", {"q": "roma street park", "city": "brisbane"}).json()["results"]
        self.assertEqual(results, [{"name": "Roma Street Parkland", "detail": "Park · Brisbane", "lat": -27.4747, "lon": 153.0289}])
        sent = fetch.call_args.args[0].full_url
        self.assertIn("q=roma+street+park", sent)
        self.assertIn("lat=-27.4698", sent)  # the city's centre, not the visitor
        self.assertEqual(self.post("core:journey_places", {"q": "ro", "city": "brisbane"}).json(), {"results": []})
