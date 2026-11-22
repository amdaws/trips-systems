"""What visitors must never see: hotels at the ends of public journeys, hotel names as stay labels or
calendar locations, and private legs behind the countdown or the transit credits. Plus the cookie
promise on /legals/ and the request size checks (15 Nov 2026 review)."""

import json
from datetime import date, datetime, time, timezone as tz
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from journeys import transit
from journeys.forms import StayForm
from journeys.models import Item, Leg, Place, Stay
from journeys.presenters import countdown, show_leg

from .factories import sample_trip


def _pages(client, trip, paths=("", "stays/", "activities/", "map.json", "calendar.ics")):
    return {path or "trip": client.get(f"/trips/{trip.slug}/{path}").content.decode() for path in paths}


class HotelEndsOfJourneysTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()
        self.hotel = Place.objects.create(name="PROBE-HOTEL-LEG", kind=Place.Kind.HOTEL, lat=22.29871, lon=114.17222,
                                          tz="Asia/Hong_Kong", country="HK", city="Hong Kong")
        self.leg = Leg.objects.create(trip=self.trip, mode=Leg.Mode.CAR, origin=self.hotel, destination=self.places["xkl"],
                                      depart_date=date(2027, 1, 2), depart_time=time(6, 30), is_public=True)

    def test_visitors_never_see_the_hotel(self):
        for page, body in _pages(self.client, self.trip).items():
            with self.subTest(page=page):
                self.assertNotIn("PROBE-HOTEL-LEG", body)
                self.assertNotIn("22.29871", body)
        self.assertNotIn(self.leg.pk, [leg["id"] for leg in self.client.get(f"/trips/{self.trip.slug}/map.json").json()["legs"]])

    def test_visitors_still_see_the_journey(self):
        self.assertContains(self.client.get(f"/trips/{self.trip.slug}/"), f'data-journey-detail="{self.leg.pk}"')

    def test_hotel_without_a_city_reads_hotel(self):
        Place.objects.filter(pk=self.hotel.pk).update(city="")
        body = self.client.get(f"/trips/{self.trip.slug}/").content.decode()
        self.assertNotIn("PROBE-HOTEL-LEG", body)
        self.assertIn("Hotel", body)

    def test_full_viewers_still_see_it(self):
        response = self.client.get(reverse("journeys:shared_map", args=[self.trip.share_token]))
        leg = next(leg for leg in response.json()["legs"] if leg["id"] == self.leg.pk)
        self.assertEqual(leg["from"], "PROBE-HOTEL-LEG")

    def test_stand_in_is_never_saved(self):
        show_leg(self.leg, False)
        self.assertEqual(self.leg.origin.name, "Hotel")
        self.assertEqual(self.leg.origin_id, self.hotel.pk)
        self.hotel.refresh_from_db()
        self.assertEqual(self.hotel.name, "PROBE-HOTEL-LEG")

    def test_change_log_names_the_city(self):
        Place.objects.filter(pk=self.hotel.pk).update(city="")
        self.leg.refresh_from_db()
        self.leg.depart_time = time(6, 45)
        self.leg.save()
        self.assertNotIn("PROBE-HOTEL-LEG", self.client.get(f"/trips/{self.trip.slug}/").content.decode())


