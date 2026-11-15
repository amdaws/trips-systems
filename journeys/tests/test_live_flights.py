import io
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from journeys import live_flights
from journeys.management.commands.run_jobs import Command as RunJobs
from journeys.models import Airline, Day, Leg, Place, Trip

from .factories import place

BNE, HKG = (-27.38, 153.12), (22.31, 113.91)
TAKE_OFF = datetime(2026, 12, 27, 1, 20, tzinfo=UTC)  # 11:20 in Brisbane


def aircraft(lat, lon, altitude=37000, callsign="CPA156", seen=1.0):
    return {"flight": f"{callsign}  ", "hex": "780a94", "lat": lat, "lon": lon, "alt_baro": altitude, "gs": 480.0,
            "track": 300.5, "seen_pos": seen}


def answer(*planes, at=TAKE_OFF):
    return {"ac": list(planes), "now": at.timestamp() * 1000, "total": len(planes), "msg": "No error"}


def between(fraction):
    return BNE[0] + (HKG[0] - BNE[0]) * fraction, BNE[1] + (HKG[1] - BNE[1]) * fraction


class Flight:
    """CX156 Brisbane-Hong Kong on 27 Dec 2026, a public flight on a public trip."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        override = override_settings(TRANSIT_DIR=folder.name, ADSBLOL_TRACKS=False)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        Airline.objects.create(code="CX", icao="CPA", name="Cathay Pacific")
        self.bne = place("Brisbane International Airport", Place.Kind.AIRPORT, *BNE, code="BNE", country="AU", tz="Australia/Brisbane")
        self.hkg = place("Hong Kong International Airport", Place.Kind.AIRPORT, *HKG, code="HKG", country="HK", tz="Asia/Hong_Kong")
        self.trip = Trip.objects.create(title="T", slug="t", start_date=date(2026, 12, 27), end_date=date(2027, 1, 31),
                                        visibility=Trip.Visibility.PUBLIC)
        self.leg = Leg.objects.create(
            trip=self.trip, mode="flight", number="CX156", origin=self.bne, destination=self.hkg, is_public=True,
            depart_date=date(2026, 12, 27), depart_time=time(11, 0), arrive_date=date(2026, 12, 27), arrive_time=time(18, 0),
        )
        Day.objects.create(trip=self.trip, date=date(2026, 12, 27), title="Fly to Hong Kong")

    def live(self, now, *answers):
        with mock.patch("journeys.live_flights.get_json", side_effect=list(answers)) as get:
            result = live_flights.for_trip([self.leg], now)
        return result, get


class LiveFlightTests(Flight, TestCase):
    def test_watched_from_an_hour_before_until_three_hours_after(self):
        start, end = live_flights.window(self.leg)
        self.assertEqual(start, datetime(2026, 12, 27, 0, 0, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 12, 27, 13, 0, tzinfo=UTC))  # arrives 18:00 in Hong Kong (10:00 UTC)
        result, get = self.live(start - timedelta(hours=2))
        self.assertEqual(result, {"flights": [], "poll": 7200})
        get.assert_not_called()

    def test_flying_without_the_flight_number(self):
        now = TAKE_OFF + timedelta(minutes=30)
        result, get = self.live(now, answer(aircraft(*between(0.1)), at=now))
        self.assertIn("/v2/callsign/CPA156", get.call_args.args[0])
        [flight] = result["flights"]
        self.assertEqual(flight["status"], "flying")
        self.assertEqual(flight["position"]["altitude_ft"], 37000)
        self.assertEqual(flight["position"]["kmh"], 889)
        self.assertEqual(flight["trail"][0], list(BNE))
        self.assertEqual(result["poll"], 30)
        self.assertNotIn("156", str(result))

        # Asked again within 30 seconds: the kept position, without asking adsb.lol.
        _, get = self.live(now + timedelta(seconds=10))
        get.assert_not_called()

    def test_the_same_callsign_somewhere_else_is_ignored(self):
        now = TAKE_OFF + timedelta(minutes=30)
        result, _ = self.live(now, answer(aircraft(51.47, -0.45), at=now))  # over London
        self.assertEqual(result["flights"][0]["status"], "waiting")
        self.assertIsNone(result["flights"][0]["position"])

    def test_out_of_range_shows_an_estimate(self):
        seen = TAKE_OFF + timedelta(hours=2)
        self.live(seen, answer(aircraft(*between(0.3)), at=seen))
        result, _ = self.live(seen + timedelta(minutes=45), answer(at=seen + timedelta(minutes=45)))
        flight = result["flights"][0]
        self.assertEqual(flight["status"], "out_of_range")
        self.assertEqual(flight["position"]["seen_secs"], 45 * 60 + 1)  # adsb.lol had it a second before
        lat, lon = flight["estimate"]
        self.assertGreater(lat, between(0.3)[0])  # carried on towards Hong Kong
        self.assertLess(lat, HKG[0])

    def flies_there(self):
        """Positions all the way to Hong Kong, then the job runs after landing."""
        for step in range(11):
            now = TAKE_OFF + timedelta(minutes=40 * step)
            lat, lon = between(step / 10)
            plane = aircraft(lat, lon, altitude=500 if step == 10 else 37000)
            with mock.patch("journeys.live_flights.get_json", return_value=answer(plane, at=now)):
                live_flights.refresh(self.leg, now)
        out = io.StringIO()
        with mock.patch("django.utils.timezone.now", return_value=TAKE_OFF + timedelta(hours=7, minutes=10)), \
                mock.patch("journeys.live_flights.get_json", return_value=answer()):
            RunJobs(stdout=out).record_live_flights()
        return out.getvalue()

    def test_the_recording_becomes_the_route_after_landing(self):
        log = self.flies_there()
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.path_source, Leg.PathSource.LIVE)
        self.assertEqual(self.leg.path[0], list(BNE))
        self.assertEqual(self.leg.path[-1], list(HKG))
        self.assertIn("Real track of CPA156 on 27 Dec 2026, recorded live (adsb.lol).", self.leg.path_message)
        self.assertIn("recorded live", log)
        result, get = self.live(TAKE_OFF + timedelta(hours=7, minutes=20))
        self.assertEqual(result["flights"][0]["status"], "landed")
        self.assertEqual(result["poll"], 300)
        get.assert_not_called()  # saved: adsb.lol isn't asked again

    def test_a_short_recording_leaves_the_route_alone(self):
        before = self.leg.path_source
        now = TAKE_OFF + timedelta(hours=6, minutes=50)
        with mock.patch("journeys.live_flights.get_json", return_value=answer(aircraft(HKG[0] + 0.05, HKG[1], altitude=0), at=now)):
            live_flights.refresh(self.leg, now)
        with mock.patch("django.utils.timezone.now", return_value=now + timedelta(minutes=5)), \
                mock.patch("journeys.live_flights.get_json", return_value=answer()):
            RunJobs(stdout=io.StringIO()).record_live_flights()
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.path_source, before)


class LivePageTests(Flight, TestCase):
    def get(self, url, now):
        with mock.patch("django.utils.timezone.now", return_value=now), \
                mock.patch("journeys.live_flights.get_json", return_value=answer(aircraft(*between(0.2)), at=now)):
            return self.client.get(url)

    def test_visitors_see_public_flights_without_numbers(self):
        hidden = Leg.objects.create(
            trip=self.trip, mode="flight", number="CX100", origin=self.bne, destination=self.hkg, is_public=False,
            depart_date=date(2026, 12, 27), depart_time=time(11, 0), arrive_time=time(18, 0),
        )
        now = TAKE_OFF + timedelta(hours=1)
        response = self.get("/trips/t/live.json", now)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        ids = [f["id"] for f in response.json()["flights"]]
        self.assertEqual(ids, [self.leg.pk])
        self.assertNotIn(b"CX156", response.content)
        self.assertNotIn(b"CPA", response.content)
        shared = self.get(f"/s/{self.trip.share_token}/live.json", now)
        self.assertEqual(sorted(f["id"] for f in shared.json()["flights"]), sorted([self.leg.pk, hidden.pk]))

    def test_trips_that_hide_their_dates_dont_show_visitors_live_flights(self):
        Trip.objects.filter(pk=self.trip.pk).update(hide_dates=True)
        response = self.get("/trips/t/live.json", TAKE_OFF + timedelta(hours=1))
        self.assertEqual(response.json(), {"flights": [], "poll": None})

    def test_private_trips_are_not_found(self):
        Trip.objects.filter(pk=self.trip.pk).update(visibility=Trip.Visibility.PRIVATE)
        self.assertEqual(self.get("/trips/t/live.json", TAKE_OFF).status_code, 404)

    def test_the_trip_page_asks_for_live_flights(self):
        page = self.client.get("/trips/t/").content.decode()
        self.assertIn('data-live="/trips/t/live.json"', page)
        self.assertIn(f'data-live-card="{self.leg.pk}"', page)
        self.assertIn(f'data-live-detail="{self.leg.pk}"', page)
