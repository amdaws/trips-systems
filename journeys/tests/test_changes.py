from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from journeys import changes
from journeys.models import Item, Leg, Stay, TripChange

from .factories import sample_trip


class RecentlyUpdatedTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()
        self.page = reverse("journeys:trip", args=[self.trip.slug])
        self.train = Leg.objects.get(number="G388")

    def settle(self):
        """Make everything so far older than the list goes back."""
        TripChange.objects.update(created_at=timezone.now() - timedelta(days=40))

    def lines(self, full):
        urls = {"trip": "/t/", "stays": "/t/stays/", "activities": "/t/activities/"}
        return [(line["action"], line["label"], line["title"]) for line in changes.recent(self.trip, full, urls)]

    def sign_in(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))

    def test_visitors_see_public_additions_only_without_numbers_or_hotels(self):
        response = self.client.get(self.page)
        self.assertContains(response, "<strong>3</strong> recent updates")  # two journeys and the walk
        self.assertContains(response, f'href="{self.page}#journey-{self.train.pk}"')
        self.assertNotContains(response, "Harbour walk")  # the itinerary stays transport only…
        self.assertContains(response, "1 activity update →")  # …and points to the Activities page
        activities = self.client.get(reverse("journeys:activities", args=[self.trip.slug]))
        self.assertContains(activities, f'href="{reverse("journeys:activities", args=[self.trip.slug])}#activity-')
        for page in (response, activities):
            for secret in ("G388", "CX156", "Hidden plan", "SECRET-HOTEL", "Hong Kong 1"):
                self.assertNotContains(page, secret)

    def test_owner_sees_everything(self):
        lines = self.lines(full=True)
        self.assertIn(("Added", "High-speed train G388", "Hong Kong → Shenzhen"), lines)
        self.assertIn(("Added", "Activity", "Hidden plan"), lines)
        self.assertIn(("Added", "Stay", "Hong Kong 1"), lines)  # by its label, never the hotel
        self.sign_in()
        self.assertContains(self.client.get(reverse("journeys:activities", args=[self.trip.slug])), "Hidden plan")
        self.assertContains(self.client.get(self.page), "2 activity updates →")

    def test_changes_are_described(self):
        self.settle()
        self.train.depart_time, self.train.arrive_time = time(8, 5), time(8, 30)
        self.train.save()
        self.assertEqual(self.lines(full=False), [("Changed", "High-speed train", "Hong Kong → Shenzhen")])
        line = changes.recent(self.trip, False, {"trip": "/t/", "stays": "", "activities": ""})[0]
        self.assertEqual((line["details"], line["date"]), (["times"], "Sat 2 Jan"))

    def test_private_details_only_show_the_owner(self):
        self.settle()
        stay = Stay.objects.get()
        stay.is_public = True
        stay.save()
        self.settle()
        stay.name = "Another Hotel"
        stay.save()
        self.assertEqual(self.lines(full=False), [])  # visitors never see hotel names, so nothing changed for them
        self.assertEqual(self.lines(full=True), [("Changed", "Stay", "Hong Kong 1")])
        line = changes.recent(self.trip, True, {"trip": "", "stays": "/t/stays/", "activities": ""})[0]
        self.assertEqual(line["details"], ["property"])

    def test_made_public_is_new_to_visitors_and_made_private_is_gone(self):
        self.settle()
        hidden = Item.objects.get(title="Hidden plan")
        changes.record_updates(Item.objects.filter(pk=hidden.pk), is_public=True)
        changes.record_updates(Leg.objects.filter(pk=self.train.pk), is_public=False)
        self.assertCountEqual(self.lines(full=False), [
            ("Added", "Activity", "Hidden plan"), ("Removed", "High-speed train", "Hong Kong → Shenzhen"),
        ])
        self.assertCountEqual(self.lines(full=True), [
            ("Made public", "Activity", "Hidden plan"), ("Made private", "High-speed train G388", "Hong Kong → Shenzhen"),
        ])

    def test_toggles_and_bulk_actions_are_recorded(self):
        self.settle()
        self.sign_in()
        self.client.post(reverse("manage:leg_toggle", args=[self.trip.pk, self.train.pk]))
        walk = Item.objects.get(title="Harbour walk")
        day3 = self.trip.days.get(date__day=3)
        self.client.post(reverse("manage:activities_bulk", args=[self.trip.pk]), {"items": [walk.pk], "action": "move", "day": day3.pk})
        lines = changes.recent(self.trip, True, {"trip": "", "stays": "", "activities": ""})
        self.assertEqual({(line["kind"], line["action"]) for line in lines}, {("leg", "Made private"), ("item", "Changed")})
        self.assertIn("day", next(line for line in lines if line["kind"] == "item")["details"])

    def test_quiet_changes_and_short_lived_plans_are_left_out(self):
        self.settle()
        self.train.path_message = "Traced along the railway"  # background route work isn't news
        self.train.save()
        extra = Item.objects.create(day=self.trip.days.first(), title="Maybe a museum", is_public=True)
        extra.delete()  # added and removed again: nothing to tell
        self.assertEqual(self.lines(full=True), [])
        self.assertEqual(self.lines(full=False), [])

    def test_made_private_shows_visitors_only_what_they_saw(self):
        self.settle()
        self.train.destination = self.places["hkg"]
        self.train.is_public = False
        self.train.save()  # moved and hidden in one edit: visitors mustn't learn the new route
        line = changes.recent(self.trip, False, {"trip": "", "stays": "", "activities": ""})[0]
        self.assertEqual((line["action"], line["title"]), ("Removed", "Hong Kong → Shenzhen"))

    def test_removals_show_without_a_link(self):
        self.settle()
        self.train.delete()
        line = changes.recent(self.trip, False, {"trip": "/t/", "stays": "", "activities": ""})[0]
        self.assertEqual((line["action"], line["link"]), ("Removed", None))

    def test_deleting_a_trip_takes_its_list(self):
        self.trip.delete()
        self.assertFalse(TripChange.objects.exists())
