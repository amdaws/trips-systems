from datetime import date, time
from io import BytesIO
from unittest import mock

from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook

from journeys.geo import great_circle, simplify, tidy_station_name
from journeys.models import Place, Trip
from journeys.routing import _shortest_path
from journeys.xlsx_import import TRANSPORT_RE, import_workbook

from .factories import place


class GeoTests(SimpleTestCase):
    def test_great_circle_starts_and_ends_at_the_airports(self):
        path = great_circle([-27.38, 153.12], [22.31, 113.91])
        self.assertAlmostEqual(path[0][0], -27.38, places=3)
        self.assertAlmostEqual(path[-1][1], 113.91, places=3)
        self.assertGreater(len(path), 50)

    def test_simplify_drops_points_on_a_straight_line(self):
        line = [[0, i / 10] for i in range(100)]
        self.assertEqual(len(simplify(line)), 2)

    def test_station_names(self):
        self.assertEqual(tidy_station_name("Zhangjiajiexi", "张家界西"), "Zhangjiajie West")
        self.assertEqual(tidy_station_name("Chongqingdong Railway Station", "重庆东"), "Chongqing East")
        self.assertEqual(tidy_station_name("Shenzhen North", "深圳北"), "Shenzhen North")
        self.assertEqual(tidy_station_name("Hexi", "河西"), "Hexi")

    def test_shortest_path_follows_the_network(self):
        # A square of track: going A -> C must go via B or D, not straight across.
        a, b, c, d = (0.0, 0.0), (0.0, 0.1), (0.1, 0.1), (0.1, 0.0)
        ways = {"elements": [
            {"type": "way", "nodes": [1, 2, 3], "tags": {}, "geometry": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}, {"lat": c[0], "lon": c[1]}]},
            {"type": "way", "nodes": [1, 4], "tags": {}, "geometry": [{"lat": a[0], "lon": a[1]}, {"lat": d[0], "lon": d[1]}]},
        ]}
        path = _shortest_path(ways, list(a), list(c), lambda tags: 1.0)
        self.assertEqual(path, [[0.0, 0.0], [0.0, 0.1], [0.1, 0.1]])
        self.assertIsNone(_shortest_path(ways, [5.0, 5.0], list(c), lambda tags: 1.0))  # too far from any track


class TransportTextTests(SimpleTestCase):
    def test_overnight_train(self):
        m = TRANSPORT_RE.search("Train - D901 2128hrs Shijiazhuang -> Shenzhenbei (North) 0818hrs+1")
        self.assertEqual((m["number"], m["origin"], m["destination"], m["arrive"], m["next_day"]),
                         ("D901", "Shijiazhuang", "Shenzhenbei (North)", "0818", "+1"))

    def test_business_class_and_typo(self):
        m = TRANSPORT_RE.search("Train - G2431 1238hrs Chongqing East -> Zhnagjaijeixi 1522hrs (Business Class)")
        self.assertEqual((m["destination"], m["arrive"], m["seat"]), ("Zhnagjaijeixi", "1522", "Business Class"))

    def test_arrival_without_hrs_and_missing_arrival(self):
        self.assertEqual(TRANSPORT_RE.search("Train - G3206 1235hrs Yiwu -> Hangzhouxi 1309")["arrive"], "1309")
        m = TRANSPORT_RE.search("Train: C988 1501hrs Shangri la city -> Lijiang ")
        self.assertEqual((m["destination"], m["arrive"]), ("Lijiang", None))


