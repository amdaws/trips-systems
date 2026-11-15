import io
import json
import urllib.error
from datetime import date
from unittest import mock

from django.test import TestCase, override_settings

from journeys import flights
from journeys.cities import derive_city
from journeys.models import FlightLookup, Leg, Place, Trip
from journeys.search import parse_leg

from .factories import place

# Shaped like AeroDataBox's FlightContract (from its OpenAPI specification).
SAMPLE = [{
    "number": "CX 156",
    "airline": {"name": "Cathay Pacific", "iata": "CX"},
    "aircraft": {"model": "Airbus A350-900"},
    "departure": {"airport": {"iata": "BNE", "name": "Brisbane", "municipalityName": "Brisbane", "countryCode": "AU",
                              "location": {"lat": -27.38, "lon": 153.12}},
                  "scheduledTime": {"utc": "2026-12-26 14:50Z", "local": "2026-12-27 00:50+10:00"}},
    "arrival": {"airport": {"iata": "HKG", "name": "Hong Kong", "municipalityName": "Hong Kong", "countryCode": "HK",
                            "location": {"lat": 22.31, "lon": 113.91}},
                "scheduledTime": {"utc": "2026-12-26 23:20Z", "local": "2026-12-27 07:20+08:00"}},
}]


def fake_response(payload, status=200):
    response = mock.MagicMock()
    response.status = status
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    return response


@override_settings(AERODATABOX_RAPIDAPI_KEY="test-key")
class FlightLookupTests(TestCase):
    def setUp(self):
        place("Brisbane International Airport", Place.Kind.AIRPORT, -27.38, 153.12, code="BNE", country="AU", tz="Australia/Brisbane", city="Brisbane")
        self.trip = Trip.objects.create(title="T", slug="t", start_date=date(2026, 12, 27), end_date=date(2027, 1, 31))

    def test_lookup_fills_airports_and_times_and_caches(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response(SAMPLE)) as urlopen:
            result = flights.lookup("CX 156", date(2026, 12, 27))
            again = flights.lookup("CX156", date(2026, 12, 27))
        self.assertEqual(urlopen.call_count, 1)  # second time from the cache
        request = urlopen.call_args[0][0]
        self.assertIn("/flights/number/CX156/2026-12-27", request.full_url)
        self.assertEqual(request.get_header("X-rapidapi-key"), "test-key")
        self.assertEqual((result["origin"].code, result["destination"].code), ("BNE", "HKG"))
        self.assertEqual((result["depart_time"], result["arrive_time"], result["operator"]), ("00:50", "07:20", "Cathay Pacific"))
        self.assertEqual(again["destination"].city, "Hong Kong")  # airport created from the flight data
        self.assertEqual(FlightLookup.objects.count(), 1)

    def test_auto_fill_uses_the_live_schedule(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            r = parse_leg("CX156 27 Dec", trip=self.trip)
        self.assertEqual((r["origin"]["code"], r["destination"]["code"], r["depart_time"], r["arrive_time"]), ("BNE", "HKG", "00:50", "07:20"))
        self.assertTrue(any("AeroDataBox" in m for m in r["messages"]))

    def test_not_found_and_bad_key(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response([], status=204)):
            r = parse_leg("CX999", trip=self.trip, depart_date=date(2027, 1, 5))
        self.assertTrue(any("No published schedule" in m for m in r["messages"]))
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            r = parse_leg("CX998", trip=self.trip, depart_date=date(2027, 1, 5))
        self.assertTrue(any("rejected the API key" in m for m in r["messages"]))

    @override_settings(AERODATABOX_RAPIDAPI_KEY="")
    def test_off_without_a_key(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            parse_leg("CX156 27 Dec", trip=self.trip)
        urlopen.assert_not_called()


class CityTests(TestCase):
    def test_cities_and_pinyin(self):
        cases = [
            (Place(kind="station", name="Beijing West", name_local="北京西", country="CN"), ("Beijing", "北京"), "Beijingxi"),
            (Place(kind="station", name="Xianggelila (Shangri-La)", name_local="香格里拉", country="CN"), ("Shangri-La", "香格里拉"), "Xianggelila"),
            (Place(kind="station", name="Shangrao", name_local="上饶", country="CN"), ("Shangrao", "上饶"), ""),
            (Place(kind="station", name="Hong Kong West Kowloon", name_local="香港西九龍", country="HK"), ("Hong Kong", "香港"), ""),
            (Place(kind="airport", name="Shanghai Pudong International Airport", code="PVG", aliases="Shanghai (Pudong)", source="ourairports", country="CN"), ("Shanghai", ""), ""),
            (Place(kind="airport", name="Taiwan Taoyuan International Airport", code="TPE", aliases="Taoyuan", source="ourairports", country="TW"), ("Taipei", ""), ""),
        ]
        for p, city, pinyin in cases:
            with self.subTest(place=p.name):
                self.assertEqual(derive_city(p), city)
                self.assertEqual(p.pinyin, pinyin)

    def test_lookup_links(self):
        bne = place("Brisbane International Airport", Place.Kind.AIRPORT, -27.38, 153.12, code="BNE", country="AU", city="Brisbane")
        hkg = place("Hong Kong International Airport", Place.Kind.AIRPORT, 22.31, 113.91, code="HKG", country="HK")
        cq = place("Chongqing East", Place.Kind.STATION, 29.49, 106.67, local="重庆东")
        zj = place("Zhangjiajie West", Place.Kind.STATION, 29.17, 110.46, local="张家界西")
        trip = Trip.objects.create(title="T", slug="t", start_date=date(2027, 1, 1), end_date=date(2027, 1, 31))
        flight = Leg(trip=trip, mode="flight", number="CX156", origin=bne, destination=hkg, depart_date=date(2027, 1, 2))
        train = Leg(trip=trip, mode="train", number="G2431", origin=cq, destination=zj, depart_date=date(2027, 1, 12))
        flight_links, train_links = dict(flight.lookup_links()), dict(train.lookup_links())
        self.assertIn("Flights%20from%20BNE%20to%20HKG%20on%202027-01-02", flight_links["Google Flights"])
        self.assertEqual(flight_links["Trip.com"], "https://au.trip.com/flights/brisbane-to-hong-kong/airfares-bne-hkg/")
        self.assertEqual(train_links["gaotie.net timetable"], "https://www.gaotie.net/train/G2431/")
        self.assertEqual(train_links["Trip.com trains"], "https://au.trip.com/trains/china/route/chongqing-to-zhangjiajie/")
        self.assertEqual(str(train), "G2431 Train Chongqing → Zhangjiajie")
