"""Claude reading pages and confirmations (journeys/claude_read.py), and Send to Trips: pages sent from
the browser by the bookmarklet. Claude is never called here: its client is replaced by a stand-in."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from journeys import booking_costs, booking_import, claude_read, link_details, stay_import
from journeys.models import ClaudeRead, Item

from .test_plans_costs import PlannerCase

KEY = override_settings(ANTHROPIC_API_KEY="test-key", CLAUDE_MODEL="claude-opus-5-5")
CONFIRMATION = """Your booking is confirmed
Hotel Example Shenzhen
Booking No. 9988776655
Check-in: Sat 2 Jan 2027 (from 3:00 PM)
Check-out: Sun 3 Jan 2027 (until 12:00 PM)
Total paid: CNY 530.00
Amount due at hotel: CNY 0.00
"""
CLAUDE_CONFIRMATION = {
    "kind": "stay", "booking_ref": "9988776655", "name": "Hotel Example Shenzhen", "name_local": "", "address": "", "phone": "",
    "email": "", "check_in_date": "2027-01-02", "check_in_time": "15:00", "check_out_date": "2027-01-03", "check_out_time": "12:00",
    "nights": 1, "room": "", "special_requests": "", "title": "", "activity_date": "", "start_time": "", "meeting_point": "",
    "pickup": "", "dropoff": "", "travellers": "2 adults", "amount_paid": "530.00", "currency": "CNY",
}
TRIPCOM = {
    "v": 1, "url": "https://au.trip.com/travel-guide/attraction/guilin/li-river-four-star-cruise-ship-1500450",
    "title": "Li River Four Star Cruise Ship - Guilin | Trip.com", "meta": {"og:description": "Cruise from Guilin to Yangshuo."},
    "ld": "", "geo": '"lat":25.20,"lng":110.32,"mapType":"gd"',
    "text": "Li River Four Star Cruise Ship\nOpening Hours\nDaily 08:00-16:00\nFrom A$ 89.00\nReviews (2,401)",
}


def answer(fields, stop_reason="end_turn"):
    """A stand-in for Claude's reply."""
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=json.dumps(fields))])


def claude(*replies):
    client = mock.MagicMock()
    client.beta.messages.create.side_effect = list(replies)
    return mock.patch("journeys.claude_read._client", return_value=client), client


class WithoutAKeyTests(TestCase):
    def test_nothing_is_sent(self):
        with mock.patch("journeys.claude_read._client", side_effect=AssertionError("called")):
            self.assertEqual(claude_read.read("confirmation", CONFIRMATION), {})
            found = booking_import.parse(CONFIRMATION)
        self.assertNotIn("read_by", found)
        self.assertEqual((found["amount"], found["currency"]), ("530.00", "CNY"))

    def test_a_zero_amount_is_not_a_cost(self):
        found = booking_import.parse("Booking confirmed\nHotel Example\nAmount due at hotel: CNY 0.00\n")
        self.assertIsNone(found["amount"])
        self.assertEqual(link_details.paid("Total: A$ 0.00"), {})


