"""Stays, transport and activities: Planned or Booked (ideas merged into Planned), one Cost that's an
estimate while planned and what was paid once booked, the budget built from them, Add confirmation /
View confirmation, how each was added, and what a booking link fills in (and leaves missing)."""

import importlib
import json
from datetime import date, time
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from journeys import activity_import, booking_costs, stay_import
from journeys.models import BookingImport, BudgetItem, ChecklistItem, Item, Leg, Stay, Trip

from .factories import sample_trip

CONFIRMATION = """Booking confirmed
Hotel Example Shenzhen
Booking No. 9988776655
Check-in: Sat 2 Jan 2027 15:00
Check-out: Sun 3 Jan 2027 12:00
Total paid: CNY 480.00
"""


class PlannerCase(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.trip, self.places = sample_trip(visibility=Trip.Visibility.PRIVATE)
        self.day = self.trip.days.get(date=date(2027, 1, 2))

    def stay_data(self, **extra):
        return {"label": "Shenzhen", "kind": "hotel", "city": self.places["shenzhen"].pk, "check_in_date": "2027-01-02",
                "check_out_date": "2027-01-03", "status": "planned", **extra}

    def add_stay(self, **extra):
        self.client.post(reverse("manage:stay_create", args=[self.trip.pk]), self.stay_data(**extra))
        return Stay.objects.get(label="Shenzhen")

    def planned_stay(self, **extra):
        return Stay.objects.create(trip=self.trip, label="Shenzhen", city=self.places["shenzhen"], status="planned",
                                   check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3), **extra)

    def page(self, name, *args):
        return self.client.get(reverse(f"manage:{name}", args=[self.trip.pk, *args])).content.decode()


class StatusTests(PlannerCase):
    def test_only_planned_and_booked(self):
        self.planned_stay()
        page = self.page("stays")
        self.assertIn(">Booked</span>", page)  # the factory's stay
        self.assertIn(">Planned</span>", page)
        self.assertNotIn("Idea", page)
        form = self.page("item_create")
        self.assertIn('<option value="planned" selected>', form)
        self.assertNotIn('value="idea"', form)

    def test_ideas_become_planned_and_go_on_the_checklist(self):
        stay = self.planned_stay()
        leg = Leg.objects.get(number="G388")
        Stay.objects.filter(pk=stay.pk).update(status="idea")
        Leg.objects.filter(pk=leg.pk).update(status="idea")
        importlib.import_module("journeys.migrations.0025_merge_planned").ideas_to_planned(django_apps, None)
        stay.refresh_from_db()
        leg.refresh_from_db()
        self.assertEqual((stay.status, leg.status), ("planned", "planned"))
        reviews = ChecklistItem.objects.filter(trip=self.trip, to_review=True)
        self.assertEqual({(r.stay_id, r.leg_id) for r in reviews}, {(stay.pk, None), (None, leg.pk)})
        page = self.page("checklist")
        self.assertIn("To review", page)
        self.assertIn(reverse("manage:stay_edit", args=[self.trip.pk, stay.pk]), page)


class CostTests(PlannerCase):
    def test_a_planned_cost_is_an_estimate(self):
        stay = self.add_stay(cost="480.50", cost_currency="CNY")
        self.assertEqual(stay.status, "planned")
        line = stay.budget_items.get()
        self.assertEqual((line.estimate, line.estimate_currency, line.actual, line.booked), (Decimal("480.50"), "CNY", None, False))
        totals = self.client.get(reverse("manage:budget", args=[self.trip.pk])).context["totals"]
        self.assertEqual(totals["estimate"] - totals["actual"], line.estimate_aud or Decimal(0))

    def test_a_booked_cost_is_what_it_cost(self):
        stay = self.add_stay(status="booked", cost="500", cost_currency="AUD")
        line = stay.budget_items.get()
        self.assertEqual((line.actual, line.booked, line.purchased), (Decimal("500"), True, True))

    def test_booking_a_planned_one_makes_its_estimate_the_cost(self):
        stay = self.add_stay(cost="300")
        edit = reverse("manage:stay_edit", args=[self.trip.pk, stay.pk])
        self.assertIn('value="300.00"', self.client.get(edit).content.decode())
        self.client.post(edit, self.stay_data(status="booked", cost="300", cost_currency="AUD"))
        stay.refresh_from_db()
        line = stay.budget_items.get()
        self.assertEqual((stay.status, line.actual), ("booked", Decimal("300")))

    def test_clearing_a_planned_cost_removes_an_empty_line(self):
        stay = self.add_stay(cost="300")
        self.client.post(reverse("manage:stay_edit", args=[self.trip.pk, stay.pk]), self.stay_data(cost=""))
        self.assertFalse(BudgetItem.objects.filter(stay=stay).exists())

    def test_transport_and_activities(self):
        self.client.post(reverse("manage:leg_create", args=[self.trip.pk]), {
            "mode": "train", "number": "G999", "origin": self.places["szn"].pk, "destination": self.places["xkl"].pk,
            "depart_date": "2027-01-02", "status": "planned", "cost": "120", "cost_currency": "HKD",
        })
        leg = Leg.objects.get(number="G999")
        self.assertEqual((leg.status, leg.budget_items.get().category, leg.budget_items.get().estimate), ("planned", "train", Decimal("120")))
        self.client.post(reverse("manage:item_create", args=[self.trip.pk]), {
            "day": self.day.pk, "title": "Window of the World", "block": "afternoon", "cost": "45",
        })
        item = Item.objects.get(title="Window of the World")
        self.assertEqual((item.status, item.budget_items.get().estimate), ("planned", Decimal("45")))
        self.assertIn("est. AUD 45", self.page("activities"))

    def test_one_cost_field(self):
        form = self.page("stay_create")
        self.assertIn('name="cost"', form)
        self.assertNotIn('name="booking_cost"', form)
        self.assertNotIn('name="estimated_cost"', form)


