"""Where each night is spent comes from the stays (days' own "overnight city" is no longer used)."""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from journeys import ics, nights, presenters
from journeys.forms import DayForm
from journeys.models import Day, Place, Stay

from .factories import place, sample_trip


class NightsTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()  # one stay: Hong Kong 1 (private), the night of 1 Jan
        self.trip.end_date = date(2027, 1, 6)
        self.trip.save()

    def stay(self, label, first, last, city=None, **extra):
        return Stay.objects.create(trip=self.trip, label=label, city=city, check_in_date=date(2027, 1, first),
                                   check_out_date=date(2027, 1, last), **extra)

    def test_nights_and_stops_follow_the_stays(self):
        self.stay("Hong Kong 2", 2, 4, city=self.places["hongkong"])  # straight on from Hong Kong 1: one stop
        self.stay("Shenzhen", 5, 6, city=self.places["shenzhen"])  # after a night with no stay
        self.stay("Overlap", 3, 4, city=self.places["shenzhen"])  # the stay checked into first wins the night
        found = nights.by_night(self.trip)
        self.assertEqual([found[date(2027, 1, d)].label for d in (1, 2, 3)], ["Hong Kong 1", "Hong Kong 2", "Hong Kong 2"])
        self.assertNotIn(date(2027, 1, 4), found)  # nobody's booked that night
        stops = presenters.stops(self.trip)
        self.assertEqual([(s["name"], s["first"].day, s["nights"]) for s in stops], [("Hong Kong", 1, 3), ("Shenzhen", 5, 1)])

    def test_a_stay_without_a_city(self):
        hotel = place("The Peninsula", Place.Kind.HOTEL, 22.295, 114.172, city="Kowloon")
        unnamed = self.stay("Somewhere", 2, 3, location=hotel)
        self.assertEqual(nights.city_of(unnamed), ("Kowloon", "", None))  # the hotel's city by name, not its position
        self.assertEqual(nights.city_of(self.stay("Hut", 3, 4)), ("Hut", "", None))
        data = presenters.map_data(self.trip)
        self.assertEqual([s["name"] for s in data["stops"]], ["Hong Kong"])  # no city Place, so no map pin at the hotel
        self.assertEqual(presenters.summary(self.trip)["cities"], 3)  # Hong Kong, Kowloon and Hut
        stays_page = self.client.get(reverse("journeys:stays", args=[self.trip.slug]))
        self.assertContains(stays_page, "Kowloon")  # a private stay: its city, by name, for visitors…
        self.assertNotContains(stays_page, "The Peninsula")  # …never the hotel

    def test_days_own_overnight_city_is_ignored(self):
        day = Day.objects.get(trip=self.trip, date=date(2027, 1, 2))
        self.assertEqual(day.overnight, self.places["shenzhen"])  # still stored…
        page = self.client.get(reverse("journeys:trip", args=[self.trip.slug])).content.decode()
        self.assertNotIn("Shenzhen</span> <span class=\"local\">深圳", page)  # …but not a stop
        self.assertNotIn("overnight", DayForm().fields)
        calendar = ics.build(self.trip)
        self.assertIn("Overnight: Hong Kong", calendar.replace("\r\n ", ""))
        self.assertNotIn("Overnight: Shenzhen", calendar.replace("\r\n ", ""))

    def test_visitors_see_the_city_of_a_private_stay_never_the_hotel(self):
        stay = Stay.objects.get(label="Hong Kong 1")
        self.assertFalse(stay.is_public)
        page = self.client.get(reverse("journeys:trip", args=[self.trip.slug]))
        self.assertContains(page, '<span class="stop-name">Hong Kong <span class="local">香港</span></span>', html=True)
        for secret in ("SECRET-HOTEL", "SECRET-ADDRESS", "SECRETBOOKING"):
            self.assertNotContains(page, secret)
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        planner = self.client.get(reverse("manage:trip", args=[self.trip.pk]))
        self.assertContains(planner, "Overnight in Hong Kong")


class StayDetailsTests(TestCase):
    """Each stay carries its days, weather, check-in times and what's planned; the map labels hotels with their city."""

    def setUp(self):
        from datetime import time

        from journeys.models import Item, WeatherCache

        self.trip, self.places = sample_trip()  # Hong Kong 1 (private) on 1 Jan; Harbour walk (public) that day
        self.stay = Stay.objects.get(trip=self.trip)
        self.hotel = place("SECRET-HOTEL-PIN", Place.Kind.HOTEL, 22.301, 114.17, city="Hong Kong", country="HK", tz="Asia/Hong_Kong")
        Stay.objects.filter(pk=self.stay.pk).update(check_in_time=time(15, 0), check_out_time=time(11, 0), location=self.hotel)
        Item.objects.create(day=self.trip.days.get(date=date(2027, 1, 1)), title="Secret dinner", is_public=False)
        WeatherCache.objects.create(place=self.places["hongkong"], kind=WeatherCache.Kind.TYPICAL, start_date=date(2027, 1, 1),
                                    end_date=date(2027, 1, 1), data={"summary": {"min": 13, "max": 18, "rain_share": 10}})

    def test_the_planner_stays_page(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        page = self.client.get(reverse("manage:stays", args=[self.trip.pk]))
        self.assertContains(page, "Typical 13–18 °C · rain 10% of days")
        self.assertContains(page, "Day 1–2")
        self.assertContains(page, "2 activities: ")  # private ones too, for you
        self.assertContains(page, "Secret dinner")

    def test_visitors_see_a_private_stays_city_and_weather_only(self):
        page = self.client.get(reverse("journeys:stays", args=[self.trip.slug]))
        self.assertContains(page, "Typical 13–18 °C · rain 10% of days")
        self.assertContains(page, "1 activity</a>: Harbour walk")  # the public one only
        self.assertContains(page, "The hotel and how to get there are private.")
        for secret in ("SECRET-HOTEL", "Secret dinner", "15:00", "Hong Kong 1"):
            self.assertNotContains(page, secret)  # no hotel, private activity, check-in time or the stay's own label
        Stay.objects.filter(pk=self.stay.pk).update(is_public=True)
        public = self.client.get(reverse("journeys:stays", args=[self.trip.slug]))
        self.assertContains(public, "Check in 15:00 · Check out 11:00")  # shown once the stay is public
        self.assertNotContains(public, "SECRET-HOTEL")

    def test_the_map_labels_the_hotel_with_its_city(self):
        full = presenters.map_data(self.trip, private=True)
        pin = full["stays"][0]
        self.assertEqual((pin["index"], pin["city"], pin["nights"]), (1, "Hong Kong", 1))
        self.assertEqual(full["stops"], [])  # no separate city marker where the hotel is pinned
        visitor = presenters.map_data(self.trip)
        self.assertEqual([s["name"] for s in visitor["stops"]], ["Hong Kong"])  # visitors: the city marker, never the hotel
        self.assertNotIn("SECRET-HOTEL-PIN", str(visitor))
        self.assertTrue(all(stay.get("lat") is None for stay in visitor["stays"]))
