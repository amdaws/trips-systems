import io
import urllib.error
from datetime import date, timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from journeys import flight_tracks, routing
from journeys.management.commands.run_jobs import Command as RunJobs
from journeys.models import Airline, Leg, Place, Trip

from .factories import place


def summary(fr24_id, landed, dest="HKG", ended=True, **extra):
    """Shaped like a record from Flightradar24's /flight-summary/full."""
    return {"fr24_id": fr24_id, "flight": "CX156", "callsign": "CPA156", "orig_iata": "BNE", "dest_iata": dest,
            "dest_iata_actual": None, "datetime_takeoff": None, "datetime_landed": landed, "flight_ended": ended, **extra}


FLIGHTS = {"data": [
    summary("live", None, ended=False),  # in the air now
    summary("diverted", "2026-10-25T09:00:00Z", dest_iata_actual="TPE"),  # landed somewhere else
    summary("yesterday", "2026-10-24T23:25:00"),  # no Z: Flightradar24 times are UTC
    summary("older", "2026-10-20T23:25:00Z"),
]}
EMPTY = {"data": []}
# Shaped like /flight-tracks: one entry per flight, positions under "tracks".
TRACK = [{"fr24_id": "yesterday", "tracks": [
    {"timestamp": "2026-10-24T15:40:00Z", "lat": -20.0, "lon": 149.5, "alt": 38000, "source": "ADSB"},
    {"timestamp": "2026-10-24T14:55:00Z", "lat": -27.3, "lon": 153.0, "alt": 3000, "source": "ADSB"},  # out of order
    {"timestamp": "2026-10-24T17:40:00Z", "lat": -5.0, "lon": 140.0, "alt": 38000, "source": "ESTIMATED"},  # big gap before this
    {"timestamp": "2026-10-24T23:20:00Z", "lat": 22.2, "lon": 114.0, "alt": 2000, "source": "ADSB"},
]}]