class BudgetPageTests(PlannerCase):
    def test_columns_and_estimate(self):
        stay = self.add_stay(cost="200", cost_currency="AUD")
        response = self.client.get(reverse("manage:budget", args=[self.trip.pk]))
        page = response.content.decode()
        for gone in ("<th>Budgeted</th>", "<th>Booked</th>", "<th>Purchased</th>", "<th>Funded</th>"):
            self.assertNotIn(gone, page)
        self.assertIn("Estimated (AUD)", page)
        self.assertIn("estimated for planned items", page)
        totals = response.context["totals"]
        self.assertEqual(totals["planned"], Decimal("200"))
        stay.status = "booked"
        stay.save()
        self.assertEqual(self.client.get(reverse("manage:budget", args=[self.trip.pk])).context["totals"]["planned"], Decimal("0"))

    def test_lines_in_date_order(self):
        later = Item.objects.create(day=self.trip.days.get(date=date(2027, 1, 3)), title="Later thing")
        sooner = Item.objects.create(day=self.trip.days.get(date=date(2027, 1, 1)), title="Sooner thing")
        booking_costs.set_estimate(later, Decimal("10"))
        booking_costs.set_estimate(sooner, Decimal("20"))
        groups = self.client.get(reverse("manage:budget", args=[self.trip.pk])).context["totals"]["categories"]
        group = next(g for g in groups if g["label"] == "Activities and tours")
        self.assertEqual([line.item.title for line in group["items"]], ["Sooner thing", "Later thing"])

    def test_budget_form_and_old_estimates(self):
        BudgetItem.objects.create(trip=self.trip, description="Old guess", estimate=Decimal("123.45"))
        self.assertNotIn("123.45", self.page("budget"))
        form = self.page("budget_create")
        for gone in ('name="estimate"', 'name="funded"', 'name="budgeted"'):
            self.assertNotIn(gone, form)


class ConfirmationButtonTests(PlannerCase):
    def test_add_for_planned_view_for_booked(self):
        planned = self.planned_stay()
        booked = self.trip.stays.get(label="Hong Kong 1")
        booked.confirmation_url = "https://au.trip.com/order/123"
        booked.save()
        page = self.page("stays")
        self.assertIn(reverse("manage:confirm_booking", args=[self.trip.pk, "stay", planned.pk]), page)
        self.assertNotIn(reverse("manage:confirm_booking", args=[self.trip.pk, "stay", booked.pk]), page)
        self.assertIn('href="https://au.trip.com/order/123" target="_blank" rel="noopener nofollow">View confirmation</a>', page)

    def test_no_view_without_an_imported_confirmation(self):
        leg = Leg.objects.get(number="CX156")
        leg.status, leg.url = "booked", "https://www.cathaypacific.com/booking"  # its own booking page isn't a confirmation
        leg.save()
        self.assertNotIn(">View confirmation</a>", self.page("legs"))

    def test_day_plan_has_the_buttons_too(self):
        item = Item.objects.create(day=self.day, title="Window of the World")
        self.assertIn(reverse("manage:confirm_booking", args=[self.trip.pk, "item", item.pk]), self.page("day", "2027-01-02"))

    def test_every_plan_page_can_import_a_booking(self):
        for name in ("stays", "legs", "activities"):
            with self.subTest(page=name):
                self.assertIn(f"{reverse('manage:imports')}?trip={self.trip.pk}", self.page(name))