class StayLabelTests(TestCase):
    def test_label_that_names_the_hotel_is_replaced_on_save(self):
        trip, places = sample_trip()
        stay = Stay.objects.create(trip=trip, label="Hilton Shenzhen", name="Hilton", city=places["shenzhen"],
                                   check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3))
        self.assertEqual(stay.label, "Shenzhen")
        no_city = Stay.objects.create(trip=trip, label="PROBE-HOTEL-NAME", name="PROBE-HOTEL-NAME",
                                      check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3))
        self.assertEqual(no_city.label, "Stay")
        for page, body in _pages(self.client, trip).items():
            with self.subTest(page=page):
                self.assertNotIn("PROBE-HOTEL-NAME", body)

    def test_city_label_is_kept(self):
        trip, places = sample_trip()
        stay = Stay.objects.create(trip=trip, label="Hong Kong 2", name="Hong Kong Hotel Grand", city=places["hongkong"],
                                   check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3))
        self.assertEqual(stay.label, "Hong Kong 2")

    def test_form_refuses_a_label_naming_the_hotel(self):
        form = StayForm(data={"label": "The Peninsula", "kind": "hotel", "name": "The Peninsula",
                              "check_in_date": "2027-01-01", "check_out_date": "2027-01-02"})
        self.assertFalse(form.is_valid())
        self.assertIn("label", form.errors)


class CalendarActivityTests(TestCase):
    def test_hotel_location_only_for_full_viewers(self):
        trip, _ = sample_trip()
        hotel = Place.objects.create(name="PROBE-HOTEL-PLACE", kind=Place.Kind.HOTEL, lat=22.3, lon=114.17, tz="Asia/Hong_Kong")
        Item.objects.create(day=trip.days.get(date=date(2027, 1, 1)), title="Rest", start_time=time(15, 0), place=hotel, is_public=True)
        self.assertNotContains(self.client.get(f"/trips/{trip.slug}/calendar.ics"), "PROBE-HOTEL-PLACE")
        self.assertContains(self.client.get(reverse("journeys:shared_calendar", args=[trip.share_token])), "PROBE-HOTEL-PLACE")


class PrivateLegsTests(TestCase):
    def test_countdown_ignores_a_private_first_leg(self):
        trip, places = sample_trip()
        Leg.objects.create(trip=trip, mode=Leg.Mode.CAR, origin=places["hongkong"], destination=places["hkg"],
                           depart_date=date(2026, 12, 31), depart_time=time(21, 7), is_public=False)
        now = datetime(2026, 12, 1, tzinfo=tz.utc)
        visitor, owner = countdown(trip, False, now=now), countdown(trip, True, now=now)
        self.assertIn("Brisbane", visitor["when"])  # the first public leg: the flight from Brisbane
        self.assertNotIn("21:15", visitor["when"])
        self.assertIn("21:07", owner["when"])

    def test_transit_credits_ignore_private_legs(self):
        trip, _ = sample_trip()
        central = Place.objects.create(name="Central", kind=Place.Kind.STATION, lat=-33.883, lon=151.206, tz="Australia/Sydney", country="AU")
        town_hall = Place.objects.create(name="Town Hall", kind=Place.Kind.STATION, lat=-33.873, lon=151.207, tz="Australia/Sydney", country="AU")
        Leg.objects.create(trip=trip, mode=Leg.Mode.TRAIN, origin=central, destination=town_hall,
                           depart_date=date(2027, 1, 3), depart_time=time(9, 0), is_public=False)
        with mock.patch("journeys.presenters.transit.networks_near", return_value=[]) as near:
            self.client.get(f"/trips/{trip.slug}/")
        self.assertNotIn([-33.883, 151.206], [point for call in near.call_args_list for point in call.args[0]])

    def test_midnight_departure_keeps_the_next_day_mark(self):
        trip, places = sample_trip()
        leg = Leg.objects.create(trip=trip, mode=Leg.Mode.FLIGHT, number="CX1", origin=places["hkg"], destination=places["hkg"],
                                 depart_date=date(2027, 1, 2), depart_time=time(23, 50), arrive_date=date(2027, 1, 3),
                                 arrive_time=time(1, 10), is_public=True)
        show_leg(leg, False)
        self.assertEqual((leg.shown_depart, leg.shown_arrive, leg.shown_next_day), ("00:00", "01:15", True))


