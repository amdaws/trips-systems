import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from journeys import leg_import
from journeys.models import Leg

from .factories import sample_trip

# Page text copied from a Trip.com things-to-do transfer (it shows robots a verification check instead).
PASTED = """Home
Songpan local experience
Huanglong Jiuzhai station - Jiuzhaigou National Park shuttle bus [VIP 2+1 luxury bus] pick-up/drop-off optional
Trip Select
Service languages: Mandarin, English
Free cancellation by 16:00, 1 day before the date of use
Highlights
5-9 seater shared transfer, with free pick-up and drop-off at hotels within 10 km
From A$32.50"""


class TitleTests(TestCase):
    def test_routes_from_titles(self):
        cases = {
            "Huanglong Jiuzhai station - Jiuzhaigou National Park shuttle bus [VIP 2+1 luxury bus] pick-up/drop-off optional":
                ("Huanglong Jiuzhai station", "Jiuzhaigou National Park", "VIP 2+1 luxury bus", Leg.Mode.SHUTTLE),
            "Private Transfer from Zhangjiajie Hehua Airport to Wulingyuan": ("Zhangjiajie Hehua Airport", "Wulingyuan", "", Leg.Mode.CAR),
            "Hong Kong ⇌ Macau Coach (HZMB)": ("Hong Kong", "Macau", "HZMB", Leg.Mode.BUS),
            "Shenzhen Bao'an Airport to Hong Kong Cross-Border Private Car Transfer": ("Shenzhen Bao'an Airport", "Hong Kong", "", Leg.Mode.CAR),
            "Private car transfer: Guilin Liangjiang Airport - Yangshuo hotels": ("Guilin Liangjiang Airport", "Yangshuo", "", Leg.Mode.CAR),
        }
        for title, (origin, destination, extra, mode) in cases.items():
            self.assertEqual(leg_import.route_from_title(title), (origin, destination, extra), title)
            self.assertEqual(leg_import.mode_for(title), mode, title)

    def test_pasted_page_text(self):
        found = leg_import.extract(PASTED)
        self.assertEqual((found["origin"], found["destination"], found["mode"]), ("Huanglong Jiuzhai station", "Jiuzhaigou National Park", "shuttle"))
        self.assertEqual(found["seat_class"], "VIP 2+1 luxury bus")
        self.assertIn("Price: A$32.50", found["notes"])
        self.assertIn("Free cancellation by 16:00, 1 day before the date of use", found["notes"])

    def test_schema_org_bus_trip(self):
        page = """<script type="application/ld+json">{"@type": "BusTrip", "name": "Hong Kong to Macau", "provider": {"name": "One Bus"},
        "departureBusStop": {"name": "Kowloon Station"}, "arrivalBusStop": {"name": "Macau Ferry Terminal"},
        "offers": {"price": "160", "priceCurrency": "HKD"}}</script>"""
        found = leg_import.extract(page, "https://12go.asia/en/travel/hong-kong/macau")
        self.assertEqual((found["origin"], found["destination"], found["operator"]), ("Kowloon Station", "Macau Ferry Terminal", "One Bus"))
        self.assertIn("Price: HKD 160", found["notes"])


class EndpointTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.url = reverse("manage:api_leg_extract", args=[self.trip.pk])

    def post(self, body):
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def test_matches_places_already_in_the_trip(self):
        text = "Shenzhen North Station - Hong Kong West Kowloon shuttle bus [2+1 seats]\nFrom A$20"
        with mock.patch("journeys.manage_views.geocode.search", return_value=[]):
            result = self.post({"text": text}).json()
        self.assertEqual(result["origin"]["id"], self.places["szn"].pk)
        self.assertEqual(result["destination"]["id"], self.places["xkl"].pk)
        self.assertEqual(result["fields"]["mode"], "shuttle")

    def test_robot_check_asks_for_the_text(self):
        with mock.patch("journeys.leg_import.fetch_page", return_value="<title>Trip.com-Verification</title>To continue, please complete the verification test"):
            response = self.post({"url": "https://au.trip.com/things-to-do/detail/101347044"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("paste the text", response.json()["error"])

    def test_transport_form_offers_the_link_box_and_keeps_the_link(self):
        page = self.client.get(reverse("manage:leg_create", args=[self.trip.pk])).content.decode()
        self.assertIn('data-page-import="leg"', page)
        self.assertIn('name="url"', page)
