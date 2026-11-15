"""View confirmation only for imported confirmations (a link or an uploaded file), reading a planned
plan's link when it's saved, Refresh details, and setting who paid for several budget lines at once."""

import shutil
import tempfile
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from journeys import link_details, stay_import
from journeys.models import BookingImport, BudgetItem, Document, Item, Leg, Stay, Trip

from .factories import sample_trip

CONFIRMATION = """Booking confirmed
Hotel Example Shenzhen
Booking No. 9988776655
Check-in: Sat 2 Jan 2027 15:00
Check-out: Sun 3 Jan 2027 12:00
Total paid: CNY 480.00
"""
HOTEL_PAGE = {"fields": {"name": "Hotel Example", "address": "1 Example Road, Shenzhen", "phone": "+86 755 1111 2222",
                         "check_in_time": "14:00", "check_out_time": "12:00", "amount": "480", "currency": "CNY"},
              "city": None, "location": None, "missing": []}


class PlannerCase(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.trip, self.places = sample_trip(visibility=Trip.Visibility.PRIVATE)
        self.day = self.trip.days.get(date=date(2027, 1, 2))

    def planned_stay(self, **extra):
        return Stay.objects.create(trip=self.trip, label="Shenzhen", city=self.places["shenzhen"], status="planned",
                                   check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3), **extra)

    def stay_data(self, **extra):
        return {"label": "Shenzhen", "kind": "hotel", "city": self.places["shenzhen"].pk, "check_in_date": "2027-01-02",
                "check_out_date": "2027-01-03", "status": "planned", **extra}


class ViewConfirmationTests(PlannerCase):
    def confirm(self, stay, data):
        url = reverse("manage:confirm_booking", args=[self.trip.pk, "stay", stay.pk])
        self.client.post(url, data)
        record = BookingImport.objects.latest("pk")
        self.client.post(f"{url}?import={record.pk}", {"action": "apply", "fields": ["booking_ref"], "budget": "1"})
        stay.refresh_from_db()
        return record

    def test_an_uploaded_confirmation_is_kept_and_opened(self):
        stay = self.planned_stay()
        self.confirm(stay, {"file": SimpleUploadedFile("confirmation.txt", CONFIRMATION.encode(), content_type="text/plain")})
        self.assertEqual(stay.status, "booked")
        doc = stay.confirmation_document
        self.assertEqual((doc.stay, doc.trip), (stay, self.trip))
        link = reverse("manage:document_download", args=[self.trip.pk, doc.pk])
        self.assertEqual(stay.confirmation_link(), link)
        self.assertIn(f'href="{link}"', self.client.get(reverse("manage:stays", args=[self.trip.pk])).content.decode())
        self.assertEqual(b"".join(self.client.get(link).streaming_content), CONFIRMATION.encode())

    def test_a_pasted_confirmation_gives_no_view_button(self):
        stay = self.planned_stay()
        self.confirm(stay, {"text": CONFIRMATION})
        self.assertEqual((stay.status, stay.confirmation_link()), ("booked", ""))
        page = self.client.get(reverse("manage:stays", args=[self.trip.pk])).content.decode()
        self.assertNotIn(">View confirmation</a>", page)

    def test_import_a_booking_upload_counts_too(self):
        stay = self.trip.stays.get(label="Hong Kong 1")
        upload = SimpleUploadedFile("booking.txt", CONFIRMATION.replace("Shenzhen", "Hong Kong").encode(), content_type="text/plain")
        self.client.post(reverse("manage:imports"), {"trip": self.trip.pk, "file": upload})
        record = BookingImport.objects.get()
        self.assertTrue(record.file)
        self.client.post(reverse("manage:import_review", args=[record.pk]), {"action": "apply", "trip": self.trip.pk, "stay": stay.pk, "fields": ["booking_ref"]})
        stay.refresh_from_db()
        self.assertIsNotNone(stay.confirmation_document)
        self.assertTrue(Document.objects.filter(stay=stay).exists())