class CookieTests(TestCase):
    """/legals/ says the site sets no cookies for visitors."""

    def test_visitors_get_no_cookies(self):
        trip, _ = sample_trip()
        for path in ("/", "/journey/", f"/trips/{trip.slug}/", "/legals/", "/changelog/"):
            with self.subTest(path=path):
                self.assertEqual(dict(self.client.get(path).cookies), {})

    def test_public_planner_api_works_without_a_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post("/journey/api/places/", json.dumps({"q": ""}), content_type="application/json")
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(dict(response.cookies), {})

    def test_staff_api_still_needs_the_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.assertEqual(client.post("/journey/api/add/", "{}", content_type="application/json").status_code, 403)
        self.assertContains(client.get("/journey/"), "csrfmiddlewaretoken")


class RequestSizeTests(TestCase):
    def test_oversized_bodies_are_not_read(self):
        from journeys.journey_views import _body

        class Request:
            META = {"CONTENT_LENGTH": "5000000"}

            @property
            def body(self):
                raise AssertionError("the body was read")

        self.assertEqual(_body(Request()), {})
        with mock.patch("core.views.log") as log:
            response = self.client.post("/csp-report/", "{}", content_type="application/csp-report", CONTENT_LENGTH="5000000")
        self.assertEqual(response.status_code, 204)
        log.warning.assert_not_called()


class PtvAddressTests(TestCase):
    def setUp(self):
        transit._ptv_url.cache_clear()
        self.addCleanup(transit._ptv_url.cache_clear)

    def _catalogue(self, url, name="GTFS Schedule"):
        body = json.dumps({"result": {"resources": [{"name": "Readme", "url": transit.PTV_HOST + "x/readme.pdf"},
                                                   {"name": name, "url": url}]}}).encode()
        response = mock.MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        return response

    def test_current_address_comes_from_the_catalogue(self):
        new = transit.PTV_HOST + "dataset/a/resource/b/download/gtfs.zip"
        with mock.patch("journeys.transit.urllib.request.urlopen", return_value=self._catalogue(new)):
            self.assertEqual(transit._ptv_url(), new)

    def test_other_sites_are_ignored(self):
        with mock.patch("journeys.transit.urllib.request.urlopen", return_value=self._catalogue("https://evil.example/gtfs.zip")):
            self.assertIsNone(transit._ptv_url())

    def test_build_tries_the_current_address_first(self):
        new = transit.PTV_HOST + "dataset/a/resource/c/download/gtfs.zip"
        tried = []

        def download(url, key=None):
            tried.append(url)
            raise OSError("offline")

        feed = {"url": transit.PTV_URL, "resolve": lambda: new}
        with mock.patch("journeys.transit._download", side_effect=download), self.assertRaises(OSError):
            transit._fetch_feed(feed, {})
        self.assertEqual(tried, [new, transit.PTV_URL])


class HotelTransferTests(TestCase):
    """A public shuttle or bus to the hotel's door: visitors get no metro or taxi transfer from it,
    which would name the hotel ("Walk from <hotel> to the hotel")."""

    def setUp(self):
        from journeys.models import Stay

        self.trip, self.places = sample_trip()
        self.hotel = Place.objects.create(name="PROBE-SHUTTLE-HOTEL", kind=Place.Kind.HOTEL, lat=22.5450, lon=114.0550,
                                          tz="Asia/Shanghai", country="CN", city="Shenzhen")
        self.leg = Leg.objects.create(trip=self.trip, mode=Leg.Mode.SHUTTLE, origin=self.places["szn"], destination=self.hotel,
                                      depart_date=date(2027, 1, 2), depart_time=time(9, 0), arrive_time=time(9, 40), is_public=True)
        self.stay = Stay.objects.create(trip=self.trip, label="Shenzhen", name="Probe Stay", city=self.places["shenzhen"],
                                        location=self.hotel, check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3),
                                        is_public=True)
        Stay.objects.filter(pk=self.stay.pk).update(metro={"taxi": {"arrival": {"place": self.hotel.pk, "minutes": 5, "km": 1.0}}})

    def views(self, exact):
        from journeys import metro
        from journeys.models import Stay

        [stay] = metro.for_stays(self.trip, [Stay.objects.get(pk=self.stay.pk)], exact=exact)
        return stay.metro_view

    def test_owner_sees_it_visitors_do_not(self):
        self.assertEqual(self.views(exact=True)["arrival_taxi"]["place"], "PROBE-SHUTTLE-HOTEL")
        self.assertNotIn("arrival_taxi", self.views(exact=False))
        for page, body in _pages(self.client, self.trip).items():
            with self.subTest(page=page):
                self.assertNotIn("PROBE-SHUTTLE-HOTEL", body)

    def test_a_place_named_after_the_property_counts_as_the_hotel(self):
        from journeys import metro

        Place.objects.filter(pk=self.hotel.pk).update(kind=Place.Kind.ATTRACTION, name="Probe Stay Shuttle Stop")
        self.stay.location = None
        self.assertTrue(metro.at_the_hotel(Place.objects.get(pk=self.hotel.pk), self.stay))
        self.assertFalse(metro.at_the_hotel(self.places["szn"], self.stay))


