import hashlib
import hmac
import time
from datetime import date, time as dtime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from journeys import booking_import
from journeys.models import BookingImport, BudgetItem, Place, Stay, Trip

# Laid out like a Trip.com booking page copied from the browser (invented numbers).
CONFIRMATION = """Confirmed
Booking No. 9999000011112222 PIN 4321(do not disclose)
This booking cannot be modified, and no refund will be given if you cancel it. You'll be charged the cancellation fee if you don't check in.
Harbour View Hotel ★★★★★
港景酒店
1 Pier Road, Cotai, Macau, China Transfer service
+853-12345678
harbour.view@htlpartner.example.com
Check-in
Tue, Jan 26, 2027
After 15:00
2 nights
Check-out
Thu, Jan 28, 2027
Before 11:00
Deluxe King Room · 1 Room
Special requests Non-smoking room. High floor.
Price Details
1 room × 2 nights AU$431.38
Diamond Tier Deal - AU$72.30
Prepay Online Paid AU$387.14"""


class ParseTests(SimpleTestCase):
    def test_trip_com_page(self):
        f = booking_import.parse(CONFIRMATION)
        self.assertEqual((f["booking_ref"], f["name"], f["name_local"]), ("9999000011112222", "Harbour View Hotel", "港景酒店"))
        self.assertEqual(f["address"], "1 Pier Road, Cotai, Macau, China")
        self.assertEqual((f["phone"], f["email"]), ("+853-12345678", "harbour.view@htlpartner.example.com"))
        self.assertEqual((f["check_in_date"], f["check_in_time"]), (date(2027, 1, 26), "15:00"))
        self.assertEqual((f["check_out_date"], f["check_out_time"], f["nights"]), (date(2027, 1, 28), "11:00", 2))
        self.assertEqual((f["amount"], f["currency"]), ("387.14", "AUD"))  # what was paid, not a discount line
        self.assertEqual((f["room"], f["special_requests"]), ("Deluxe King Room · 1 Room", "Non-smoking room. High floor."))

    def test_pin_is_removed(self):
        self.assertNotIn("4321", booking_import.redact(CONFIRMATION))

    def test_side_by_side_dates_and_labels(self):
        text = "Hotel: Garden Inn\nAddress: 5 Rose St, Kowloon, Hong Kong\nCheck-in Check-out\n26 Jan 2027 28 Jan 2027\nConfirmation number: ABC12345\nTotal charged HK$1,234.50"
        f = booking_import.parse(text)
        self.assertEqual((f["name"], f["address"], f["booking_ref"]), ("Garden Inn", "5 Rose St, Kowloon, Hong Kong", "ABC12345"))
        self.assertEqual((f["check_in_date"], f["check_out_date"]), (date(2027, 1, 26), date(2027, 1, 28)))
        self.assertEqual((f["amount"], f["currency"]), ("1234.50", "HKD"))

    def test_email_file(self):
        raw = (b"From: Me <me@example.com>\r\nSubject: Fwd: Booking confirmed\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               b"<p>Booking No. 1234567890</p><p>Check-in</p><p>Tue, Jan 26, 2027</p><p>Check-out</p><p>Thu, Jan 28, 2027</p>")
        subject, sender, text = booking_import.text_from_upload(SimpleUploadedFile("conf.eml", raw))
        self.assertEqual(subject, "Fwd: Booking confirmed")
        self.assertIn("Booking No. 1234567890", text)
        self.assertEqual(booking_import.parse(text)["check_out_date"], date(2027, 1, 28))


class ImportFlowTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "a@example.com", None))
        self.trip = Trip.objects.create(title="China 2027", slug="china-2027", start_date=date(2026, 12, 27), end_date=date(2027, 1, 31))
        macau = Place.objects.create(kind=Place.Kind.CITY, name="Macau", name_local="澳門", lat=22.19, lon=113.54, country="MO", tz="Asia/Macau")
        self.stay = Stay.objects.create(trip=self.trip, label="Macau", city=macau, check_in_date=date(2027, 1, 26), check_out_date=date(2027, 1, 28))
        self.budget = BudgetItem.objects.create(trip=self.trip, stay=self.stay, category="accommodation", description="Hotel: Macau", estimate=Decimal("250"))

    def test_paste_review_and_apply(self):
        response = self.client.post(reverse("manage:imports"), {"text": CONFIRMATION})
        record = BookingImport.objects.get()
        self.assertRedirects(response, reverse("manage:import_review", args=[record.pk]))
        self.assertEqual(record.trip, self.trip)  # worked out from the dates
        self.assertNotIn("4321", record.text)
        review = self.client.get(reverse("manage:import_review", args=[record.pk]))
        self.assertContains(review, "matches the dates")
        self.assertContains(review, "AUD 387.14")
        self.client.post(reverse("manage:import_review", args=[record.pk]), {
            "action": "apply", "trip": self.trip.pk, "stay": self.stay.pk, "budget": "1",
            "fields": ["booking_ref", "name", "address", "phone", "email", "check_in_time", "check_out_time", "room"],
        })
        self.stay.refresh_from_db()
        self.assertEqual((self.stay.name, self.stay.booking_ref, self.stay.check_in_time), ("Harbour View Hotel", "9999000011112222", dtime(15, 0)))
        self.assertEqual(self.stay.status, "booked")
        self.assertIn("Room: Deluxe King Room", self.stay.notes)
        self.budget.refresh_from_db()
        self.assertEqual((self.budget.actual, self.budget.actual_currency, self.budget.purchased), (Decimal("387.14"), "AUD", True))
        record.refresh_from_db()
        self.assertEqual((record.status, record.stay), (BookingImport.Status.APPLIED, self.stay))

    def test_dismiss(self):
        record = BookingImport.objects.create(trip=self.trip, source="paste", text=CONFIRMATION)
        self.client.post(reverse("manage:import_review", args=[record.pk]), {"action": "dismiss"})
        record.refresh_from_db()
        self.assertEqual(record.status, BookingImport.Status.DISMISSED)


