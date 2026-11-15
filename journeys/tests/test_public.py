import json

from django.test import TestCase
from django.urls import reverse

from journeys.models import Leg, Trip

from .factories import sample_trip

SECRETS = ["SECRETREF1", "SECRETBOOKING", "SECRET-HOTEL", "SECRET-ADDRESS", "SECRET-ITEM-NOTE",
           "SECRET-LEG-NOTE", "SECRET-DAY-NOTE", "SECRET-TRIP-NOTE", "Hidden plan", "G388", "CX156"]


class VisibilityTests(TestCase):
    def test_home_shows_holding_message_without_public_trips(self):
        sample_trip(visibility=Trip.Visibility.PRIVATE)
        response = self.client.get("/")
        self.assertContains(response, "This page is under development.")
        self.assertNotContains(response, "Test Trip")

    def test_home_lists_public_trips(self):
        sample_trip()
        self.assertContains(self.client.get("/"), "Test Trip")

    def test_private_trip_is_not_reachable(self):
        trip, _ = sample_trip(visibility=Trip.Visibility.PRIVATE)
        self.assertEqual(self.client.get(reverse("journeys:trip", args=[trip.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse("journeys:shared_trip", args=[trip.share_token])).status_code, 404)
        self.assertEqual(self.client.get(reverse("journeys:map", args=[trip.slug])).status_code, 404)

    def test_unlisted_trip_only_by_secret_link(self):
        trip, _ = sample_trip(visibility=Trip.Visibility.UNLISTED)
        self.assertEqual(self.client.get(reverse("journeys:trip", args=[trip.slug])).status_code, 404)
        response = self.client.get(reverse("journeys:shared_trip", args=[trip.share_token]))
        self.assertContains(response, "Test Trip")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertNotContains(self.client.get("/"), "Test Trip")


class PrivacyTests(TestCase):
    def setUp(self):
        self.trip, _ = sample_trip()

    def test_legs_read_city_to_city_with_hubs_underneath(self):
        response = self.client.get(reverse("journeys:trip", args=[self.trip.slug]))
        self.assertContains(response, '<span class="journey-card__route">Brisbane → Hong Kong</span>', html=True)
        self.assertContains(response, '<span class="journey-card__route">Hong Kong → Shenzhen</span>', html=True)
        self.assertContains(response, '<span class="dayrow__title">Hong Kong → Shenzhen</span>', html=True)
        self.assertContains(response, "Brisbane International Airport")
        self.assertContains(response, '<span class="pinyin">(Shenzhenbei)</span>', html=True)
        data = self.client.get(reverse("journeys:map", args=[self.trip.slug])).json()
        train = next(leg for leg in data["legs"] if leg["mode"] == "train")
        self.assertEqual((train["from_city"], train["to_city"], train["to_pinyin"]), ("Hong Kong", "Shenzhen", "Shenzhenbei"))

    def test_public_page_shows_route_and_story_only(self):
        response = self.client.get(reverse("journeys:trip", args=[self.trip.slug]))
        self.assertNotContains(response, "Harbour walk")  # the itinerary is transport only
        self.assertContains(self.client.get(reverse("journeys:activities", args=[self.trip.slug])), "Harbour walk")
        self.assertContains(response, "Hong Kong West Kowloon")
        self.assertContains(response, "深圳北")
        for secret in SECRETS:
            self.assertNotContains(response, secret)

    def test_public_map_data_has_no_private_fields(self):
        data = self.client.get(reverse("journeys:map", args=[self.trip.slug])).json()
        self.assertEqual({leg["mode"] for leg in data["legs"]}, {"flight", "train"})
        self.assertTrue(all("number" not in leg for leg in data["legs"]))
        # Stops come from the stays: Hong Kong 1 is private, but every viewer sees its city (never the hotel).
        # Day 2's own "overnight city" (Shenzhen) has no stay behind it, so it isn't a stop.
        self.assertEqual([s["name"] for s in data["stops"]], ["Hong Kong"])
        self.assertNotIn("SECRET-HOTEL", json.dumps(data))
        flight = next(leg for leg in data["legs"] if leg["mode"] == "flight")
        self.assertGreater(len(flight["path"]), 10)  # great-circle arc, not a straight line

    def test_public_calendar(self):
        response = self.client.get(reverse("journeys:calendar", args=[self.trip.slug]))
        body = response.content.decode().replace("\r\n ", "")  # unfold long lines
        self.assertEqual(response["Content-Type"], "text/calendar; charset=utf-8")
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("SUMMARY:Flight Brisbane → Hong Kong", body)
        self.assertIn("Brisbane International Airport → Hong Kong International Airport", body)
        self.assertIn("DTSTART:20261231T150000Z", body)  # 00:50 Brisbane time, rounded up to 01:00, is 15:00 UTC the day before
        self.assertIn("SUMMARY:Flight Brisbane → Hong Kong (Cathay Pacific)", body)
        for secret in SECRETS:
            self.assertNotIn(secret, body)

    def test_private_calendar_has_everything(self):
        body = self.client.get(reverse("journeys:private_calendar", args=[self.trip.calendar_token])).content.decode().replace("\r\n ", "")
        self.assertIn("SECRETREF1", body)
        self.assertIn("SECRET-HOTEL", body)
        self.assertIn("CX156", body)

    def test_wrong_calendar_token(self):
        self.assertEqual(self.client.get(reverse("journeys:private_calendar", args=["nope"])).status_code, 404)

    def test_hidden_dates(self):
        self.trip.hide_dates = True
        self.trip.save()
        response = self.client.get(reverse("journeys:trip", args=[self.trip.slug]))
        self.assertContains(response, "Day 1")
        self.assertNotContains(response, "2027")
        self.assertEqual(self.client.get(reverse("journeys:calendar", args=[self.trip.slug])).status_code, 404)


class HighSpeedRailTests(TestCase):
    def setUp(self):
        self.trip, _places = sample_trip()

    def test_high_speed_trains_are_red_without_showing_the_number(self):
        legs = self.client.get(reverse("journeys:map", args=[self.trip.slug])).json()["legs"]
        train = next(leg for leg in legs if leg["mode"] == "train")
        self.assertTrue(train["high_speed"])  # G388
        self.assertEqual(train["mode_label"], "High-speed train")
        self.assertNotIn("number", train)
        self.assertFalse(next(leg for leg in legs if leg["mode"] == "flight")["high_speed"])
        page = self.client.get(reverse("journeys:trip", args=[self.trip.slug]))
        self.assertContains(page, '<span class="mode mode--hsr">High-speed train</span>', html=True)
        self.assertContains(page, "High-speed rail")  # in the map key

    def test_other_trains_stay_blue(self):
        self.trip.legs.filter(mode="train").update(number="K1234")
        legs = self.client.get(reverse("journeys:map", args=[self.trip.slug])).json()["legs"]
        self.assertFalse(next(leg for leg in legs if leg["mode"] == "train")["high_speed"])


class TemplateCommentTests(TestCase):
    def test_no_template_comments_leak_onto_pages(self):
        """{# … #} only works on one line; a wrapped one shows up as text on the page."""
        trip, _places = sample_trip()
        for name in ("trip", "stays", "activities"):
            page = self.client.get(reverse(f"journeys:{name}", args=[trip.slug])).content.decode()
            self.assertNotIn("{#", page, name)
            self.assertNotIn("#}", page, name)

    def test_template_comments_close_on_the_same_line(self):
        from pathlib import Path

        from django.conf import settings

        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if "{#" in line:
                    self.assertIn("#}", line, f"{path.name}:{number} – use {{% comment %}} for comments over several lines")


class ItineraryTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()

    def test_travel_days_and_quiet_day_bands(self):
        from journeys import presenters

        rows, journeys = presenters.itinerary(self.trip)
        self.assertEqual([row.get("number") for row in rows if not row["band"]], [1, 2])
        day1, day2, band = rows
        self.assertEqual((day1["title"], day1["summary"], day1["kinds"]), ("Brisbane → Hong Kong", "1 flight", ["flight"]))
        self.assertEqual((day2["summary"], day2["kinds"]), ("1 high-speed train", ["hsr"]))
        self.assertEqual((band["band"], band["range"], band["text"]), (True, "3", "No travel"))
        self.assertEqual(len(journeys), 2)

    def test_a_journey_without_a_time_follows_the_one_it_connects_with(self):
        from journeys import presenters
        from journeys.models import Leg

        train = Leg.objects.get(number="G388")
        Leg.objects.create(trip=self.trip, mode=Leg.Mode.SHUTTLE, origin=self.places["szn"], destination=self.places["shenzhen"],
                           depart_date=train.depart_date, is_public=True)
        rows, _journeys = presenters.itinerary(self.trip)
        day2 = next(row for row in rows if row.get("number") == 2)
        self.assertEqual([leg.mode for leg in day2["legs"]], ["train", "shuttle"])
        self.assertEqual(day2["summary"], "High-speed train, shuttle")

    def test_itineraries_have_no_check_links(self):
        from django.contrib.auth import get_user_model

        for signed_in in (False, True):
            if signed_in:
                self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
            with self.subTest(signed_in=signed_in):
                page = self.client.get(reverse("journeys:trip", args=[self.trip.slug])).content.decode()
                for link in ("google.com/travel/flights", "gaotie.net", "trip.com/flights", "Check times and fares"):
                    self.assertNotIn(link, page)
        planner = self.client.get(reverse("manage:trip", args=[self.trip.pk])).content.decode()
        self.assertNotIn("google.com/travel/flights", planner)  # the planner's itinerary too
        transport = self.client.get(reverse("manage:legs", args=[self.trip.pk])).content.decode()
        self.assertIn("google.com/travel/flights", transport)  # kept where transport is added and edited
        self.assertIn("gaotie.net", transport)
        flight = Leg.objects.get(number="CX156")
        self.assertContains(self.client.get(reverse("manage:leg_edit", args=[self.trip.pk, flight.pk])), "Trip.com")

    def test_owner_gets_the_public_switch_in_the_journey_panel(self):
        from django.contrib.auth import get_user_model

        url = reverse("journeys:trip", args=[self.trip.slug])
        self.assertNotContains(self.client.get(url), 'role="switch"')
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        page = self.client.get(url)
        self.assertContains(page, 'role="switch"', count=2)  # one per journey
