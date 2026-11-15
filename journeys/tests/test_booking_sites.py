import json
from datetime import date, time
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from journeys import booking_import, site_links
from journeys.models import BookingImport, BudgetItem, Day, Item, Leg, Place, Trip

BOOKING_COM = """Thanks, Aaron! Your booking in Sydney is confirmed.
The Langham, Sydney
89-113 Kent Street, Millers Point, Sydney, NSW 2000, Australia
Phone: +61 2 8248 5555
Booking number: 4123.456.789
PIN code: 7342
Check-in Tuesday, 5 January 2027 (from 14:00)
Check-out Friday, 8 January 2027 (until 11:00)
Your reservation 3 nights, 1 room
Deluxe King Room
Total price AU$ 1,234.50
Booking.com"""

KLOOK_ACTIVITY = """Your booking is confirmed!
Booking reference ID: KL12345678
Activity name: Sydney Harbour BridgeClimb
Package: BridgeClimb Summit - Day
Participation date: 6 Jan 2027
Time: 10:15
Meeting point: 3 Cumberland Street, The Rocks NSW 2000
2 x Adult
Total: AUD 698.00
Klook Travel Technology"""

KLOOK_TRANSFER = """Booking confirmed
Booking reference ID: KL87654321
Service: Sydney Airport (SYD) to Sydney CBD Private Transfer
Pick-up date: 5 Jan 2027
Pick-up time: 07:30
Pick-up location: Sydney Airport International Terminal T1
Drop-off location: The Langham Sydney, 89-113 Kent Street
2 Adults
Total: AUD 89.00
Klook"""

VIATOR = """Your Viator booking is confirmed
Booking reference: BR-1234567890
Tour name: Great Ocean Road Small-Group Tour from Melbourne
Travel date: Sat, 9 Jan 2027
Meeting point: Federation Square, Melbourne
Start time: 07:00
2 Adults
Total paid: A$290.00"""


class LinkTests(SimpleTestCase):
    def test_booking_com_hotel_with_dates(self):
        found = site_links.read("https://www.booking.com/hotel/au/the-langham-sydney.en-gb.html?aid=1&checkin=2027-01-05&checkout=2027-01-08&group_adults=2")
        self.assertEqual((found["kind"], found["name"], found["country"]), ("stay", "The Langham Sydney", "AU"))
        self.assertEqual((found["check_in_date"], found["check_out_date"], found["adults"]), (date(2027, 1, 5), date(2027, 1, 8), 2))

    def test_tripadvisor_pages(self):
        sight = site_links.read("https://www.tripadvisor.com.au/Attraction_Review-g255060-d256561-Reviews-Sydney_Opera_House-Sydney_New_South_Wales.html")
        self.assertEqual((sight["kind"], sight["name"], sight["city"], sight["region"]), ("activity", "Sydney Opera House", "Sydney", "New South Wales"))
        hotel = site_links.read("https://www.tripadvisor.com.au/Hotel_Review-g255068-d300427-Reviews-Rydges_Southbank_Brisbane-Brisbane_Brisbane_Region_Queensland.html")
        self.assertEqual((hotel["kind"], hotel["city"]), ("stay", "Brisbane"))
        tour = site_links.read("https://www.tripadvisor.com/AttractionProductReview-g294212-d1-Great_Wall_at_Mutianyu_Private_Tour-Beijing.html")
        self.assertEqual(site_links.search_texts(tour)[:2], ["Great Wall at Mutianyu Private Tour, Beijing", "Great Wall at Mutianyu, Beijing"])
        climb = site_links.read("https://www.klook.com/en-AU/activity/1494-sydney-harbour-bridge-climb-sydney/")
        self.assertIn("Sydney Harbour Bridge", site_links.search_texts(climb))  # shorter forms of a tour's name
        self.assertLessEqual(len(site_links.search_texts(climb)), 5)  # OpenStreetMap's search allows one a second

    def test_klook_and_viator(self):
        climb = site_links.read("https://www.klook.com/en-AU/activity/1494-sydney-harbour-bridge-climb-sydney/")
        self.assertEqual((climb["kind"], climb["name"]), ("activity", "Sydney Harbour Bridge Climb"))
        transfer = site_links.read("https://www.klook.com/en-AU/activity/23017-sydney-airport-to-sydney-city-private-transfer/")
        self.assertEqual((transfer["kind"], transfer["mode"], transfer["origin"], transfer["destination"], transfer["operator"]),
                         ("transport", "car", "Sydney Airport", "Sydney City", "Klook"))
        self.assertEqual(site_links.read("https://www.klook.com/en-AU/activity/1420-jr-pass-whole-japan/")["mode"], "train")
        tour = site_links.read("https://www.viator.com/tours/Melbourne/Great-Ocean-Road-Small-Group-Tour/d384-5370GOR")
        self.assertEqual((tour["name"], tour["city"]), ("Great Ocean Road Small Group Tour", "Melbourne"))
        self.assertIsNone(site_links.read("https://www.example.com/hotel/au/x.html"))


