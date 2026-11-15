import io
import json
import urllib.error
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from journeys import metro, presenters
from journeys.models import Leg, MetroNetwork, Place, Stay

from .factories import place, sample_trip

# Shaped like the station list embedded in MetroMan's /en/maps/<city> page.
OPTIONS = {
    "lines": [
        {"slug": "airport-express", "name": "Airport Express", "color": "#00888A", "textColor": "#fff"},
        {"slug": "tsuen-wan-line", "name": "Tsuen Wan Line", "color": "#E2231A", "textColor": "#fff"},
        {"slug": "tuen-ma-line", "name": "Tuen Ma Line", "color": "#9A3820", "textColor": "red;background:url(x)"},
    ],
    "stations": [
        {"slug": "airport", "name": "Airport", "secondaryName": "機場", "lines": ["airport-express"], "url": "/en/cities/hongkong/stations/airport",
         "search": "机场|airport|機場|"},
        {"slug": "tsim-sha-tsui", "name": "Tsim Sha Tsui", "secondaryName": "尖沙咀", "lines": ["tsuen-wan-line"],
         "url": "/en/cities/hongkong/stations/tsim-sha-tsui", "search": "尖沙咀|tsim sha tsui|"},
        {"slug": "austin", "name": "Austin", "secondaryName": "柯士甸", "lines": ["tuen-ma-line"], "url": "/en/cities/hongkong/stations/austin",
         "search": "柯士甸|austin|"},
        {"slug": "nowhere", "name": "Nowhere", "secondaryName": "無處", "lines": [], "url": "/en/cities/hongkong/stations/nowhere", "search": ""},
    ],
}
PAGE = f'<html><script type="application/json" class="mm-map-options">{json.dumps(OPTIONS)}</script></html>'
OSM_STATIONS = {"elements": [
    {"type": "node", "lat": 22.3158, "lon": 113.9365, "tags": {"railway": "station", "station": "subway", "name": "機場 Airport"}},
    {"type": "node", "lat": 22.2975, "lon": 114.1722, "tags": {"railway": "station", "station": "subway", "name": "尖沙咀 Tsim Sha Tsui"}},
    {"type": "way", "center": {"lat": 22.3045, "lon": 114.1665}, "tags": {"public_transport": "station", "subway": "yes", "name:en": "Austin"}},
    {"type": "node", "lat": 22.40, "lon": 114.10, "tags": {"railway": "station", "name": "Austin"}},  # a railway station of the same name further out
]}
EXITS = {"elements": [
    {"type": "node", "lat": 22.2979, "lon": 114.1719, "tags": {"railway": "subway_entrance", "ref": "B1"}},
    {"type": "node", "lat": 22.2960, "lon": 114.1740, "tags": {"railway": "subway_entrance", "ref": "L5"}},
]}


def fake_overpass(query, timeout=200):
    return EXITS if "subway_entrance" in query else OSM_STATIONS


OSRM = {"code": "Ok", "routes": [{"duration": 1500, "distance": 12345}]}
ROUTE = {"status": "Ok", "plans": [{"duration": "48 min", "fare": "HK$84", "transfers": "1 transfer", "legs": [
    {"ride": True, "name": "Airport Express", "color": "#009999", "textColor": "#000", "from": "Airport", "to": "Austin",
     "points": [{"slug": "airport"}, {"slug": "austin"}], "stopsText": "1 stop"},
    {"ride": False, "name": "In-station transfer", "color": "#687684", "points": [{"slug": "austin"}, {"slug": "austin"}]},
    {"ride": True, "name": "Tsuen Wan Line", "color": "#E2231A", "textColor": "#fff", "from": "Austin", "to": "Tsim Sha Tsui",
     "points": [{"slug": "austin"}, {"slug": "tsim-sha-tsui"}], "stopsText": "1 stop"},
]}]}


