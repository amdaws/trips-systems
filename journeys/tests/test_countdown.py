from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from journeys import presenters
from journeys.forms import TripForm
from journeys.models import Leg

from .factories import sample_trip

BRISBANE = ZoneInfo("Australia/Brisbane")


class CountdownTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()  # first flight: CX156 from Brisbane, 1 Jan 2027 00:50

    def test_counts_down_to_the_first_departure(self):
        now = datetime(2026, 12, 30, 22, 50, 30, tzinfo=BRISBANE)
        found = presenters.countdown(self.trip, full=True, now=now)
        self.assertEqual((found["days"], found["hours"], found["minutes"], found["seconds"]), (1, 1, 59, 30))
        self.assertEqual(found["label"], "until the trip starts")
        self.assertEqual(found["when"], "Fri 1 Jan 2027, 00:50 Brisbane time")
        visitor = presenters.countdown(self.trip, full=False, now=now)  # rounded up to the quarter hour, as on the itinerary
        self.assertEqual((visitor["hours"], visitor["minutes"], visitor["when"]), (2, 9, "Fri 1 Jan 2027, about 01:00 Brisbane time"))
        page = self.client.get(reverse("journeys:trip", args=[self.trip.slug]))
        self.assertContains(page, 'data-countdown="')

    def test_your_own_time_and_words(self):
        form = TripForm({**{f: getattr(self.trip, f) for f in ("title", "slug", "start_date", "end_date", "summary", "visibility", "notes")},
                         "train_ticket_window": 58, "show_countdown": "on", "countdown_to": "2026-12-31T18:30", "countdown_label": "until we fly"},
                        instance=self.trip)
        self.assertTrue(form.is_valid(), form.errors)
        trip = form.save()
        self.assertEqual(trip.countdown_to, datetime(2026, 12, 31, 18, 30, tzinfo=BRISBANE))  # Brisbane time, whatever the server's
        found = presenters.countdown(trip, full=True, now=datetime(2026, 12, 31, 18, 0, tzinfo=BRISBANE))
        self.assertEqual((found["days"], found["minutes"], found["label"]), (0, 30, "until we fly"))
        self.assertEqual(TripForm(instance=trip).initial["countdown_to"], datetime(2026, 12, 31, 18, 30))  # shown as typed

    def test_during_and_after_the_trip(self):
        during = presenters.countdown(self.trip, full=False, now=datetime(2027, 1, 2, 12, 0, tzinfo=BRISBANE))
        self.assertEqual((during["underway"], during["day"], during["of"]), (True, 2, 3))
        self.assertIsNone(presenters.countdown(self.trip, full=False, now=datetime(2027, 1, 4, 1, 0, tzinfo=BRISBANE)))

    def test_hidden_when_switched_off_or_dates_are_hidden(self):
        now = datetime(2026, 12, 1, tzinfo=BRISBANE)
        self.trip.hide_dates = True
        self.trip.save()
        self.assertIsNone(presenters.countdown(self.trip, full=False, now=now))  # it would give the date away
        self.assertIsNotNone(presenters.countdown(self.trip, full=True, now=now))
        self.trip.show_countdown = False
        self.trip.save()
        self.assertIsNone(presenters.countdown(self.trip, full=True, now=now))

    def test_without_transport_it_counts_to_day_one(self):
        Leg.objects.filter(trip=self.trip).delete()
        found = presenters.countdown(self.trip, full=False, now=datetime(2026, 12, 31, 23, 0, tzinfo=BRISBANE))
        self.assertEqual((found["days"], found["hours"]), (0, 1))

    def test_settings_page_has_the_fields(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        page = self.client.get(reverse("manage:trip_settings", args=[self.trip.pk]))
        self.assertContains(page, 'type="datetime-local"')
        self.assertContains(page, "In Brisbane time.")