@override_settings(FLIGHTRADAR24_API_TOKEN="test-token")
class FlightTrackTests(TestCase):
    def setUp(self):
        Airline.objects.create(code="CX", icao="CPA", name="Cathay Pacific")
        self.bne = place("Brisbane International Airport", Place.Kind.AIRPORT, -27.38, 153.12, code="BNE", country="AU", tz="Australia/Brisbane")
        self.hkg = place("Hong Kong International Airport", Place.Kind.AIRPORT, 22.31, 113.91, code="HKG", country="HK", tz="Asia/Hong_Kong")
        trip = Trip.objects.create(title="T", slug="t", start_date=date(2026, 12, 27), end_date=date(2027, 1, 31))
        self.leg = Leg.objects.create(trip=trip, mode="flight", number="CX156", origin=self.bne, destination=self.hkg, depart_date=date(2026, 12, 27))

    def fake(self, answers, seen):
        """A stand-in for _get: flight-summary searches get the next answer in turn; tracks get TRACK."""
        answers = iter(answers)

        def get(path, params=None):
            seen.append((path, dict(params or {})))
            return TRACK if path == "/flight-tracks" else next(answers)
        return get

    def test_uses_the_latest_completed_flight(self):
        seen = []
        with mock.patch("journeys.flight_tracks._get", side_effect=self.fake([FLIGHTS], seen)):
            routing.refresh_path(self.leg)
        self.assertEqual([path for path, _ in seen], ["/flight-summary/full", "/flight-tracks"])
        self.assertEqual(seen[0][1]["flights"], "CX156")
        self.assertEqual(seen[1][1], {"flight_id": "yesterday"})
        self.assertEqual((self.leg.path_source, self.leg.path_status), (Leg.PathSource.FLOWN, Leg.PathStatus.OK))
        self.assertIn("Real track of CX156 on 25 Oct 2026 (Flightradar24)", self.leg.path_message)
        self.assertEqual(self.leg.path[0], [-27.38, 153.12])  # starts and ends at the airports
        self.assertEqual(self.leg.path[1], [-27.3, 153.0])  # positions put in time order
        self.assertEqual(self.leg.path[-1], [22.31, 113.91])
        self.assertGreater(len(self.leg.path), 7)  # the 1,800 km gap was filled with an arc
        self.assertIsNotNone(Leg.objects.get(pk=self.leg.pk).path_updated)

    def test_tries_the_callsign_then_an_older_window(self):
        seen = []
        with mock.patch("journeys.flight_tracks._get", side_effect=self.fake([EMPTY, EMPTY, EMPTY, FLIGHTS], seen)):
            routing.refresh_path(self.leg)
        searches = [params for path, params in seen if path == "/flight-summary/full"]
        self.assertEqual([s.get("flights") or s.get("callsigns") for s in searches], ["CX156", "CPA156", "CX156", "CPA156"])
        self.assertEqual(searches[2]["flight_datetime_to"], searches[0]["flight_datetime_from"])  # the 14 days before
        self.assertEqual(self.leg.path_source, Leg.PathSource.FLOWN)

    def test_no_matching_flight_falls_back_to_an_arc(self):
        other_route = {"data": [summary("tpe", "2026-10-24T23:25:00Z", dest="TPE")]}
        with mock.patch("journeys.flight_tracks._get", side_effect=self.fake([other_route] * 4, [])):
            routing.refresh_path(self.leg)
        self.assertEqual(self.leg.path_source, Leg.PathSource.GREAT_CIRCLE)
        self.assertIn("No completed CX156 BNE→HKG flight", self.leg.path_message)

    def test_spots_a_sandbox_token(self):
        sample = {"data": [summary("380ce8ef", "2023-01-27T06:15:10", flight="SK1415", callsign="SAS1415")]}
        with mock.patch("journeys.flight_tracks._get", side_effect=self.fake([sample], [])):
            routing.refresh_path(self.leg)
        self.assertIn("this is a sandbox token", self.leg.path_message)
        self.assertEqual(self.leg.path_source, Leg.PathSource.GREAT_CIRCLE)

    def test_api_errors_are_explained(self):
        cases = [
            (401, b"", "rejected the API token"),
            (402, b"", "credits for this month have run out"),
            (400, b'{"details": "Invalid date range"}', "Invalid date range"),
        ]
        for code, body, expected in cases:
            error = urllib.error.HTTPError("https://fr24api.flightradar24.com/api/x", code, "Error", {}, io.BytesIO(body))
            with mock.patch("urllib.request.urlopen", side_effect=error), mock.patch("journeys.flight_tracks.time.sleep"):
                routing.refresh_path(self.leg)
            self.assertIn(expected, self.leg.path_message)
            self.assertEqual(self.leg.path_source, Leg.PathSource.GREAT_CIRCLE)

    def test_sends_the_token_and_api_version(self):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"data": []}')
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen, mock.patch("journeys.flight_tracks.time.sleep"):
            flight_tracks._get("/flight-summary/full", {"flights": "CX156"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(request.get_header("Accept-version"), "v1")
        self.assertTrue(request.full_url.startswith("https://fr24api.flightradar24.com/api/flight-summary/full?flights=CX156"))

    def test_fetched_by_the_background_job_only(self):
        with mock.patch("journeys.flight_tracks._get") as get:
            routing.refresh_path(self.leg, allow_network=False)
        get.assert_not_called()
        self.assertEqual(self.leg.path_status, Leg.PathStatus.PENDING)
        self.assertGreater(len(self.leg.path), 10)  # arc shown meanwhile

    def flown(self, days_old):
        Leg.objects.filter(pk=self.leg.pk).update(
            path=[[1, 1], [2, 2], [3, 3]], path_source=Leg.PathSource.FLOWN, path_status=Leg.PathStatus.OK,
            path_updated=timezone.now() - timedelta(days=days_old),
        )

    def test_job_refetches_tracks_before_the_30_day_limit(self):
        self.flown(days_old=26)
        RunJobs(stdout=io.StringIO()).expire_flight_tracks()
        leg = Leg.objects.get(pk=self.leg.pk)
        self.assertEqual((leg.path_source, leg.path_status), (Leg.PathSource.GREAT_CIRCLE, Leg.PathStatus.PENDING))
        self.assertNotEqual(leg.path[1], [2, 2])  # the Flightradar24 positions are gone

    @override_settings(FLIGHTRADAR24_API_TOKEN="", ADSBLOL_TRACKS=False)
    def test_job_drops_old_tracks_without_a_token(self):
        self.flown(days_old=26)
        RunJobs(stdout=io.StringIO()).expire_flight_tracks()
        leg = Leg.objects.get(pk=self.leg.pk)
        self.assertEqual((leg.path_source, leg.path_status), (Leg.PathSource.GREAT_CIRCLE, Leg.PathStatus.OK))

    def test_job_keeps_recent_tracks(self):
        self.flown(days_old=5)
        RunJobs(stdout=io.StringIO()).expire_flight_tracks()
        self.assertEqual(Leg.objects.get(pk=self.leg.pk).path, [[1, 1], [2, 2], [3, 3]])