@override_settings(INBOUND_EMAIL_TOKEN="secret-token", MAILGUN_WEBHOOK_SIGNING_KEY="key-123", INBOUND_ALLOWED_SENDERS=["me@example.com"])
class InboundEmailTests(TestCase):
    def setUp(self):
        Trip.objects.create(title="China 2027", slug="china-2027", start_date=date(2026, 12, 27), end_date=date(2027, 1, 31))
        self.url = reverse("inbound_email", args=["secret-token"])

    def mailgun(self, **extra):
        timestamp, token = str(int(time.time())), "abc123"
        signature = hmac.new(b"key-123", (timestamp + token).encode(), hashlib.sha256).hexdigest()
        data = {"timestamp": timestamp, "token": token, "signature": signature, "from": "Me <me@example.com>",
                "subject": "Fwd: Booking confirmed", "body-plain": CONFIRMATION}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_wrong_address_is_not_found(self):
        self.assertEqual(self.client.post(reverse("inbound_email", args=["nope"]), {}).status_code, 404)

    def test_mailgun_delivery_lands_in_the_inbox(self):
        self.assertEqual(self.mailgun().status_code, 200)
        record = BookingImport.objects.get()
        self.assertEqual((record.source, record.trip.slug), (BookingImport.Source.EMAIL, "china-2027"))
        self.assertNotIn("4321", record.text)

    def test_bad_signature_and_unknown_sender(self):
        self.assertEqual(self.mailgun(signature="0" * 64).status_code, 403)
        self.assertEqual(self.mailgun(**{"from": "stranger@example.net"}).content, b"ignored")
        self.assertFalse(BookingImport.objects.exists())

    def test_raw_email(self):
        raw = b"From: me@example.com\r\nSubject: Booking\r\nContent-Type: text/plain\r\n\r\n" + CONFIRMATION.encode()
        self.client.post(self.url, raw, content_type="message/rfc822")
        self.assertEqual(BookingImport.objects.get().subject, "Booking")