@KEY
class ReaderTests(TestCase):
    def test_the_form_is_strict(self):
        schema = claude_read.schema("confirmation")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(schema["properties"]["kind"]["enum"], ["stay", "activity", "transfer"])

    def test_confirmation_read_by_claude_wins(self):
        patch, client = claude(answer(CLAUDE_CONFIRMATION))
        with patch:
            found = booking_import.parse(CONFIRMATION)
        self.assertEqual(found["read_by"], "claude")
        self.assertEqual((found["check_in_time"], found["check_out_time"]), ("15:00", "12:00"))  # not 03:00
        self.assertEqual((found["amount"], found["currency"], found["check_in_date"]), ("530.00", "CNY", date(2027, 1, 2)))
        request = client.beta.messages.create.call_args.kwargs
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual((request["fallbacks"], request["betas"]), ("default", ["server-side-fallback-2026-07-01"]))
        self.assertEqual(request["output_config"]["effort"], "low")
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")

    def test_each_text_is_read_once(self):
        patch, client = claude(answer(CLAUDE_CONFIRMATION))
        with patch:
            booking_import.parse(CONFIRMATION)
            booking_import.parse(CONFIRMATION, subject="Booking confirmed")  # the review, then applying it
        self.assertEqual(client.beta.messages.create.call_count, 1)
        self.assertEqual(ClaudeRead.objects.count(), 1)

    def test_bad_answers_are_dropped(self):
        patch, _client = claude(answer(CLAUDE_CONFIRMATION | {"check_in_time": "3 PM", "check_in_date": "soon", "amount_paid": "0",
                                                            "currency": "XYZ", "nights": -2}))
        with patch:
            read = claude_read.read("confirmation", "Some booking")
        for gone in ("check_in_time", "check_in_date", "amount_paid", "currency", "nights"):
            self.assertNotIn(gone, read)
        self.assertEqual(read["check_out_time"], "12:00")

    def test_declines_and_faults_leave_the_patterns(self):
        patch, _client = claude(answer({}, stop_reason="refusal"))
        with patch:
            self.assertEqual(claude_read.read("activity", "A page"), {})
        failing = mock.MagicMock()
        failing.beta.messages.create.side_effect = TimeoutError("slow")
        with mock.patch("journeys.claude_read._client", return_value=failing), self.assertLogs("journeys.claude_read", "WARNING"):
            found = booking_import.parse(CONFIRMATION)
        self.assertEqual((found["amount"], "read_by" in found), ("530.00", False))

    @override_settings(CLAUDE_MODEL="claude-haiku-5-5")
    def test_haiku_asks_without_fallbacks(self):
        patch, client = claude(answer({"name": "X"} | {key: "" for key in claude_read.SCHEMAS["activity"] if key != "name"}
                                      | {"price_per_person": True}))
        with patch:
            claude_read.read("activity", "A page")
        self.assertNotIn("fallbacks", client.beta.messages.create.call_args.kwargs)

    def test_a_scanned_pdf_is_transcribed(self):
        reply = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=CONFIRMATION)])
        patch, client = claude(reply)
        with patch:
            text = booking_import.text_from_pdf(b"%PDF-1.4 no text layer")
        self.assertIn("Total paid: CNY 530.00", text)
        document = client.beta.messages.create.call_args.kwargs["messages"][0]["content"][0]
        self.assertEqual((document["type"], document["source"]["media_type"]), ("document", "application/pdf"))


class SendToTripsTests(PlannerCase):
    def post(self, kind, **body):
        name = {"activity": "manage:api_activity_extract", "stay": "manage:api_stay_extract", "leg": "manage:api_leg_extract"}[kind]
        return self.client.post(reverse(name, args=[self.trip.pk]), json.dumps(body), content_type="application/json")

    def test_the_page_offers_the_button(self):
        page = self.client.get(reverse("manage:capture")).content.decode()
        self.assertIn('href="javascript:', page)
        self.assertIn("http%3A%2F%2Ftestserver%2Fmanage%2Fcapture%2F", page)  # the bookmarklet brings pages here
        self.assertIn('id="capture-trips"', page)
        self.assertIn("Claude isn't set up yet", page)
        self.assertNotIn("__CAPTURE_URL__", page)

    def test_the_bookmarklet_has_no_comments_to_break_it(self):
        from journeys.manage_views import _bookmarklet_source

        self.assertFalse([line for line in _bookmarklet_source().splitlines() if line.strip().startswith("//")])
        self.assertIn("deflate-raw", _bookmarklet_source())

    def test_a_tripcom_page_from_the_browser(self):
        with mock.patch("journeys.link_details.geocode.search", side_effect=AssertionError("searched")):
            data = self.post("activity", capture=TRIPCOM, day=self.day.pk).json()
        fields = data["fields"]
        self.assertEqual((fields["name"], fields["opening_hours"]), ("Li River Four Star Cruise Ship", "Daily 08:00-16:00"))
        self.assertEqual((fields["amount"], fields["currency"]), ("89.00", "AUD"))
        self.assertEqual(fields["site"], "au.trip.com")
        self.assertIsNotNone(data["place"])  # pinned from the page's own data, converted from China's map system
        self.assertNotIn("read_by", fields)

    def test_a_wikipedia_page_is_pinned_from_its_own_position(self):
        capture = {"url": "https://en.wikipedia.org/wiki/Li_River", "title": "Li River - Wikipedia", "meta": {}, "ld": "",
                   "geo": '"lat":24.8331,"lon":110.6', "text": "Li River\nThe Li River is a river in Guangxi."}
        with mock.patch("journeys.link_details.geocode.search", side_effect=AssertionError("searched")):
            data = self.post("activity", capture=capture, day=self.day.pk).json()
        from journeys.models import Place

        place = Place.objects.get(pk=data["place"]["id"])
        self.assertEqual((place.name, place.lat, place.lon), ("Li River", 24.8331, 110.6))  # as given: not on China's offset system

    @KEY
    def test_claude_reads_what_the_browser_sent(self):
        reply = {"name": "Li River Cruise (4-star ship)", "address": "Zhujiang Pier, Lingchuan, Guilin", "city": "Guilin", "phone": "",
                 "email": "", "price": "640", "currency": "CNY", "price_per_person": False, "opening_hours": "Daily 08:00–16:00",
                 "description": "A day cruise down the Li River to Yangshuo."}
        patch, client = claude(answer(reply))
        with patch, mock.patch("journeys.link_details.geocode.search", return_value=[]):
            data = self.post("activity", capture=TRIPCOM).json()
        fields = data["fields"]
        self.assertEqual((fields["read_by"], fields["name"], fields["address"]), ("claude", reply["name"], reply["address"]))
        self.assertEqual((fields["amount"], fields["currency"], fields["price_is_total"]), ("640", "CNY", True))
        sent = client.beta.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Opening Hours", sent)
        self.assertIn("au.trip.com", sent)  # where it came from

    def test_a_booking_com_stay_keeps_its_dates(self):
        capture = {"url": "https://www.booking.com/hotel/au/the-langham-sydney.html?checkin=2027-01-05&checkout=2027-01-08",
                   "title": "The Langham, Sydney – Booking.com", "meta": {}, "ld": json.dumps({
                       "@type": "Hotel", "name": "The Langham, Sydney", "address": {"streetAddress": "89-113 Kent Street", "addressLocality": "Sydney"},
                       "geo": {"latitude": -33.8607, "longitude": 151.2041}}), "geo": "", "text": "Check-in from 15:00"}
        fields = self.post("stay", capture=capture).json()["fields"]
        self.assertEqual((fields["name"], fields["check_in_date"], fields["check_out_date"]), ("The Langham, Sydney", "2027-01-05", "2027-01-08"))
        self.assertTrue(fields["address"].startswith("89-113 Kent Street"))

    def test_nothing_to_read(self):
        response = self.post("leg", capture={"url": "https://example.com/", "title": "", "text": ""})
        self.assertEqual(response.status_code, 422)

    def test_a_group_price_isnt_multiplied(self):
        item = Item.objects.create(day=self.day, title="Private car", people=2)
        self.assertEqual(booking_costs.total_from_price(item, "1200", per_person=False), Decimal("1200"))
        self.assertEqual(booking_costs.total_from_price(item, "100"), Decimal("200"))

    def test_forms_point_to_send_to_trips(self):
        self.assertIn(reverse("manage:capture"), self.page("item_create"))
        self.assertIn(reverse("manage:capture"), self.client.get(reverse("manage:imports")).content.decode())