class ImportTests(TestCase):
    def workbook(self):
        wb = Workbook()
        wb.active.title = "Overview"
        wb["Overview"].append(["Table 1"])
        wb["Overview"].append([None, "Train tickets open 30 days before"])
        overview = wb.create_sheet("Intinerary (Overview)")
        overview.append(["Table 1"])
        overview.append(["Day", "Date", "Activity", "Overnight Stay"])
        overview.append(["Fri", "1/1/27", "Flight / Hong Kong", "Hong Kong"])
        overview.append(["Sat", "2/1/27", "Train / Shenzhen", "Shenzhen"])
        detailed = wb.create_sheet("Intinerary (Detailed)")
        detailed.append(["Table 1"])
        detailed.append(["Date", "City/Location", "Time", "Transport", "Activities", "Notes", "Links"])
        detailed.append(["1/1/27", None, "0000-0600", "Flight: CX156 0050hrs BNE -> HKG 0720hrs", None, None, None])
        detailed.append([None, None, "1800-2400", None, "Harbour walk\nNight market", "Bring cash", "Guide - https://example.com/guide"])
        detailed.append(["2/1/27", None, "0600-1200", "Train: G388 0720hrs Hong Kong -> Shenzhenbei 0744hrs", None, None, None])
        stays = wb.create_sheet("Accomodations")
        stays.append(["Accomodation Options"])
        stays.append([None, "Booked", "Booking No.", "Accom Name", "Address", "Check IN - Check OUT", "Nearest Metro"])
        stays.append(["Hong Kong", "Y", "123", "Test Inn", "1 Nathan Rd", "1500hrs 1 Jan - 2 Jan 1100hrs", "TST"])
        budget = wb.create_sheet("Budgets")
        budget.append(["Table 1"])
        budget.append([None, "Activity", "AUD Est", "AUD Actuals", "Budgeted Y/N", "Booked Y/N", "Purchased Y/N", "Funded Y/N", "Paid By"])
        budget.append([None, "Hotel-Hong Kong", 300, 250.5, "Y", "Y", "Y", "N", "JY"])
        budget.append([None, "Train-x", None, None, None, None, None, None, None])
        for name in ("Notes", "Data"):
            wb.create_sheet(name)
        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        return buffer

    def test_import(self):
        place("Brisbane International Airport", Place.Kind.AIRPORT, -27.38, 153.12, code="BNE", tz="Australia/Brisbane")
        place("Hong Kong International Airport", Place.Kind.AIRPORT, 22.31, 113.91, code="HKG", tz="Asia/Hong_Kong")
        place("Hong Kong West Kowloon", Place.Kind.STATION, 22.304, 114.165, local="香港西九龍", tz="Asia/Hong_Kong")
        place("Shenzhen North", Place.Kind.STATION, 22.612, 114.024, local="深圳北")
        with mock.patch("journeys.geocode.search", return_value=[]):
            trip = import_workbook(self.workbook(), title="Test", slug="test", log=lambda *a: None)
        self.assertEqual(trip.visibility, Trip.Visibility.PRIVATE)
        self.assertEqual((trip.start_date, trip.end_date, trip.train_ticket_window), (date(2027, 1, 1), date(2027, 1, 2), 30))
        self.assertEqual(trip.legs.count(), 2)
        train = trip.legs.get(number="G388")
        self.assertEqual((train.origin.name, train.destination.name, train.arrive_time), ("Hong Kong West Kowloon", "Shenzhen North", time(7, 44)))
        items = list(trip.days.get(date=date(2027, 1, 1)).items.order_by("order"))
        self.assertEqual([i.title for i in items], ["Harbour walk", "Night market"])
        self.assertEqual(items[0].private_notes, "Bring cash")
        self.assertIn("https://example.com/guide", items[0].links)
        stay = trip.stays.get()
        self.assertEqual((stay.name, stay.booking_ref, stay.check_in_time), ("Test Inn", "123", time(15, 0)))
        item = trip.budget_items.get()
        self.assertEqual((item.description, str(item.actual), item.paid_by.initials, item.stay), ("Hotel: Hong Kong", "250.50", "JY", stay))
        self.assertEqual(trip.checklist.get().leg, train)
        self.assertEqual(trip.days.get(date=date(2027, 1, 1)).overnight.name, "Hong Kong")


