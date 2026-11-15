import io
import json
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from journeys import activity_import, china_coords, metro, stay_import
from journeys.models import Item, Leg, Place, Stay

from .factories import place, sample_trip
from .test_metro import OSRM, fake_overpass, fake_page


class ChinaCoordsTests(TestCase):
    def test_mainland_positions_lose_the_offset(self):
        # Trip.com's Chongqing hotel: about 480 m out before the correction (checked against OpenStreetMap landmarks).
        self.assertEqual(china_coords.to_wgs84(29.560013, 106.580469, "gcj02"), (29.562899, 106.576781))

    def test_hong_kong_macau_and_taiwan_are_left_alone(self):
        for spot in ((22.3057, 114.1719), (22.14081, 113.56139), (25.03, 121.56)):
            self.assertEqual(china_coords.to_wgs84(*spot, "gcj02"), spot)

    def test_baidu_positions(self):
        lat, lon = china_coords.to_wgs84(29.566, 106.587, "bd09")
        self.assertAlmostEqual(lat, 29.5629, places=2)
        self.assertAlmostEqual(lon, 106.5768, places=2)

    def test_tripcom_pages_are_on_the_offset_system(self):
        self.assertEqual(stay_import.datum('"mapType":"gg","lat":"29.56"', "https://www.trip.com/hotels/detail/?hotelId=1"), "gcj02")
        self.assertEqual(stay_import.datum("", "https://www.booking.com/hotel/x.html"), "wgs84")


class LinkTests(TestCase):
    def test_google_links_are_refused(self):
        # No Google services for maps or routes: short links aren't followed, and pages aren't fetched.
        for url in ("https://maps.app.goo.gl/abc123", "https://www.google.com/maps/place/Hongya+Cave/@29.5633,106.5792,17z",
                    "https://www.google.com.au/maps?q=-27.47,153.02", "https://g.page/somewhere"):
            with self.subTest(url=url), self.assertRaisesMessage(stay_import.PageError, "Google links aren't used"):
                activity_import.read_link(url)
        with self.assertRaisesMessage(stay_import.PageError, "Google links aren't used"):
            stay_import.fetch_page("https://www.google.com/travel/hotels/x")
        self.assertEqual(activity_import.from_map_link("https://www.google.com/maps/search/?api=1&query=22.2759,114.1455"), {})

    def test_other_map_links(self):
        self.assertEqual(activity_import.from_map_link("https://www.openstreetmap.org/#map=17/22.29330/114.16940")["lat"], 22.2933)
        amap = activity_import.from_map_link("https://uri.amap.com/marker?position=106.5795,29.5631&name=洪崖洞")
        self.assertEqual((amap["name"], amap["lat"]), ("洪崖洞", china_coords.to_wgs84(29.5631, 106.5795, "gcj02")[0]))
        self.assertEqual(activity_import.from_map_link("https://maps.apple.com/?ll=22.283,114.159&q=Central%20Pier")["name"], "Central Pier")
        self.assertEqual(activity_import.from_map_link("https://example.com/maps/place/x"), {})

    def test_attraction_page(self):
        page = """<html><head><title>Hongya Cave | Trip.com</title>
        <script type="application/ld+json">{"@context": "https://schema.org", "@type": "TouristAttraction", "name": "Hongya Cave",
        "address": {"@type": "PostalAddress", "streetAddress": "88 Jiabin Road", "addressLocality": "Chongqing"},
        "geo": {"@type": "GeoCoordinates", "latitude": 29.5631, "longitude": 106.5795}}</script></head></html>"""
        found = activity_import.extract(page, "https://example.org/hongya")
        self.assertEqual((found["name"], found["address"]), ("Hongya Cave", "88 Jiabin Road, Chongqing"))
        self.assertEqual(found["lat"], 29.5631)  # a sight's own site: already on the world system

    def test_wikipedia_article(self):
        page = '<html><head><meta property="og:title" content="Victoria Peak - Wikipedia"></head><script>"wgCoordinates":{"lat":22.275556,"lon":114.143889}</script></html>'
        found = activity_import.extract(page, "https://en.wikipedia.org/wiki/Victoria_Peak")
        self.assertEqual((found["name"], found["lat"], found["lon"]), ("Victoria Peak", 22.275556, 114.143889))


class ActivityTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()
        self.stay = Stay.objects.get(trip=self.trip)
        self.stay.location = place("Harbour Hotel", Place.Kind.HOTEL, 22.2990, 114.1720, country="HK", tz="Asia/Hong_Kong")
        self.stay.save()
        self.sight = place("Kowloon Park Gate", Place.Kind.ATTRACTION, 22.3050, 114.1660, country="HK", tz="Asia/Hong_Kong")
        self.item = Item.objects.create(day=self.trip.days.get(date=date(2027, 1, 1)), title="Kowloon Park", place=self.sight, is_public=True)
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))

    def run_job(self):
        with mock.patch("journeys.metro._fetch_page", side_effect=fake_page), \
             mock.patch("journeys.metro.overpass", side_effect=fake_overpass), \
             mock.patch("journeys.metro.get_json", return_value=OSRM), \
             mock.patch("journeys.metro.geocode.search", return_value=[]), \
             mock.patch("journeys.routing.refresh_path"), \
             mock.patch("journeys.management.commands.run_jobs.Command.trace_flights"), \
             mock.patch("journeys.management.commands.run_jobs.Command.refresh_transit"), \
             mock.patch("journeys.management.commands.run_jobs.Command.refresh_china_stations"), \
             mock.patch("journeys.management.commands.run_jobs.Command.refresh_driving"), \
             mock.patch("journeys.weather.refresh_trip", return_value=0), \
             mock.patch("journeys.money.refresh_rates", return_value=0), \
             mock.patch("django.utils.timezone.localdate", return_value=date(2026, 12, 1)):
            call_command("run_jobs", stdout=io.StringIO())
        self.item.refresh_from_db()

    def test_metro_directions_from_the_hotel(self):
        self.run_job()
        directions = self.item.directions
        self.assertEqual((directions["route"]["from"], directions["route"]["to"]), ("tsim-sha-tsui", "austin"))
        self.assertEqual(directions["stay"], self.stay.pk)
        page = self.client.get(reverse("manage:activities", args=[self.trip.pk])).content.decode()
        self.assertIn("Metro Tsim Sha Tsui → Austin", page)
        self.assertIn("https://www.metroman.cn/en/maps/hongkong?from=tsim-sha-tsui&amp;to=austin", page)  # no map on this page

    def test_itinerary_animates_instead_of_linking(self):
        self.run_job()
        page = self.client.get(reverse("manage:trip", args=[self.trip.pk])).content.decode()
        self.assertIn("data-animate-route=", page)
        self.assertNotIn(">Metro map</a>", page)  # replaced by "Show route" where there's a map
        train = Leg.objects.get(number="G388")
        self.assertIn(f'data-animate-leg="{train.pk}"', page)

    def test_taxi_when_there_is_no_metro(self):
        far = place("Wong Tai Sin Temple", Place.Kind.ATTRACTION, 22.3420, 114.1940, country="HK", tz="Asia/Hong_Kong")
        self.item.place = far  # beyond walking distance of any station in the test network
        self.item.save()
        self.run_job()
        self.assertEqual(self.item.directions["taxi"], {"minutes": 25, "km": 12.3})

    def test_directions_follow_changes(self):
        self.run_job()
        self.assertFalse(list(metro.activities_waiting(self.trip)))
        self.stay.location = place("Other Hotel", Place.Kind.HOTEL, 22.3000, 114.1700, country="HK", tz="Asia/Hong_Kong")
        self.stay.save()
        self.assertEqual([item for item, _stay, _basis in metro.activities_waiting(self.trip)], [self.item])

    def test_import_from_a_map_link(self):
        response = self.client.post(reverse("manage:api_activity_extract", args=[self.trip.pk]),
                                    json.dumps({"url": "https://www.openstreetmap.org/?mlat=22.2933&mlon=114.1694"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["place"])  # a bare position with no name isn't saved as a place
        with mock.patch("journeys.activity_import.fetch_page", return_value=(
                '<meta property="og:title" content="Victoria Peak - Wikipedia"><script>"wgCoordinates":{"lat":22.275556,"lon":114.143889}</script>')):
            response = self.client.post(reverse("manage:api_activity_extract", args=[self.trip.pk]),
                                        json.dumps({"url": "https://en.wikipedia.org/wiki/Victoria_Peak"}), content_type="application/json")
        result = response.json()
        self.assertEqual(result["fields"]["name"], "Victoria Peak")
        self.assertEqual(Place.objects.get(pk=result["place"]["id"]).kind, Place.Kind.ATTRACTION)

    def test_saving_an_activity_works_out_directions_again(self):
        self.item.directions = {"basis": [1, 2, 3, 4], "walk_m": 100}
        self.item.save()
        url = reverse("manage:item_edit", args=[self.trip.pk, self.item.pk]) + "?next=" + reverse("manage:activities", args=[self.trip.pk])
        response = self.client.post(url, {"day": self.item.day.pk, "title": "Kowloon Park", "block": "morning", "place": self.sight.pk,
                                          "next": reverse("manage:activities", args=[self.trip.pk])})
        self.assertRedirects(response, reverse("manage:activities", args=[self.trip.pk]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.directions, {"pending": True})

    def test_public_and_private_switches(self):
        self.run_job()
        trip_url, activities_url = reverse("journeys:trip", args=["test-trip"]), reverse("journeys:activities", args=["test-trip"])
        self.assertNotContains(self.client.get(trip_url), "Kowloon Park")  # activities aren't in the itinerary
        self.client.post(reverse("manage:item_toggle", args=[self.trip.pk, self.item.pk]))  # make it private
        self.client.logout()
        self.assertNotContains(self.client.get(activities_url), "Kowloon Park")
        shared = reverse("journeys:shared_activities", args=[self.trip.share_token])
        self.assertContains(self.client.get(shared), "Kowloon Park")  # the secret link shows private items
        self.assertNotContains(self.client.get(shared), "SECRET-ITEM-NOTE")
        train = Leg.objects.get(number="G388")
        Leg.objects.filter(pk=train.pk).update(is_public=False)
        self.assertNotContains(self.client.get(trip_url), "Shenzhen North")
        self.assertContains(self.client.get(reverse("journeys:shared_trip", args=[self.trip.share_token])), "Shenzhen North")

    def test_visitors_see_rounded_times_and_the_airline_not_the_number(self):
        self.client.logout()
        page = self.client.get(reverse("journeys:trip", args=["test-trip"])).content.decode()
        self.assertIn("~01:00", page)  # CX156 leaves at 00:50
        self.assertIn("~07:30", page)  # and lands at 07:20 (Hong Kong time)
        self.assertIn("about 8 h 30 min", page)
        self.assertIn("Cathay Pacific", page)
        self.assertNotIn("CX156", page)
        self.assertNotIn("00:50", page)
        shared = self.client.get(reverse("journeys:shared_trip", args=[self.trip.share_token])).content.decode()
        self.assertIn("CX156", shared)
        self.assertIn("00:50", shared)
        self.assertNotIn("SECRETREF1", shared)  # booking references stay in the planner

    def test_directions_need_a_public_stay_for_visitors(self):
        self.run_job()
        url = reverse("journeys:activities", args=["test-trip"])
        self.assertContains(self.client.get(url), "Getting there from Hong Kong 1")  # signed in
        self.client.logout()
        public = self.client.get(url)
        self.assertContains(public, "Kowloon Park")
        self.assertNotContains(public, "Getting there")  # it sets out from a private stay
        Stay.objects.filter(pk=self.stay.pk).update(is_public=True)
        self.assertContains(self.client.get(url), "Getting there from Hong Kong 1")
        self.assertContains(self.client.get(reverse("journeys:trip", args=["test-trip"])), "data-animate-leg=")  # trains play for everyone


class BulkActivityTests(TestCase):
    def setUp(self):
        from journeys.models import Trip

        self.trip, self.places = sample_trip()
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        day1 = self.trip.days.get(date=date(2027, 1, 1))
        self.a = Item.objects.create(day=day1, title="Peak Tram")
        self.b = Item.objects.create(day=day1, title="Star Ferry", directions={"walk_m": 300, "basis": [1]})
        other = Trip.objects.create(title="Other", slug="other", start_date=date(2027, 5, 1), end_date=date(2027, 5, 2))
        self.stranger = Item.objects.create(day=other.days.create(date=date(2027, 5, 1)), title="Not this trip")
        self.url = reverse("manage:activities_bulk", args=[self.trip.pk])

    def post(self, **data):
        return self.client.post(self.url, data)

    def test_make_public_and_private_together(self):
        self.post(items=[self.a.pk, self.b.pk, self.stranger.pk], action="public")
        self.assertEqual(set(Item.objects.filter(is_public=True).values_list("title", flat=True)), {"Harbour walk", "Peak Tram", "Star Ferry"})
        self.post(items=[self.a.pk], action="private")
        self.assertFalse(Item.objects.get(pk=self.a.pk).is_public)
        self.assertFalse(Item.objects.get(pk=self.stranger.pk).is_public)  # another trip's activity is left alone

    def test_move_to_another_day(self):
        day3 = self.trip.days.get(date=date(2027, 1, 3))
        response = self.post(items=[self.a.pk, self.b.pk], action="move", day=day3.pk)
        self.assertRedirects(response, reverse("manage:activities", args=[self.trip.pk]))
        self.b.refresh_from_db()
        self.assertEqual(self.b.day, day3)
        self.assertEqual(self.b.directions, {"pending": True})  # worked out again from that night's hotel
        self.post(items=[self.a.pk], action="move", day="")
        self.assertEqual(Item.objects.get(pk=self.a.pk).day, day3)  # no day picked: nothing moves

    def test_delete_asks_first(self):
        page = self.post(items=[self.a.pk, self.b.pk], action="delete")
        self.assertContains(page, "Delete 2 activities?")
        self.assertContains(page, "Peak Tram")
        self.assertEqual(Item.objects.filter(pk__in=[self.a.pk, self.b.pk]).count(), 2)
        self.post(items=[self.a.pk, self.b.pk, self.stranger.pk], action="delete", confirm="yes")
        self.assertFalse(Item.objects.filter(pk__in=[self.a.pk, self.b.pk]).exists())
        self.assertTrue(Item.objects.filter(pk=self.stranger.pk).exists())

    def test_nothing_ticked(self):
        response = self.post(action="public")
        self.assertRedirects(response, reverse("manage:activities", args=[self.trip.pk]))
        self.assertFalse(Item.objects.get(pk=self.a.pk).is_public)

    def test_page_offers_the_checkboxes(self):
        page = self.client.get(reverse("manage:activities", args=[self.trip.pk])).content.decode()
        self.assertIn('id="bulk-form"', page)
        self.assertEqual(page.count('form="bulk-form" name="items"'), 4)  # Harbour walk, Hidden plan, Peak Tram, Star Ferry
        self.assertIn("data-bulk-group=", page)