class LabelPartOfTheHotelTests(TestCase):
    def test_part_of_the_name_gives_it_away(self):
        from journeys.models import label_gives_hotel_away as gives

        self.assertTrue(gives("Hilton", "Hilton Shenzhen Futian", "Shenzhen"))
        self.assertTrue(gives("Futian Hilton", "Hilton Shenzhen Futian", "Shenzhen"))
        self.assertTrue(gives("希尔顿", "深圳希尔顿酒店", "Shenzhen", "深圳"))
        for label, name, city in (("Shenzhen", "Hilton Shenzhen Futian", "Shenzhen"), ("Shenzhen 2", "Hilton Shenzhen Futian", "Shenzhen"),
                                  ("Shenzhen Airport", "Hilton Garden Inn Shenzhen Airport", "Shenzhen"),
                                  ("Hong Kong 2", "Hong Kong Hotel Grand", "Hong Kong")):
            with self.subTest(label=label):
                self.assertFalse(gives(label, name, city))
        self.assertFalse(gives("深圳", "深圳希尔顿酒店", "Shenzhen", "深圳"))

    def test_saved_and_typed_labels(self):
        trip, places = sample_trip()
        stay = Stay.objects.create(trip=trip, label="Hilton", name="Hilton Shenzhen Futian", city=places["shenzhen"],
                                   check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3))
        self.assertEqual(stay.label, "Shenzhen")
        form = StayForm(data={"label": "Hilton", "kind": "hotel", "name": "Hilton Shenzhen Futian", "city": places["shenzhen"].pk,
                              "check_in_date": "2027-01-02", "check_out_date": "2027-01-03"})
        self.assertFalse(form.is_valid())
        self.assertIn("part of it", str(form.errors["label"]))

    def test_existing_labels_and_change_list_are_fixed(self):
        import importlib

        from django.apps import apps as django_apps

        from journeys import changes

        trip, places = sample_trip()
        stay = Stay.objects.create(trip=trip, label="Shenzhen", name="Hilton Shenzhen Futian", city=places["shenzhen"],
                                   check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3), is_public=True)
        Stay.objects.filter(pk=stay.pk).update(label="Hilton")  # as saved before this check
        change = changes.record(Stay.objects.get(pk=stay.pk), "added")  # the "Recently updated" list kept it too
        importlib.import_module("journeys.migrations.0028_stay_labels_hide_hotel").hide_hotels_in_labels(django_apps, None)
        stay.refresh_from_db(), change.refresh_from_db()
        self.assertEqual((stay.label, change.data["public"]["label"]), ("Shenzhen", "Shenzhen"))
        self.assertNotIn("Hilton", self.client.get(f"/trips/{trip.slug}/").content.decode())
