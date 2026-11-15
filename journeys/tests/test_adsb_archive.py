import gzip
import io
import json
import tarfile
import urllib.error
from datetime import date
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from journeys import adsb_archive, routing
from journeys.management.commands.run_jobs import Command as RunJobs
from journeys.models import Airline, Leg, Place, Trip

from .factories import place

HKG, TPE, NGO = (22.31, 113.91), (25.08, 121.23), (35.25, 136.81)


def along(a, b, count, start, step=150):
    """Readsb trace points from a to b, `step` seconds apart."""
    return [[start + i * step, a[0] + (b[0] - a[0]) * i / (count - 1), a[1] + (b[1] - a[1]) * i / (count - 1),
             30000, 450, 70, 0, 0, None] for i in range(count)]


def cathay_day():
    """One aircraft's day, shaped like a readsb trace_full file: CX530 Hong Kong-Taipei-Nagoya."""
    points = along(HKG, TPE, 30, 0)
    points[0][8] = {"flight": "CPA530  "}
    points[15:15] = [[2255, 10.0, 100.0, 30000, 450, 70, 0, 0, None]]  # a bad position (Thailand, 5 s later)
    points += along(TPE, NGO, 25, 6000)  # onwards under the same flight number
    return {"icao": "780a94", "r": "B-LBJ", "t": "A333", "timestamp": 1792800000.0, "trace": points}