class ReadOnSaveTests(PlannerCase):
    def test_a_new_planned_stay_reads_its_link(self):
        with mock.patch("journeys.link_details.read_stay", return_value=HOTEL_PAGE) as read:
            response = self.client.post(reverse("manage:stay_create", args=[self.trip.pk]),
                                        self.stay_data(url="https://au.trip.com/hotels/x-hotel-detail-1/"), follow=True)
        read.assert_called_once_with("https://au.trip.com/hotels/x-hotel-detail-1/")
        stay = Stay.objects.get(label="Shenzhen")
        self.assertEqual((stay.address, stay.phone, stay.check_in_time.hour), ("1 Example Road, Shenzhen", "+86 755 1111 2222", 14))
        self.assertEqual(stay.name, "Hotel Example")
        line = stay.budget_items.get()
        self.assertEqual((line.estimate, line.estimate_currency, line.actual), (Decimal("480"), "CNY", None))
        self.assertContains(response, "Filled in from its link")

    def test_what_you_typed_is_kept(self):
        with mock.patch("journeys.link_details.read_stay", return_value=HOTEL_PAGE):
            self.client.post(reverse("manage:stay_create", args=[self.trip.pk]),
                             self.stay_data(url="https://au.trip.com/hotels/x/", phone="+61 7 0000 0000", cost="300"))
        stay = Stay.objects.get(label="Shenzhen")
        self.assertEqual(stay.phone, "+61 7 0000 0000")
        self.assertEqual(stay.budget_items.get().estimate, Decimal("300"))

    def test_only_when_new_or_the_link_changed_and_planned(self):
        stay = self.planned_stay(url="https://au.trip.com/hotels/x/")
        edit = reverse("manage:stay_edit", args=[self.trip.pk, stay.pk])
        with mock.patch("journeys.link_details.read_stay", return_value=HOTEL_PAGE) as read:
            self.client.post(edit, self.stay_data(url="https://au.trip.com/hotels/x/", notes="unchanged link"))
            self.assertFalse(read.called)
            self.client.post(edit, self.stay_data(url="https://au.trip.com/hotels/y/"))
            self.assertEqual(read.call_count, 1)
            self.client.post(reverse("manage:stay_create", args=[self.trip.pk]),
                             self.stay_data(label="Booked one", status="booked", url="https://au.trip.com/hotels/z/"))
            self.assertEqual(read.call_count, 1)  # booked: its confirmation gives the details instead

    def test_an_unreadable_link_is_noted(self):
        with mock.patch("journeys.link_details.read_stay", side_effect=stay_import.PageError("The site showed a robot check.")):
            response = self.client.post(reverse("manage:stay_create", args=[self.trip.pk]),
                                        self.stay_data(url="https://au.trip.com/hotels/x/"), follow=True)
        self.assertContains(response, "read its link: The site showed a robot check.")
        stay = Stay.objects.get(label="Shenzhen")
        self.assertEqual(stay.details_note, "The site showed a robot check.")
        self.assertContains(self.client.get(reverse("manage:stay_edit", args=[self.trip.pk, stay.pk])), "The site showed a robot check.")

    def test_an_activity_reads_its_first_link(self):
        page = {"fields": {"opening_hours": "Daily 09:00–21:30", "phone": "+86 755 2660 8000", "amount": "220", "currency": "CNY"},
                "place": None, "missing": []}
        with mock.patch("journeys.link_details.read_activity", return_value=page) as read:
            self.client.post(reverse("manage:item_create", args=[self.trip.pk]), {
                "day": self.day.pk, "title": "Window of the World", "block": "afternoon",
                "links": "Trip.com | https://au.trip.com/travel-guide/attraction/x/\nhttps://example.com/other"})
        self.assertEqual(read.call_args.args[1], "https://au.trip.com/travel-guide/attraction/x/")
        item = Item.objects.get(title="Window of the World")
        self.assertEqual((item.opening_hours, item.budget_items.get().estimate), ("Daily 09:00–21:30", Decimal("220")))