def fake_page(url):
    if "/hongkong/routes?" in url:
        return json.dumps(ROUTE)
    if url.endswith("/hongkong"):
        return PAGE
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class MetroTests(TestCase):
    def setUp(self):
        self.trip, self.places = sample_trip()
        self.stay = Stay.objects.get(trip=self.trip)
        self.hotel = place("Harbour Hotel", Place.Kind.HOTEL, 22.2990, 114.1720, country="HK", tz="Asia/Hong_Kong")

    def lookup(self, stay=None, geocoded=None):
        with mock.patch("journeys.metro._fetch_page", side_effect=fake_page), \
             mock.patch("journeys.metro.overpass", side_effect=fake_overpass), \
             mock.patch("journeys.metro.get_json", return_value=OSRM) as drive, \
             mock.patch("journeys.metro.geocode.search", return_value=geocoded or []) as search:
            info = metro.refresh_stay(stay or self.stay)
        self.search, self.drive = search, drive
        return info

    def guilin_stay(self):
        """A stay in a city MetroMan has no map of, reached by train."""
        guilin = place("Guilin", Place.Kind.CITY, 25.27, 110.29)
        station = place("Guilin West", Place.Kind.STATION, 25.26, 110.20, local="桂林西")
        hotel = place("Riverside Inn", Place.Kind.HOTEL, 25.28, 110.29)
        Leg.objects.create(trip=self.trip, mode=Leg.Mode.TRAIN, number="G2902", origin=self.places["szn"], destination=station,
                           depart_date=date(2027, 1, 2))
        return Stay.objects.create(trip=self.trip, label="Guilin", city=guilin, location=hotel,
                                   check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3))

    def test_network_is_matched_to_map_positions(self):
        self.stay.location = self.hotel
        self.lookup()
        network = MetroNetwork.objects.get(slug="hongkong")
        by_slug = {s["slug"]: s for s in network.stations}
        self.assertEqual((by_slug["airport"]["lat"], by_slug["airport"]["lon"]), (22.3158, 113.9365))  # "機場 Airport" matched
        self.assertEqual(by_slug["austin"]["lat"], 22.3045)  # the metro station, not the railway station of the same name
        self.assertIsNone(by_slug["nowhere"]["lat"])
        self.assertEqual(network.lines["tuen-ma-line"]["text"], "")  # unsafe colours are dropped

    def test_nearest_station_and_exit(self):
        self.stay.location = self.hotel
        info = self.lookup()
        self.assertEqual(info["station"], "tsim-sha-tsui")
        self.assertEqual(info["exit"], "B1")
        self.assertLess(info["distance_m"], 300)
        self.assertEqual(Stay.objects.get(pk=self.stay.pk).metro["station"], "tsim-sha-tsui")

    def test_named_station_wins(self):
        self.stay.location = self.hotel
        self.stay.nearest_metro = "Austin Station - Exit D"
        info = self.lookup()
        self.assertEqual((info["station"], info["exit"], info["manual"]), ("austin", "D", True))

    def test_address_is_placed_on_the_map(self):
        found = [{"source_id": "node/1", "name": "Harbour Hotel", "name_local": "", "kind": Place.Kind.HOTEL,
                  "lat": 22.2990, "lon": 114.1720, "country": "HK", "city": "Hong Kong", "label": "Harbour Hotel"}]
        info = self.lookup(geocoded=found)
        self.search.assert_called_once()
        self.assertTrue(info["geocoded"])
        self.assertEqual(Stay.objects.get(pk=self.stay.pk).location.name, "SECRET-HOTEL")
        self.assertEqual(info["station"], "tsim-sha-tsui")

    def test_address_queries_drop_flat_and_floor(self):
        queries = metro.address_queries(
            "Flat D1, 13F, Mirador Mansion, No.58-64 Nathan Road, Yau Tsim Mong District, Hong Kong, China", "Hong Kong")
        self.assertEqual(queries[1], "Mirador Mansion, 58 Nathan Road, Yau Tsim Mong District, Hong Kong, China")
        self.assertIn("Mirador Mansion, Hong Kong", queries)
        self.assertIn("58 Nathan Road, Hong Kong", queries)
        self.assertNotIn("Yau Tsim Mong District, Hong Kong", queries)  # a whole district is too vague
        taipei = metro.address_queries("5F, No. 115, Section 2, Keelung Rd, Xinyi District, Taipei, Taiwan, China", "Taipei")
        self.assertEqual(taipei[1], "115, Section 2, Keelung Rd, Xinyi District, Taipei, Taiwan, China")

    def test_address_far_from_the_named_station_is_ignored(self):
        self.stay.nearest_metro = "Tsim Sha Tsui, Exit B1"
        elsewhere = [{"source_id": "node/2", "name": "Another Inn", "name_local": "", "kind": Place.Kind.HOTEL,
                      "lat": 22.3700, "lon": 114.1100, "country": "HK", "city": "Hong Kong", "label": "Another Inn"}]
        info = self.lookup(geocoded=elsewhere)
        self.assertFalse(info["geocoded"])
        self.assertIsNone(Stay.objects.get(pk=self.stay.pk).location)
        self.assertEqual((info["station"], info["exit"]), ("tsim-sha-tsui", "B1"))  # the named station still stands

    def test_taxi_where_there_is_no_metro(self):
        stay = self.guilin_stay()
        info = self.lookup(stay)
        self.assertEqual(info["taxi"]["arrival"]["minutes"], 25)
        self.assertEqual(info["taxi"]["arrival"]["km"], 12.3)
        url = self.drive.call_args[0][0]
        self.assertIn("110.2,25.26;110.29,25.28", url)  # from the station to the hotel
        [stay] = metro.for_stays(self.trip, [Stay.objects.get(pk=stay.pk)])
        self.assertEqual(stay.metro_view["arrival_taxi"]["to"], "the hotel")
        leg = Leg.objects.get(number="G2902")
        self.assertEqual(metro.transfers_by_leg(self.trip)[leg.pk]["after"][0]["taxi"]["minutes"], 25)

    def test_no_taxi_where_the_metro_goes(self):
        self.stay.location = self.hotel
        info = self.lookup()
        self.assertEqual(info["taxi"], {})  # Airport station at the airport, Austin beside West Kowloon
        self.drive.assert_not_called()

    def test_metro_journey_is_listed_and_drawn(self):
        self.stay.location = self.hotel
        self.stay.save()
        info = self.lookup()
        self.assertEqual(info["routes"]["arrival"]["fare"], "HK$84")
        self.assertEqual([r["line"] for r in info["routes"]["arrival"]["rides"]], ["Airport Express", "Tsuen Wan Line"])  # walks left out
        pin = presenters.map_data(self.trip, private=True)["stays"][0]
        ride = pin["arrival"]["route"]["rides"][0]
        self.assertEqual(ride["path"], [[22.3158, 113.9365], [22.3045, 114.1665]])  # Airport -> Austin, station by station
        days = presenters.timeline(self.trip, private=True)
        flight = days[0]["blocks"][0]["entries"][0]["obj"]
        self.assertEqual(flight.transfers["after"][0]["metro"]["route"]["duration"], "48 min")

    def test_owner_gets_hotels_on_the_public_map_but_visitors_do_not(self):
        self.stay.location = self.hotel
        self.stay.save()
        self.lookup()
        url = reverse("journeys:map", args=["test-trip"])
        public = self.client.get(url)
        self.assertEqual(public.json()["stays"], [])  # the stay is private
        self.assertIn("max-age=300", public["Cache-Control"])
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        owner = self.client.get(url)
        self.assertEqual(owner.json()["stays"][0]["metro"]["name"], "Tsim Sha Tsui")
        self.assertIn("no-store", owner["Cache-Control"])

    def test_public_stay_shows_its_metro_but_never_the_hotel(self):
        self.stay.location = self.hotel
        self.stay.is_public = True
        self.stay.save()
        self.lookup()
        page = self.client.get(reverse("journeys:trip", args=["test-trip"])).content.decode()
        self.assertIn("On to Hong Kong 1", page)
        self.assertIn("Airport → Tsim Sha Tsui", page)
        self.assertNotIn("22.299,", page)  # the hotel's position (22.2990, 114.1720) isn't in the animated route
        stays = self.client.get(reverse("journeys:stays", args=["test-trip"])).content.decode()
        self.assertIn("Tsim Sha Tsui", stays)
        self.assertIn("Hong Kong 1", stays)
        self.assertNotIn("SECRET-HOTEL", stays)
        self.assertNotIn("SECRET-ADDRESS", stays)
        self.assertNotIn("SECRETBOOKING", stays)
        pin = self.client.get(reverse("journeys:map", args=["test-trip"])).json()["stays"][0]
        self.assertNotIn("lat", pin)
        self.assertNotIn("name", pin)
        self.assertEqual(pin["metro"]["name"], "Tsim Sha Tsui")

    def test_private_stay_transfers_show_to_the_owner_not_visitors(self):
        self.stay.location = self.hotel
        self.stay.save()
        self.lookup()
        url = reverse("journeys:trip", args=["test-trip"])
        public = self.client.get(url)
        self.assertNotContains(public, "Tsim Sha Tsui")
        self.assertNotContains(public, "On to Hong Kong 1")
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        owner = self.client.get(url)
        self.assertContains(owner, "On to Hong Kong 1")
        self.assertContains(owner, "Airport → Tsim Sha Tsui")
        self.assertContains(owner, '<span class="badge">Private</span>', html=True)
        self.assertIn("private", owner["Cache-Control"])
        self.assertNotContains(owner, "SECRET-HOTEL")  # the stay's label only, never the hotel itself

    def test_overnight_stops_start_folded_on_the_trip_page(self):
        public = self.client.get(reverse("journeys:trip", args=["test-trip"])).content.decode()
        self.assertIn('<details class="chip-pop">', public)  # behind the "overnight stops" chip
        self.assertIn("1</strong> overnight stop<", public)  # from the stays: Hong Kong 1
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        planner = self.client.get(reverse("manage:trip", args=[self.trip.pk])).content.decode()
        self.assertNotIn("stops-card", planner)  # the planner shows nights and weather with the stays instead

    def test_moving_a_flight_looks_again(self):
        self.stay.metro = {"network": "hongkong", "station": "austin"}
        self.stay.save()
        metro.legs_changed(self.trip, date(2027, 1, 1))
        self.assertTrue(Stay.objects.get(pk=self.stay.pk).metro["pending"])

    def test_city_without_metro(self):
        self.stay.city = place("Guilin", Place.Kind.CITY, 25.27, 110.29)
        info = self.lookup()
        self.assertIn("no metro map for Guilin", info["message"])

    def test_links_from_arrival_and_to_departure(self):
        self.stay.location = self.hotel
        self.lookup()
        self.stay.refresh_from_db()
        [stay] = metro.for_stays(self.trip, [self.stay])
        view = stay.metro_view
        self.assertEqual(view["station"]["name"], "Tsim Sha Tsui")
        self.assertEqual(view["station"]["lines"][0]["colour"], "#E2231A")
        # Arrive by the flight at Hong Kong airport; leave by the train from West Kowloon (next to Austin).
        self.assertEqual(view["arrival"]["url"], "https://www.metroman.cn/en/maps/hongkong?from=airport&to=tsim-sha-tsui")
        self.assertEqual(view["departure"]["url"], "https://www.metroman.cn/en/maps/hongkong?from=tsim-sha-tsui&to=austin")
        self.assertEqual(view["departure"]["leg"], "High-speed train G388")

    def test_planner_map_shows_hotels_but_public_map_does_not(self):
        self.stay.location = self.hotel
        self.stay.save()
        self.lookup()
        private = presenters.map_data(self.trip, private=True)
        self.assertEqual(private["stays"][0]["metro"]["name"], "Tsim Sha Tsui")
        self.assertEqual(private["stays"][0]["address"], "SECRET-ADDRESS")
        public = json.dumps(presenters.map_data(self.trip))
        self.assertNotIn("SECRET-HOTEL", public)
        self.assertNotIn("Tsim Sha Tsui", public)

    def test_stays_page_links_to_metroman(self):
        self.stay.location = self.hotel
        self.stay.save()
        self.lookup()
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        page = self.client.get(reverse("manage:stays", args=[self.trip.pk])).content.decode()
        self.assertIn("https://www.metroman.cn/en/maps/hongkong?from=airport&amp;to=tsim-sha-tsui", page)
        self.assertIn("--line: #E2231A", page)

    def test_editing_the_address_looks_again(self):
        self.stay.metro = {"network": "hongkong", "station": "austin", "geocoded": True}
        self.stay.location = self.hotel
        self.stay.save()
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        url = reverse("manage:stay_edit", args=[self.trip.pk, self.stay.pk])
        data = {"label": "Hong Kong 1", "kind": "hotel", "name": "SECRET-HOTEL", "city": self.places["hongkong"].pk,
                "location": self.hotel.pk, "address": "A new address", "check_in_date": "2027-01-01", "check_out_date": "2027-01-02"}
        self.client.post(url, data)
        self.stay.refresh_from_db()
        self.assertEqual(self.stay.metro, {"pending": True, "geocoded": False})
        self.assertIsNone(self.stay.location)  # the old position came from the old address

    def test_background_job_runs_the_lookup(self):
        from django.core.management import call_command

        self.stay.location = self.hotel
        self.stay.save()
        with mock.patch("journeys.metro._fetch_page", side_effect=fake_page), \
             mock.patch("journeys.metro.overpass", side_effect=fake_overpass), \
             mock.patch("journeys.routing.refresh_path"), \
             mock.patch("journeys.management.commands.run_jobs.Command.trace_flights"), \
             mock.patch("journeys.management.commands.run_jobs.Command.refresh_transit"), \
             mock.patch("journeys.management.commands.run_jobs.Command.refresh_china_stations"), \
             mock.patch("journeys.management.commands.run_jobs.Command.refresh_driving"), \
             mock.patch("journeys.routing.trace_flights"), \
             mock.patch("journeys.weather.refresh_trip", return_value=0), \
             mock.patch("journeys.money.refresh_rates", return_value=0), \
             mock.patch("django.utils.timezone.localdate", return_value=date(2026, 12, 1)):
            call_command("run_jobs", stdout=io.StringIO())
        self.assertEqual(Stay.objects.get(pk=self.stay.pk).metro["station"], "tsim-sha-tsui")