class AddConfirmationTests(PlannerCase):
    def setUp(self):
        super().setUp()
        self.stay = self.planned_stay()
        self.url = reverse("manage:confirm_booking", args=[self.trip.pk, "stay", self.stay.pk])

    def test_pasted_confirmation_is_reviewed_then_applied(self):
        response = self.client.post(self.url, {"text": CONFIRMATION})
        record = BookingImport.objects.get()
        self.assertRedirects(response, f"{self.url}?import={record.pk}")
        review = self.client.get(f"{self.url}?import={record.pk}")
        self.assertContains(review, "9988776655")
        self.client.post(f"{self.url}?import={record.pk}", {"action": "apply", "fields": ["booking_ref", "check_in_time"], "budget": "1"})
        self.stay.refresh_from_db()
        self.assertEqual((self.stay.booking_ref, self.stay.status, self.stay.check_in_time), ("9988776655", "booked", time(15, 0)))
        line = self.stay.budget_items.get()
        self.assertEqual((line.actual, line.actual_currency), (Decimal("480.00"), "CNY"))
        record.refresh_from_db()
        self.assertEqual((record.status, record.stay), ("applied", self.stay))

    def test_link_is_read_and_kept_for_view_confirmation(self):
        html = "<html><body>" + CONFIRMATION.replace("\n", "<br>") + "</body></html>"
        with mock.patch("journeys.manage_views.stay_import.fetch_page", return_value=html):
            self.client.post(self.url, {"link": "https://au.trip.com/order/123"})
        record = BookingImport.objects.get()
        self.assertEqual((record.source, record.link), ("link", "https://au.trip.com/order/123"))
        self.stay.refresh_from_db()
        self.assertEqual(self.stay.confirmation_link(), "")  # not until it's added
        self.client.post(f"{self.url}?import={record.pk}", {"action": "apply", "fields": ["booking_ref"]})
        self.stay.refresh_from_db()
        self.assertEqual((self.stay.confirmation_url, self.stay.confirmation_link()), ("https://au.trip.com/order/123",) * 2)

    def test_unreadable_link_says_to_paste_instead(self):
        with mock.patch("journeys.manage_views.stay_import.fetch_page", side_effect=stay_import.PageError("The site answered with an error (403).")):
            response = self.client.post(self.url, {"link": "https://secure.booking.com/confirmation"})
        self.assertContains(response, "paste the confirmation or upload it instead")
        self.assertFalse(BookingImport.objects.exists())

    def test_activity_and_transport(self):
        item = Item.objects.create(day=self.day, title="Window of the World")
        url = reverse("manage:confirm_booking", args=[self.trip.pk, "item", item.pk])
        self.client.post(url, {"text": "Klook booking confirmed\nBooking reference ID: KL12345678\nParticipation date: 2 Jan 2027\nTotal paid: AU$ 45.00\n",
                               "link": "https://www.klook.com/order/KL12345678"})
        self.client.post(f"{url}?import={BookingImport.objects.get().pk}", {"action": "apply", "fields": ["booking_ref"], "budget": "1"})
        item.refresh_from_db()
        self.assertEqual((item.status, item.confirmation_url, item.budget_items.get().actual),
                         ("booked", "https://www.klook.com/order/KL12345678", Decimal("45.00")))

        leg = Leg.objects.get(number="G388")
        url = reverse("manage:confirm_booking", args=[self.trip.pk, "leg", leg.pk])
        self.client.post(url, {"text": "Trip.com train booking\nBooking No. 1122334455\nG388\nTotal paid: HK$ 86.00\n"})
        self.client.post(f"{url}?import={BookingImport.objects.latest('pk').pk}", {"action": "apply", "fields": ["booking_ref"], "budget": "1"})
        leg.refresh_from_db()
        self.assertEqual((leg.booking_ref, leg.status, leg.budget_items.get().category), ("1122334455", "booked", "train"))

    def test_other_trips_plans_are_out_of_reach(self):
        other = Trip.objects.create(title="Other", slug="other", start_date=date(2027, 5, 1), end_date=date(2027, 5, 3))
        theirs = Stay.objects.create(trip=other, label="Elsewhere", check_in_date=date(2027, 5, 1), check_out_date=date(2027, 5, 2))
        self.assertEqual(self.client.get(reverse("manage:confirm_booking", args=[self.trip.pk, "stay", theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("manage:confirm_booking", args=[self.trip.pk, "nope", self.stay.pk])).status_code, 404)


class AddedFromTests(PlannerCase):
    def test_from_a_booking_link(self):
        stay = self.add_stay(filled_from="https://au.trip.com/hotels/shenzhen-hotel-detail-123/")
        self.assertEqual((stay.added_from, stay.added_from_url), ("link", "https://au.trip.com/hotels/shenzhen-hotel-detail-123/"))
        page = self.page("stay_edit", stay.pk)
        self.assertIn("Added from a booking link", page)
        self.assertIn("https://au.trip.com/hotels/shenzhen-hotel-detail-123/", page)

    def test_by_hand_and_unknown(self):
        stay = self.add_stay()
        self.assertEqual(stay.added_from, "hand")
        self.assertIn("Added by hand", self.page("stay_edit", stay.pk))
        older = self.trip.stays.get(label="Hong Kong 1")
        self.assertIn("wasn't recorded", self.page("stay_edit", older.pk))

    def test_missing_details_are_flagged_until_added(self):
        stay = self.add_stay(filled_from="https://au.trip.com/hotels/x/")
        self.assertEqual(booking_costs.missing_details(stay), ["cost", "address", "phone or email", "check-in and check-out times"])
        self.assertIn("Missing 4 details", self.page("stays"))
        self.assertIn("Some details are missing: cost, address", self.page("stay_edit", stay.pk))
        self.client.post(reverse("manage:stay_edit", args=[self.trip.pk, stay.pk]), self.stay_data(
            cost="300", address="1 Example Road", phone="+86 755 0000 0000", check_in_time="15:00", check_out_time="12:00"))
        stay.refresh_from_db()
        self.assertEqual(booking_costs.missing_details(stay), [])
        stay.status = "booked"
        self.assertEqual(booking_costs.missing_details(stay), [])


class LinkReaderTests(PlannerCase):
    ATTRACTION = """<html><head><title>Window of the World | Trip.com</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "TouristAttraction", "name": "Window of the World",
 "address": {"@type": "PostalAddress", "streetAddress": "9037 Shennan Blvd", "addressLocality": "Shenzhen"},
 "telephone": "+86 755 2660 8000",
 "openingHoursSpecification": [{"dayOfWeek": ["https://schema.org/Monday", "https://schema.org/Sunday"], "opens": "09:00:00", "closes": "21:30:00"}],
 "offers": {"@type": "Offer", "price": "220", "priceCurrency": "CNY"}}</script></head><body></body></html>"""

    def test_activity_page_gives_price_hours_and_contact(self):
        found = activity_import.extract(self.ATTRACTION, "https://au.trip.com/travel-guide/attraction/shenzhen/window-of-the-world-123/")
        self.assertEqual((found["amount"], found["currency"]), ("220", "CNY"))
        self.assertEqual(found["opening_hours"], "Mon, Sun 09:00–21:30")
        self.assertEqual(found["phone"], "+86 755 2660 8000")

    def test_hotel_page_gives_a_rough_price(self):
        page = """<script type="application/ld+json">{"@type": "Hotel", "name": "Hotel Example", "telephone": "+86 755 1111 2222",
        "address": "1 Example Road, Shenzhen", "priceRange": "CNY 480 - CNY 900"}</script>"""
        found = stay_import.extract(page)
        self.assertEqual((found["amount"], found["currency"]), ("480", "CNY"))

    def test_the_form_hears_what_was_missing(self):
        with mock.patch("journeys.manage_views.activity_import.read_link",
                        return_value={"name": "Somewhere", "url": "https://www.klook.com/activity/1-x/", "site": "klook.com"}):
            response = self.client.post(reverse("manage:api_activity_extract", args=[self.trip.pk]),
                                        json.dumps({"url": "https://www.klook.com/activity/1-x/"}), content_type="application/json")
        self.assertEqual(response.json()["missing"], ["cost", "address", "phone or email", "opening hours"])

    def test_pasted_confirmation_fills_the_cost(self):
        text = "Booking confirmed\nHotel Example Shenzhen\nCheck-in 2 Jan 2027\nTotal paid: CNY 1,280.00\n"
        response = self.client.post(reverse("manage:api_stay_extract", args=[self.trip.pk]), {"text": text}, content_type="application/json")
        fields = response.json()["fields"]
        self.assertEqual((fields["amount"], fields["currency"]), ("1280.00", "CNY"))

    def test_transfer_page_price(self):
        found = {"mode": "car", "origin": "", "destination": "", "price": "US$ 25.50", "url": "https://example.com/x"}
        with mock.patch("journeys.manage_views.leg_import.read_link", return_value=found):
            response = self.client.post(reverse("manage:api_leg_extract", args=[self.trip.pk]), {"url": "https://example.com/x"},
                                        content_type="application/json")
        self.assertEqual((response.json()["fields"]["amount"], response.json()["fields"]["currency"]), ("25.50", "USD"))


class ParseMoneyTests(TestCase):
    def test_amounts(self):
        cases = {
            "US$ 25.50": (Decimal("25.50"), "USD"), "CNY 1,280": (Decimal("1280"), "CNY"), "¥480": (Decimal("480"), "CNY"),
            "HK$1,200.50": (Decimal("1200.50"), "HKD"), "A$ 99": (Decimal("99"), "AUD"), "1,500 TWD": (Decimal("1500"), "TWD"),
            "$30": None, "free": None, "": None, "CNY 0": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(booking_costs.parse_money(text), expected)