class RefreshDetailsTests(PlannerCase):
    def test_button_on_planned_plans_with_a_link(self):
        stay = self.planned_stay(url="https://au.trip.com/hotels/x/")
        refresh = reverse("manage:refresh_details", args=[self.trip.pk, "stay", stay.pk])
        self.assertContains(self.client.get(reverse("manage:stay_edit", args=[self.trip.pk, stay.pk])), refresh)
        no_link = self.trip.stays.get(label="Hong Kong 1")
        self.assertNotContains(self.client.get(reverse("manage:stay_edit", args=[self.trip.pk, no_link.pk])), ">Refresh details<")

    def test_refresh_fills_the_gaps(self):
        stay = self.planned_stay(url="https://au.trip.com/hotels/x/", phone="+61 7 0000 0000")
        with mock.patch("journeys.link_details.read_stay", return_value=HOTEL_PAGE):
            response = self.client.post(reverse("manage:refresh_details", args=[self.trip.pk, "stay", stay.pk]), follow=True)
        stay.refresh_from_db()
        self.assertEqual((stay.address, stay.phone), ("1 Example Road, Shenzhen", "+61 7 0000 0000"))
        self.assertIsNotNone(stay.details_checked_at)
        self.assertRedirects(response, reverse("manage:stay_edit", args=[self.trip.pk, stay.pk]))
        self.assertContains(response, "Filled in from its link: property name, address")

    def test_transport_refresh(self):
        leg = Leg.objects.get(number="G388")
        leg.url = "https://www.12go.asia/en/travel/x"
        leg.save()
        page = {"fields": {"operator": "MTR", "amount": "86", "currency": "HKD"}, "origin": None, "destination": None, "missing": []}
        with mock.patch("journeys.link_details.read_leg", return_value=page):
            self.client.post(reverse("manage:refresh_details", args=[self.trip.pk, "leg", leg.pk]))
        leg.refresh_from_db()
        self.assertEqual((leg.operator, leg.budget_items.get().estimate), ("MTR", Decimal("86")))

    def test_other_trips_are_out_of_reach(self):
        self.assertEqual(self.client.post(reverse("manage:refresh_details", args=[self.trip.pk, "stay", 99999])).status_code, 404)
        self.assertEqual(self.client.post(reverse("manage:refresh_details", args=[self.trip.pk, "nope", 1])).status_code, 404)


class BudgetPaidByTests(PlannerCase):
    def test_set_and_clear_who_paid(self):
        jack, aaron = self.trip.travellers.get(initials="JY"), self.trip.travellers.get(initials="AB")
        a = BudgetItem.objects.create(trip=self.trip, description="Taxi", actual=Decimal("20"))
        b = BudgetItem.objects.create(trip=self.trip, description="Lunch", actual=Decimal("30"), paid_by=jack)
        untouched = BudgetItem.objects.create(trip=self.trip, description="Dinner", actual=Decimal("40"), paid_by=jack)
        page = self.client.get(reverse("manage:budget", args=[self.trip.pk])).content.decode()
        self.assertIn('name="lines"', page)
        self.assertIn("Set paid by", page)
        url = reverse("manage:budget_bulk", args=[self.trip.pk])
        self.client.post(url, {"lines": [a.pk, b.pk], "paid_by": aaron.pk, "action": "paid_by"})
        self.assertEqual({line.paid_by for line in BudgetItem.objects.filter(pk__in=[a.pk, b.pk])}, {aaron})
        self.assertEqual(BudgetItem.objects.get(pk=untouched.pk).paid_by, jack)
        self.client.post(url, {"lines": [a.pk], "paid_by": "none", "action": "paid_by"})
        self.assertIsNone(BudgetItem.objects.get(pk=a.pk).paid_by)

    def test_only_this_trips_lines_and_travellers(self):
        other = Trip.objects.create(title="Other", slug="other", start_date=date(2027, 5, 1), end_date=date(2027, 5, 3))
        theirs = BudgetItem.objects.create(trip=other, description="Theirs", actual=Decimal("10"))
        stranger = other.travellers.create(name="Someone", initials="SO")
        mine = BudgetItem.objects.create(trip=self.trip, description="Mine", actual=Decimal("10"))
        url = reverse("manage:budget_bulk", args=[self.trip.pk])
        self.client.post(url, {"lines": [theirs.pk, mine.pk], "paid_by": stranger.pk, "action": "paid_by"})
        self.assertIsNone(BudgetItem.objects.get(pk=mine.pk).paid_by)
        self.client.post(url, {"lines": [theirs.pk], "paid_by": self.trip.travellers.first().pk, "action": "paid_by"})
        self.assertIsNone(BudgetItem.objects.get(pk=theirs.pk).paid_by)
