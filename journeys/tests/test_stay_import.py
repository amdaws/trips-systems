import json
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from journeys import stay_import
from journeys.models import Place, Trip

# Trip.com nests schema.org data inside escaped strings in its page scripts.
TRIPCOM_LIKE = (
    '<script>self.__next_f.push([1,"{\\"structuredData\\":[\\"{\\\\\\"@type\\\\\\":\\\\\\"Hotel\\\\\\",\\\\\\"name\\\\\\":'
    '\\\\\\"Formosa 101 Hostel\\\\\\",\\\\\\"address\\\\\\":{\\\\\\"streetAddress\\\\\\":\\\\\\"5F, No. 115, Section 2, Keelung Rd, '
    'Xinyi District, Taipei\\\\\\",\\\\\\"addressRegion\\\\\\":\\\\\\"Taipei\\\\\\",\\\\\\"@type\\\\\\":\\\\\\"PostalAddress\\\\\\"}}\\"]}"])'
    '</script><script>{"mapType":"gg","lat":"25.028762","lng":"121.557218","tels":[{"show":"+886-955780359"}],'
    '"email":"hostel@example.com","content":[{"title":"Check-in: ","description":"After 15:00"},'
    '{"title":"Check-out: ","description":"Before 11:00"}]}</script>'
)
JSON_LD_PAGE = """<html><head><title>Harbour Hotel | Book now</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Hotel","name":"Harbour Hotel",
"address":{"@type":"PostalAddress","streetAddress":"1 Pier St","addressLocality":"Hong Kong","addressCountry":"HK"},
"telephone":"+852 1234 5678","checkinTime":"14:00","checkoutTime":"T12:00:00",
"geo":{"@type":"GeoCoordinates","latitude":22.29,"longitude":114.17}}</script></head><body></body></html>"""
PASTED = """Formosa 101 Hostel
Address: 5F, No. 115, Section 2, Keelung Rd, Xinyi District, Taipei
Check-in: After 15:00   Check-out: Before 11:00
Phone: +886 955 780 359"""


class ExtractTests(SimpleTestCase):
    def test_tripcom_page(self):
        found = stay_import.extract(TRIPCOM_LIKE, "https://au.trip.com/hotels/taipei-hotel-detail-131313845/formosa-101-hostel")
        self.assertEqual(found["name"], "Formosa 101 Hostel")
        self.assertTrue(found["address"].startswith("5F, No. 115, Section 2, Keelung Rd"))
        self.assertEqual((found["lat"], found["lon"]), ("25.028762", "121.557218"))
        self.assertEqual((found["phone"], found["email"]), ("+886-955780359", "hostel@example.com"))
        self.assertEqual((found["check_in_time"], found["check_out_time"]), ("15:00", "11:00"))
        self.assertEqual(found["site"], "au.trip.com")

    def test_schema_org_page(self):
        found = stay_import.extract(JSON_LD_PAGE, "https://www.example-hotel.com/")
        self.assertEqual((found["name"], found["phone"]), ("Harbour Hotel", "+852 1234 5678"))
        self.assertEqual(found["address"], "1 Pier St, Hong Kong, HK")
        self.assertEqual((found["check_in_time"], found["check_out_time"]), ("14:00", "12:00"))
        self.assertEqual((found["lat"], found["lon"]), (22.29, 114.17))

    def test_pasted_text(self):
        found = stay_import.extract(PASTED)
        self.assertTrue(found["address"].startswith("5F, No. 115"))
        self.assertEqual((found["check_in_time"], found["check_out_time"], found["phone"]), ("15:00", "11:00", "+886 955 780 359"))

    def test_robot_check_page(self):
        with self.assertRaisesMessage(stay_import.PageError, "robot check"):
            stay_import.extract("<html><title>Challenge Validation</title><div class='captcha'></div></html>")

    def test_only_public_addresses_are_fetched(self):
        for url in ["http://localhost/", "http://127.0.0.1:8001/manage/", "http://10.0.0.5/", "file:///etc/passwd", "http://169.254.169.254/latest/meta-data/"]:
            with self.subTest(url=url), self.assertRaises(stay_import.PageError):
                stay_import._check_public(url)
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.10", 443))]):
            with self.assertRaises(stay_import.PageError):
                stay_import._check_public("https://sneaky.example/")  # public name, private address
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("104.18.1.1", 443))]):
            stay_import._check_public("https://au.trip.com/hotels/x")

    def test_connected_address_is_checked_too(self):
        def response(address):
            sock = mock.Mock(getpeername=mock.Mock(return_value=(address, 443)))
            return mock.Mock(fp=mock.Mock(raw=mock.Mock(_sock=sock)))

        self.assertTrue(stay_import._peer_is_public(response("104.18.1.1")))
        self.assertFalse(stay_import._peer_is_public(response("127.0.0.1")))  # name re-pointed after the check
        self.assertFalse(stay_import._peer_is_public(response("169.254.169.254")))
        self.assertFalse(stay_import._peer_is_public(object()))  # can't tell, so refuse

    def test_compressed_pages_are_size_capped(self):
        import gzip
        import zlib

        self.assertEqual(stay_import._inflate(gzip.compress(b"<html>"), "gzip"), b"<html>")
        self.assertEqual(stay_import._inflate(zlib.compress(b"<html>"), "deflate"), b"<html>")
        with self.assertRaisesMessage(stay_import.PageError, "too large"):
            stay_import._inflate(gzip.compress(b"0" * (stay_import.MAX_BYTES + 1)), "gzip")
        with self.assertRaises(stay_import.PageError):
            stay_import._inflate(b"not compressed", "gzip")


class StayImportViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "a@example.com", None))
        self.trip = Trip.objects.create(title="T", slug="t", start_date=date(2026, 12, 27), end_date=date(2027, 1, 31))
        Place.objects.create(kind=Place.Kind.CITY, name="Taipei", name_local="台北", lat=25.04, lon=121.56, country="TW", tz="Asia/Taipei")
        self.url = reverse("manage:api_stay_extract", args=[self.trip.pk])

    def post(self, body):
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def test_link_fills_fields_city_and_private_map_pin(self):
        with mock.patch("journeys.stay_import.fetch_page", return_value=TRIPCOM_LIKE):
            data = self.post({"url": "https://au.trip.com/hotels/taipei-hotel-detail-131313845/formosa-101-hostel"}).json()
        self.assertEqual(data["fields"]["phone"], "+886-955780359")
        self.assertEqual(data["city"]["name"], "Taipei")
        pin = Place.objects.get(pk=data["location"]["id"])
        self.assertEqual((pin.kind, pin.source_id, round(pin.lat, 3)), (Place.Kind.HOTEL, "trip.com/131313845", 25.029))

    def test_blocked_site_message(self):
        with mock.patch("journeys.stay_import.fetch_page", side_effect=stay_import.PageError("That site blocks automatic reading. Paste the page text below instead.")):
            response = self.post({"url": "https://hotels.com/ho1"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Paste the page text", response.json()["error"])

    def test_pasted_text(self):
        data = self.post({"text": PASTED}).json()
        self.assertEqual(data["fields"]["check_in_time"], "15:00")

    def test_stay_form_offers_link_box(self):
        self.assertContains(self.client.get(reverse("manage:stay_create", args=[self.trip.pk])), "Fill from a hotel link")