class AutoFillTests(TestCase):
    """The transport auto-fill understands the formats people actually type."""

    def setUp(self):
        from journeys.models import Airline, Place

        Airline.objects.create(code="CX", name="Cathay Pacific")
        place("Brisbane International Airport", Place.Kind.AIRPORT, -27.38, 153.12, code="BNE", tz="Australia/Brisbane").__class__.objects.filter(code="BNE").update(aliases="Brisbane")
        place("Hong Kong International Airport", Place.Kind.AIRPORT, 22.31, 113.91, code="HKG", tz="Asia/Hong_Kong")
        for name, local, lat, lon in [("Hong Kong West Kowloon", "香港西九龍", 22.304, 114.165), ("Shenzhen North", "深圳北", 22.612, 114.024),
                                      ("Furong Town", "芙蓉镇", 28.77, 109.98), ("Zhangjiajie West", "张家界西", 29.17, 110.46)]:
            place(name, Place.Kind.STATION, lat, lon, local=local)
        for p in Place.objects.all():
            p.save()  # rebuild search text (aliases, pinyin)
        self.trip = Trip.objects.create(title="T", slug="t", start_date=date(2026, 12, 27), end_date=date(2027, 1, 31))

    def parse(self, text, **kwargs):
        from journeys.search import parse_leg

        r = parse_leg(text, trip=self.trip, **kwargs)
        return {k: (v["name"] if isinstance(v, dict) else v) for k, v in r.items()}

    def test_flight_with_city_names_and_date(self):
        r = self.parse("CX156 Brisbane to Hong Kong 27 Dec 0050 0720")
        self.assertEqual((r["mode"], r["operator"], r["origin"], r["destination"]), ("flight", "Cathay Pacific", "Brisbane International Airport", "Hong Kong International Airport"))
        self.assertEqual((r["depart_date"], r["depart_time"], r["arrive_time"]), ("2026-12-27", "00:50", "07:20"))

    def test_spreadsheet_train_line(self):
        r = self.parse("Train: G388 0720hrs Hong Kong -> Shenzhenbei 0744hrs")
        self.assertEqual((r["mode"], r["number"], r["origin"], r["destination"]), ("train", "G388", "Hong Kong West Kowloon", "Shenzhen North"))

    def test_pinyin_typo_class_and_overnight(self):
        r = self.parse("Train - G9 2128hrs Furongzhen -> Zhnagjaijeixi 0818hrs+1 (Business Class)", depart_date=date(2027, 1, 14))
        self.assertEqual((r["origin"], r["destination"], r["seat_class"]), ("Furong Town", "Zhangjiajie West", "Business"))
        self.assertEqual((r["next_day"], r["arrive_date"]), (True, "2027-01-15"))

    def test_short_date_and_am_pm(self):
        r = self.parse("G1367 芙蓉镇 to 张家界西 16/1 7:53am 1:39pm")
        self.assertEqual((r["depart_date"], r["depart_time"], r["arrive_time"]), ("2027-01-16", "07:53", "13:39"))

    def test_number_alone_fills_from_a_known_service(self):
        from journeys.models import Leg, Place

        Leg.objects.create(trip=self.trip, mode="flight", number="CX156", origin=Place.objects.get(code="BNE"),
                           destination=Place.objects.get(code="HKG"), depart_date=date(2026, 12, 27), depart_time=time(0, 50), arrive_time=time(7, 20))
        r = self.parse("CX156")
        self.assertEqual((r["origin"], r["destination"], r["depart_time"], r["arrive_time"]),
                         ("Brisbane International Airport", "Hong Kong International Airport", "00:50", "07:20"))


class GapTests(SimpleTestCase):
    def test_tracks_that_pass_close_without_a_junction_are_joined_when_needed(self):
        # Two lines run past each other 15 m apart with no shared node (like Guilin West in the map data).
        north = {"type": "way", "nodes": [1, 2, 3], "tags": {},
                 "geometry": [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.05}, {"lat": 0.0, "lon": 0.1}]}
        south = {"type": "way", "nodes": [4, 5, 6], "tags": {},
                 "geometry": [{"lat": 0.000135, "lon": 0.05}, {"lat": 0.05, "lon": 0.05}, {"lat": 0.1, "lon": 0.05}]}
        path = _shortest_path({"elements": [north, south]}, [0.0, 0.0], [0.1, 0.05], lambda tags: 1.0)
        self.assertIsNotNone(path)
        self.assertEqual(path[-1], [0.1, 0.05])

    def test_far_apart_pieces_stay_separate(self):
        a = {"type": "way", "nodes": [1, 2], "tags": {}, "geometry": [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.05}]}
        b = {"type": "way", "nodes": [3, 4], "tags": {}, "geometry": [{"lat": 0.02, "lon": 0.05}, {"lat": 0.1, "lon": 0.05}]}
        self.assertIsNone(_shortest_path({"elements": [a, b]}, [0.0, 0.0], [0.1, 0.05], lambda tags: 1.0))  # 2 km gap