LINK = "https://au.trip.com/travel-guide/attraction/carlton/melbourne-museum-87712"
MUSEUM_PAGE = """<html><head><title>Melbourne Museum Tickets - Prices &amp; Opening Hours | Trip.com</title>
<meta name="description" content="Natural and cultural history museum in Carlton Gardens."></head>
<body><h1>Melbourne Museum</h1><p>Opening Hours</p><p>Daily 09:00-17:00</p><p>Adult A$ 15.00</p>
<p>Address: 11 Nicholson St, Carlton VIC 3053</p></body></html>"""
MUSEUM = {"name": "Melbourne Museum", "address": "11 Nicholson St, Carlton VIC 3053", "city": "Melbourne", "phone": "13 11 02",
          "email": "", "price": "15", "currency": "AUD", "price_per_person": True, "opening_hours": "Daily 09:00–17:00",
          "description": "Natural and cultural history museum in Carlton Gardens."}
ROBOT_CHECK = "<html><head><title>Trip.com-Verification</title></head><body>Please complete the captcha to continue.</body></html>"


def clock(*readings):
    """A stand-in for link_details' clock: time.monotonic() gives these readings in turn."""
    return mock.patch("journeys.link_details.time", SimpleNamespace(monotonic=mock.Mock(side_effect=list(readings))))


