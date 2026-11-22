"""Booked and still to book (to_book.py): the Transport page's count and filter, and the Booked or To book
mark on flights and trains on the trip page, which visitors never see."""

from datetime import date, time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from journeys import to_book
from journeys.models import Leg, Trip

from .factories import sample_trip
from .test_plans_costs import PlannerCase

TODAY = date(2026, 11, 15)  # G388 on 2 Jan: its tickets went on sale on 5 Nov (58 days before)


def today(value=TODAY):
    return mock.patch("journeys.to_book.timezone.localdate", return_value=value)


class MarkTests(PlannerCase):
    def legs(self, **kwargs):
        return to_book.mark(list(self.trip.legs.order_by("depart_date")), self.trip, today=TODAY, **kwargs)

    def test_booked_planned_and_on_sale(self):
        flight, train = self.legs()
        self.assertEqual(flight.booking, {"booked": True, "opens": None, "on_sale": False, "sale": ""})
        self.assertEqual(train.booking, {"booked": False, "opens": date(2026, 11, 5), "on_sale": True, "sale": "On sale since Thu 5 Nov"})
        _, train = to_book.mark([flight, train], self.trip, today=date(2026, 11, 1))
        self.assertEqual((train.booking["on_sale"], train.booking["sale"]), (False, "On sale Thu 5 Nov"))

    def test_metro_and_walks_are_not_counted(self):
        Leg.objects.create(trip=self.trip, mode=Leg.Mode.METRO, origin=self.places["szn"], destination=self.places["szn"],
                           depart_date=date(2027, 1, 2), depart_time=time(8, 0))
        Leg.objects.create(trip=self.trip, mode=Leg.Mode.BOAT, origin=self.places["szn"], destination=self.places["szn"],
                           depart_date=date(2027, 1, 3))
        counts = to_book.summary(self.legs())
        self.assertEqual((counts["total"], counts["booked"], counts["to_book"], counts["on_sale"], counts["skipped"]), (3, 1, 2, 1, 1))
        self.assertEqual(counts["percent"], 33)
        self.assertEqual([(g["label"], g["booked"], g["total"]) for g in counts["groups"]],
                         [("Flights", 1, 1), ("Trains", 0, 1), ("Boats and road", 0, 1)])

    def test_only_the_modes_asked_for(self):
        Leg.objects.create(trip=self.trip, mode=Leg.Mode.BUS, origin=self.places["szn"], destination=self.places["szn"],
                           depart_date=date(2027, 1, 3))
        marked = {leg.mode: leg.booking for leg in self.legs(modes=(Leg.Mode.FLIGHT, Leg.Mode.TRAIN))}
        self.assertIsNone(marked["bus"])
        self.assertTrue(marked["flight"]["booked"])


class TransportPageTests(PlannerCase):
    def get(self, show=None):
        with today():
            return self.client.get(reverse("manage:legs", args=[self.trip.pk]), {"show": show} if show else {})

    def test_the_count(self):
        page = self.get().content.decode()
        self.assertIn("1 of 2 booked", page)
        self.assertIn("1 still to book", page)
        self.assertIn("1 train on sale now", page)
        self.assertIn('style="inline-size: 50%"', page)
        self.assertIn("On sale since Thu 5 Nov", page)

    def test_still_to_book_and_booked(self):
        response = self.get("to-book")
        self.assertEqual(response.context["show"], "to-book")
        self.assertEqual([leg.number for leg in response.context["legs"]], ["G388"])
        page = response.content.decode()
        self.assertIn('href="?show=to-book" aria-current="page"', page)
        self.assertIn("?next=", page)  # editing comes back to this list
        self.assertEqual([leg.number for leg in self.get("booked").context["legs"]], ["CX156"])
        self.assertEqual(len(self.get("anything").context["legs"]), 2)
        Leg.objects.update(status="booked")
        page = self.get("to-book").content.decode()
        self.assertIn("Nothing left to book.", page)
        self.assertIn("Everything's booked.", page)


class TripPageTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip(visibility=Trip.Visibility.PUBLIC)

    def page(self, url):
        with today():
            return self.client.get(url).content.decode()

    def test_the_owner_and_the_secret_link_see_what_is_booked(self):
        shared = self.page(reverse("journeys:shared_trip", args=[self.trip.share_token]))
        self.assertIn("✓</span> Booked", shared)
        self.assertIn("To book", shared)
        self.assertIn("Tickets have been on sale since Thu 5 Nov", shared)
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.assertIn("To book", self.page(reverse("journeys:trip", args=[self.trip.slug])))

    def test_visitors_never_do(self):
        page = self.page(reverse("journeys:trip", args=[self.trip.slug]))
        self.assertIn("Hong Kong", page)  # the public flight is there
        for text in ("Booked", "To book", "on sale"):
            self.assertNotIn(text, page)