def archive(*aircraft):
    """A day's archive (an uncompressed tar of gzipped traces, after a heatmap) in two parts."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        files = [("./heatmap/00.bin", b"\0" * 5000)]
        files += [(f"./traces/{a['icao'][-2:]}/trace_full_{a['icao']}.json", gzip.compress(json.dumps(a).encode())) for a in aircraft]
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    return [data[:len(data) // 2], data[len(data) // 2:]]


@override_settings(ADSBLOL_TRACKS=True, FLIGHTRADAR24_API_TOKEN="")
class ArchiveTests(TestCase):
    def setUp(self):
        Airline.objects.create(code="CX", icao="CPA", name="Cathay Pacific")
        self.hkg = place("Hong Kong International Airport", Place.Kind.AIRPORT, *HKG, code="HKG", country="HK", tz="Asia/Hong_Kong")
        self.tpe = place("Taoyuan International Airport", Place.Kind.AIRPORT, *TPE, code="TPE", country="TW", tz="Asia/Taipei")
        self.trip = Trip.objects.create(title="T", slug="t", start_date=date(2027, 1, 1), end_date=date(2027, 1, 31),
                                        visibility=Trip.Visibility.PUBLIC)
        self.out = self.flight("CX530", self.hkg, self.tpe)
        self.back = self.flight("CX461", self.tpe, self.hkg)

    def flight(self, number, origin, destination):
        leg = Leg.objects.create(trip=self.trip, mode="flight", number=number, origin=origin, destination=destination,
                                 depart_date=date(2027, 1, 3))
        routing.refresh_path(leg, allow_network=False)
        return leg

    def run_job(self, parts):
        day = (date(2026, 10, 24), ["https://example.test/day.tar.aa", "https://example.test/day.tar.ab"])
        with mock.patch("journeys.adsb_archive.latest_day", return_value=day), \
                mock.patch("urllib.request.urlopen", side_effect=[io.BytesIO(p) for p in parts]) as urlopen:
            RunJobs(stdout=io.StringIO()).trace_flights()
        return urlopen

    def test_flights_wait_for_the_background_job(self):
        self.assertEqual(self.out.path_status, Leg.PathStatus.PENDING)
        self.assertGreater(len(self.out.path), 10)  # an arc meanwhile

    def test_one_read_of_the_archive_for_every_waiting_flight(self):
        urlopen = self.run_job(archive(cathay_day()))
        self.assertEqual(urlopen.call_count, 2)  # each part once, for both flights
        out = Leg.objects.get(pk=self.out.pk)
        self.assertEqual((out.path_source, out.path_status), (Leg.PathSource.ARCHIVE, Leg.PathStatus.OK))
        self.assertEqual(out.path_message, "Real track of CPA530 on 24 Oct 2026 (adsb.lol).")
        self.assertEqual(out.path[0], list(HKG))
        self.assertEqual(out.path[-1], list(TPE))
        self.assertFalse(any(lat < 20 for lat, lon in out.path))  # the bad position was dropped
        self.assertFalse(any(lon > 122 for lat, lon in out.path))  # stops at Taipei, not Nagoya
        back = Leg.objects.get(pk=self.back.pk)
        self.assertEqual((back.path_source, back.path_status), (Leg.PathSource.GREAT_CIRCLE, Leg.PathStatus.OK))
        self.assertIn("CX461 isn't in adsb.lol's archive for 24 Oct 2026", back.path_message)

    def test_the_other_direction_is_not_used(self):
        reverse_day = cathay_day()
        reverse_day["trace"] = along(TPE, HKG, 30, 0)
        reverse_day["trace"][0][8] = {"flight": "CPA530"}
        self.run_job(archive(reverse_day))
        self.assertEqual(Leg.objects.get(pk=self.out.pk).path_source, Leg.PathSource.GREAT_CIRCLE)

    def test_an_unreadable_archive_leaves_arcs_without_retrying(self):
        day = (date(2026, 10, 24), ["https://example.test/day.tar.aa"])
        with mock.patch("journeys.adsb_archive.latest_day", return_value=day), \
                mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            RunJobs(stdout=io.StringIO()).trace_flights()
        out = Leg.objects.get(pk=self.out.pk)
        self.assertEqual((out.path_source, out.path_status), (Leg.PathSource.GREAT_CIRCLE, Leg.PathStatus.OK))
        self.assertIn("Couldn't read adsb.lol's archive", out.path_message)
        self.assertGreater(len(out.path), 10)

    def test_newest_published_day(self):
        releases = [
            {"tag_name": "v2026.10.24-planes-readsb-staging-0", "assets": [{"name": "x.tar.aa", "browser_download_url": "u/staging"}]},
            {"tag_name": "v2026.10.24-planes-readsb-prod-0", "assets": [
                {"name": "v.tar.ab", "browser_download_url": "u/ab"}, {"name": "v.tar.aa", "browser_download_url": "u/aa"}]},
            {"tag_name": "v2026.10.23-planes-readsb-prod-0", "assets": [{"name": "v.tar.aa", "browser_download_url": "u/old"}]},
            {"tag_name": "v2026.10.24-planes-readsb-mlatonly-0", "assets": [{"name": "m.tar", "browser_download_url": "u/mlat"}]},
        ]
        with mock.patch("journeys.adsb_archive.get_json", return_value=releases):
            self.assertEqual(adsb_archive.latest_day(), (date(2026, 10, 24), ["u/aa", "u/ab"]))

    def test_new_year_falls_back_to_last_years_archive(self):
        missing = urllib.error.HTTPError("https://api.github.com", 404, "Not Found", {}, None)
        last_year = [{"tag_name": "v2026.12.31-planes-readsb-prod-0", "assets": [{"name": "v.tar.aa", "browser_download_url": "u/aa"}]}]
        with mock.patch("journeys.adsb_archive.get_json", side_effect=[missing, last_year]), \
                mock.patch("django.utils.timezone.now", return_value=timezone_at(2027, 1, 1)):
            self.assertEqual(adsb_archive.latest_day(), (date(2026, 12, 31), ["u/aa"]))

    def test_public_page_credits_adsb_lol(self):
        self.run_job(archive(cathay_day()))
        page = reverse("journeys:trip", args=[self.trip.slug])
        self.assertNotContains(self.client.get(page), "adsb.lol</a> (ODbL)")  # visitors: only public flights' tracks count
        self.trip.legs.update(is_public=True)
        response = self.client.get(page)
        self.assertContains(response, '<a href="https://adsb.lol/">adsb.lol</a> (ODbL)')
        self.assertNotContains(response, "Flightradar24")

    def test_retrace_queues_the_flight_again(self):
        self.run_job(archive(cathay_day()))
        leg = Leg.objects.get(pk=self.out.pk)
        routing.refresh_path(leg, allow_network=False)
        self.assertEqual(leg.path_status, Leg.PathStatus.PENDING)


def timezone_at(*day):
    from datetime import UTC, datetime

    return datetime(*day, 12, tzinfo=UTC)