@KEY
class FetchedLinkTests(PlannerCase):
    """A page the server fetched from a pasted link is read by Claude too, as sent and pasted pages are."""

    def no_map_search(self):
        return mock.patch("journeys.link_details.geocode.search", return_value=[])

    def test_claude_reads_a_fetched_activity_page(self):
        patch, client = claude(answer(MUSEUM))
        with patch, mock.patch("journeys.activity_import.fetch_page", return_value=MUSEUM_PAGE), self.no_map_search():
            fields = link_details.read_activity(self.trip, LINK, day=self.day)["fields"]
        self.assertEqual((fields["read_by"], fields["phone"], fields["opening_hours"]), ("claude", "13 11 02", "Daily 09:00–17:00"))
        self.assertEqual((fields["amount"], fields["currency"]), ("15", "AUD"))
        sent = client.beta.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Melbourne Museum Tickets - Prices & Opening Hours | Trip.com", sent)  # the title, from the page's head
        self.assertIn("Natural and cultural history museum", sent)  # and its description
        self.assertIn("Daily 09:00-17:00", sent)
        self.assertIn(LINK, sent)  # where it came from

    def test_claude_reads_a_fetched_hotel_page(self):
        page = "<html><head><title>Hotel Example Melbourne | Trip.com</title></head><body>Hotel Example Melbourne<br>Check-in from 2:00 PM</body></html>"
        reply = {"name": "Hotel Example Melbourne", "address": "1 Example St, Melbourne VIC 3000", "city": "Melbourne",
                 "phone": "+61 3 9000 0000", "email": "", "price": "", "currency": "", "check_in_time": "14:00", "check_out_time": "11:00"}
        patch, _client = claude(answer(reply))
        with patch, mock.patch("journeys.stay_import.fetch_page", return_value=page):
            fields = link_details.read_stay("https://au.trip.com/hotels/melbourne-hotel-detail-1/")["fields"]
        self.assertEqual((fields["read_by"], fields["check_in_time"], fields["phone"]), ("claude", "14:00", "+61 3 9000 0000"))

    def test_claude_reads_a_fetched_transfer_page(self):
        page = "<html><head><title>Private transfer from Guilin Airport to Yangshuo | Trip.com</title></head><body>From A$ 120</body></html>"
        reply = {"price": "120", "currency": "AUD", "operator": "Guilin Car Service", "origin": "Guilin Airport",
                 "destination": "Yangshuo", "seat_class": "5-seat car"}
        patch, _client = claude(answer(reply))
        with patch, mock.patch("journeys.leg_import.fetch_page", return_value=page), self.no_map_search():
            fields = link_details.read_leg(self.trip, "https://au.trip.com/things-to-do/detail/36394123/")["fields"]
        self.assertEqual((fields["read_by"], fields["operator"], fields["amount"]), ("claude", "Guilin Car Service", "120"))

    def test_a_robot_check_isnt_sent_to_claude(self):
        with mock.patch("journeys.claude_read._client", side_effect=AssertionError("asked Claude")), self.no_map_search():
            with mock.patch("journeys.activity_import.fetch_page", return_value=ROBOT_CHECK):
                data = link_details.read_activity(self.trip, LINK)
            with mock.patch("journeys.stay_import.fetch_page", return_value=ROBOT_CHECK), \
                    self.assertRaisesMessage(stay_import.BlockedError, "Use Send to Trips"):
                link_details.read_stay("https://au.trip.com/hotels/melbourne-hotel-detail-1/")
        self.assertEqual((data["fields"]["name"], data["fields"]["blocked"]), ("Melbourne Museum", "address"))  # from the link
        self.assertIn("use Send to Trips", data["note"])

    def test_claude_gets_what_the_fetch_left(self):
        patch, _client = claude(answer(MUSEUM))
        with patch as make_client, clock(100.0, 112.0), mock.patch("journeys.activity_import.fetch_page", return_value=MUSEUM_PAGE), \
                self.no_map_search():
            link_details.read_activity(self.trip, LINK)
        make_client.assert_called_once_with(13.0)  # its 25 seconds, less the 12 the fetch took

    def test_no_time_left_keeps_the_patterns(self):
        with clock(100.0, 122.0), mock.patch("journeys.claude_read._client", side_effect=AssertionError("asked Claude")), \
                mock.patch("journeys.activity_import.fetch_page", return_value=MUSEUM_PAGE), self.no_map_search(), \
                self.assertLogs("journeys.claude_read", "WARNING"):
            fields = link_details.read_activity(self.trip, LINK)["fields"]
        self.assertNotIn("read_by", fields)
        self.assertEqual(fields["address"], "11 Nicholson St, Carlton VIC 3053")  # the pattern readers' answer stands

    @override_settings(ANTHROPIC_API_KEY="")
    def test_without_a_key_nothing_is_sent(self):
        with mock.patch("journeys.claude_read._client", side_effect=AssertionError("asked Claude")), \
                mock.patch("journeys.activity_import.fetch_page", return_value=MUSEUM_PAGE), self.no_map_search():
            fields = link_details.read_activity(self.trip, LINK)["fields"]
        self.assertNotIn("read_by", fields)
        self.assertEqual(fields["address"], "11 Nicholson St, Carlton VIC 3053")