class ConfirmationTests(SimpleTestCase):
    def test_booking_com_stay(self):
        found = booking_import.parse(BOOKING_COM)
        self.assertEqual((found["kind"], found["site"], found["booking_ref"]), ("stay", "Booking.com", "4123.456.789"))
        self.assertEqual((found["check_in_date"], found["check_in_time"]), (date(2027, 1, 5), "14:00"))
        self.assertEqual((found["check_out_date"], found["check_out_time"]), (date(2027, 1, 8), "11:00"))
        self.assertEqual((found["amount"], found["currency"]), ("1234.50", "AUD"))
        self.assertNotIn("7342", booking_import.redact(BOOKING_COM))  # Booking.com's "PIN code" never stored

    def test_klook_activity(self):
        found = booking_import.parse(KLOOK_ACTIVITY)
        self.assertEqual((found["kind"], found["site"], found["booking_ref"]), ("activity", "Klook", "KL12345678"))
        self.assertEqual((found["title"], found["activity_date"], found["start_time"]), ("Sydney Harbour BridgeClimb", date(2027, 1, 6), "10:15"))
        self.assertEqual((found["meeting_point"], found["travellers"]), ("3 Cumberland Street, The Rocks NSW 2000", "2 adult"))
        self.assertEqual(found["amount"], "698.00")

    def test_klook_transfer(self):
        found = booking_import.parse(KLOOK_TRANSFER)
        self.assertEqual((found["kind"], found["activity_date"], found["start_time"]), ("transfer", date(2027, 1, 5), "07:30"))
        self.assertEqual((found["pickup"], found["dropoff"]), ("Sydney Airport International Terminal T1", "The Langham Sydney, 89-113 Kent Street"))

    def test_viator_tour(self):
        found = booking_import.parse(VIATOR)
        self.assertEqual((found["kind"], found["site"], found["booking_ref"]), ("activity", "Viator", "BR-1234567890"))
        self.assertEqual((found["activity_date"], found["start_time"], found["meeting_point"]), (date(2027, 1, 9), "07:00", "Federation Square, Melbourne"))


class ReviewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.trip = Trip.objects.create(title="Australia", slug="oz", start_date=date(2027, 1, 4), end_date=date(2027, 1, 10))
        for offset in range(7):
            Day.objects.create(trip=self.trip, date=date(2027, 1, 4 + offset))

    def review(self, text, **post):
        record = BookingImport.objects.create(trip=self.trip, source=BookingImport.Source.PASTE, text=text)
        url = reverse("manage:import_review", args=[record.pk])
        return record, (self.client.post(url, post) if post else self.client.get(url))

    def test_klook_activity_becomes_an_activity(self):
        record, page = self.review(KLOOK_ACTIVITY)
        self.assertContains(page, "Which activity is this?")
        self.review(KLOOK_ACTIVITY, action="apply", trip=self.trip.pk, target="new", budget="1",
                    fields=["booking_ref", "title", "start_time", "meeting_point"])
        item = Item.objects.get()
        self.assertEqual((item.title, item.day.date, item.start_time, item.address),
                         ("Sydney Harbour BridgeClimb", date(2027, 1, 6), time(10, 15), "3 Cumberland Street, The Rocks NSW 2000"))
        self.assertIn("Booked with Klook: ref KL12345678", item.private_notes)
        self.assertFalse(item.is_public)  # booking details stay private
        line = BudgetItem.objects.get()
        self.assertEqual((line.category, line.actual, line.actual_currency), ("activity", Decimal("698.00"), "AUD"))

    def test_klook_transfer_books_the_leg(self):
        airport = Place.objects.create(kind=Place.Kind.AIRPORT, name="Sydney Airport", lat=-33.94, lon=151.18, tz="Australia/Sydney")
        hotel = Place.objects.create(kind=Place.Kind.HOTEL, name="The Langham Sydney", lat=-33.86, lon=151.20, tz="Australia/Sydney")
        leg = Leg.objects.create(trip=self.trip, mode=Leg.Mode.CAR, origin=airport, destination=hotel, depart_date=date(2027, 1, 5))
        record, page = self.review(KLOOK_TRANSFER)
        self.assertContains(page, "Which transport is this?")
        self.assertContains(page, "matches the date")
        with mock.patch("journeys.manage_views.routing.refresh_path"):
            self.review(KLOOK_TRANSFER, action="apply", trip=self.trip.pk, target=leg.pk, fields=["booking_ref", "start_time"])
        leg.refresh_from_db()
        self.assertEqual((leg.booking_ref, leg.depart_time, leg.status, leg.operator), ("KL87654321", time(7, 30), Leg.Status.BOOKED, "Klook"))


class LinkFillTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.trip = Trip.objects.create(title="Australia", slug="oz", start_date=date(2027, 1, 4), end_date=date(2027, 1, 10))

    def post(self, name, url):
        return self.client.post(reverse(name, args=[self.trip.pk]), json.dumps({"url": url}), content_type="application/json").json()

    def test_booking_com_link_fills_the_stay_without_contacting_booking_com(self):
        result = {"name": "The Langham", "lat": -33.8607, "lon": 151.2041, "kind": "hotel", "country": "AU", "city": "Sydney",
                  "label": "The Langham, 89-113, Kent Street, Millers Point, Sydney, New South Wales, 2000, Australia", "source_id": "way/1", "name_local": ""}
        with mock.patch("journeys.manage_views.geocode.search", return_value=[result]) as search, \
                mock.patch("journeys.stay_import.fetch_page", side_effect=AssertionError("fetched")):
            data = self.post("manage:api_stay_extract", "https://www.booking.com/hotel/au/the-langham-sydney.html?checkin=2027-01-05&checkout=2027-01-08")
        self.assertEqual(search.call_args.kwargs["country"], "AU")
        fields = data["fields"]
        self.assertEqual((fields["name"], fields["check_in_date"], fields["check_out_date"]), ("The Langham Sydney", "2027-01-05", "2027-01-08"))
        self.assertTrue(fields["address"].startswith("The Langham, 89-113, Kent Street"))
        self.assertIsNotNone(data["location"])

    def test_klook_transfer_link_fills_transport(self):
        with mock.patch("journeys.link_details.find_place", return_value=None), \
                mock.patch("journeys.stay_import.fetch_page", side_effect=AssertionError("fetched")):
            data = self.post("manage:api_leg_extract", "https://www.klook.com/en-AU/activity/23017-sydney-airport-to-sydney-city-private-transfer/")
        self.assertEqual((data["fields"]["mode"], data["fields"]["operator"], data["fields"]["origin"]), ("car", "Klook", "Sydney Airport"))

    def test_tripadvisor_link_finds_the_sight(self):
        result = {"name": "Sydney Opera House", "lat": -33.8568, "lon": 151.2153, "kind": "attraction", "country": "AU", "city": "Sydney",
                  "label": "Sydney Opera House, Sydney", "source_id": "way/2", "name_local": ""}
        with mock.patch("journeys.manage_views.geocode.search", return_value=[result]) as search:
            data = self.post("manage:api_activity_extract",
                             "https://www.tripadvisor.com.au/Attraction_Review-g255060-d256561-Reviews-Sydney_Opera_House-Sydney_New_South_Wales.html")
        self.assertEqual(search.call_args.args[0], "Sydney Opera House, New South Wales")
        self.assertEqual((data["fields"]["name"], data["place"]["name"]), ("Sydney Opera House", "Sydney Opera House"))
