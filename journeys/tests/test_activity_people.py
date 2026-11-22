"""Activities: Cost per person × People (starting at the trip's travellers) = the Total on the budget;
and Trip.com links, whose pages Trip.com won't show the server, still finding the place."""

import importlib
import json
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.urls import reverse

from journeys import activity_import, booking_costs, link_details, page_details, site_links, stay_import
from journeys.models import BudgetItem, Item

from .test_plans_costs import PlannerCase

BLOCKED = stay_import.BlockedError("That site blocks automatic reading. Paste the page text below instead.")
PALACE = {"name": "The Palace Museum", "lat": 39.9163, "lon": 116.3972, "kind": "attraction", "country": "CN", "city": "Beijing",
          "label": "The Palace Museum, 4 Jingshan Front Street, Dongcheng, Beijing", "source_id": "way/9", "name_local": "故宫博物院"}


class PerPersonCostTests(PlannerCase):
    def add(self, **extra):
        self.client.post(reverse("manage:item_create", args=[self.trip.pk]),
                         {"day": self.day.pk, "title": "Window of the World", "block": "afternoon", **extra})
        return Item.objects.get(title="Window of the World")

    def test_the_form_starts_at_the_trips_travellers(self):
        form = self.page("item_create")
        self.assertIn("Cost per person", form)
        self.assertIn('name="people" value="2"', form)
        self.assertIn("data-cost-total", form)
        self.assertIn("Starts at the trip&#x27;s 2 travellers", form)

    def test_cost_times_people_goes_on_the_budget(self):
        item = self.add(cost="45", cost_currency="CNY", people="3")
        line = item.budget_items.get()
        self.assertEqual((item.people, line.estimate, line.estimate_currency), (3, Decimal("135"), "CNY"))
        edit = self.page("item_edit", item.pk)
        self.assertIn('name="cost" value="45.00"', edit)
        self.assertIn("CNY 135.00", edit)
        self.assertIn("(CNY 45.00 × 3)", self.page("activities"))

    def test_changing_people_keeps_the_cost_per_person(self):
        item = self.add(cost="45", people="3")
        self.client.post(reverse("manage:item_edit", args=[self.trip.pk, item.pk]),
                         {"day": self.day.pk, "title": item.title, "block": "afternoon", "cost": "45.00", "cost_currency": "AUD", "people": "4"})
        self.assertEqual(item.budget_items.get().estimate, Decimal("180"))

    def test_a_booked_one_counts_what_it_cost_in_total(self):
        item = self.add(cost="60", people="2", status="booked")
        line = item.budget_items.get()
        self.assertEqual((line.actual, line.booked), (Decimal("120"), True))
        totals = self.client.get(reverse("manage:budget", args=[self.trip.pk])).context["totals"]
        self.assertEqual(totals["actual"], Decimal("900") + Decimal("120"))  # the sample trip's flights, plus this

    def test_set_people_for_several(self):
        one = self.add(cost="45", people="1")
        two = Item.objects.get(title="Harbour walk")
        booking_costs.record(two, Decimal("100"), "AUD")  # costed before People: one person, as the migration leaves it
        two.people = 1
        two.save()
        self.client.post(reverse("manage:activities_bulk", args=[self.trip.pk]), {"items": [one.pk, two.pk], "action": "people", "people": "2"})
        one.refresh_from_db(), two.refresh_from_db()
        self.assertEqual((one.people, one.budget_items.get().estimate), (2, Decimal("90")))
        self.assertEqual((two.people, two.budget_items.get().actual), (2, Decimal("200")))
        self.assertIn("(AUD 100.00 × 2)", self.page("activities"))

    def test_existing_costs_keep_their_totals(self):
        costed, uncosted = Item.objects.get(title="Harbour walk"), Item.objects.get(title="Hidden plan")
        BudgetItem.objects.create(trip=self.trip, description="Walk", estimate=Decimal("50"), item=costed)
        Item.objects.update(people=None)
        importlib.import_module("journeys.migrations.0027_item_people").keep_existing_totals(django_apps, None)
        costed.refresh_from_db(), uncosted.refresh_from_db()
        self.assertEqual((costed.people, uncosted.people), (1, None))
        self.assertEqual(costed.budget_items.get().estimate, Decimal("50"))

    def test_a_confirmation_says_how_many(self):
        from journeys import booking_import

        item = Item.objects.get(title="Harbour walk")
        booking_import.apply_to_activity(item, {"travellers": "2 adults, 1 child"}, ["travellers"])
        item.refresh_from_db()
        self.assertEqual((item.people, item.status), (3, "booked"))


class TripComActivityTests(PlannerCase):
    def post(self, **body):
        return self.client.post(reverse("manage:api_activity_extract", args=[self.trip.pk]), json.dumps(body), content_type="application/json")

    def test_names_and_cities_in_tripcom_links(self):
        self.assertEqual(site_links.tripcom_activity("https://au.trip.com/travel-guide/attraction/beijing/the-palace-museum-75595/")
                         | {}, {"site": "trip.com", "kind": "activity", "name": "The Palace Museum", "city": "Beijing",
                                "url": "https://au.trip.com/travel-guide/attraction/beijing/the-palace-museum-75595/"})
        self.assertEqual(site_links.tripcom_activity("https://www.trip.com/travel-guide/attraction/zhangjiajie-27/tianmen-mountain-80463/")["city"],
                         "Zhangjiajie")
        self.assertNotIn("name", site_links.tripcom_activity("https://www.trip.com/things-to-do/detail/36394123/?locale=en-AU"))
        self.assertEqual(site_links.tripcom_activity("https://au.trip.com/travel-guide/attraction/chongqing/chongqing-1949-129377915/")["name"],
                         "Chongqing 1949")  # a number in the name
        self.assertEqual(site_links.tripcom_activity("https://au.trip.com/travel-guide/attraction/guilin/li-river-four-star-cruise-ship-1500450")
                         ["name"], "Li River Four Star Cruise Ship")
        self.assertEqual(site_links.tripcom_activity("https://www.klook.com/activity/1-x/"), {})

    def test_a_blocked_tripcom_link_still_finds_the_place(self):
        with mock.patch("journeys.activity_import.fetch_page", side_effect=BLOCKED), \
                mock.patch("journeys.link_details.geocode.search", return_value=[PALACE]) as search:
            data = self.post(url="https://au.trip.com/travel-guide/attraction/beijing/the-palace-museum-75595/", day=self.day.pk).json()
        self.assertEqual(search.call_args.args[0], "The Palace Museum, Beijing")
        self.assertEqual((data["fields"]["name"], data["place"]["name"]), ("The Palace Museum", "The Palace Museum"))
        self.assertIn("Trip.com doesn't let the server read its pages, so the name and city came from the link, and the map pin", data["note"])

    def test_a_tour_link_with_only_a_number_uses_the_title(self):
        url = "https://www.trip.com/things-to-do/detail/36394123/"
        with mock.patch("journeys.activity_import.fetch_page", side_effect=BLOCKED):
            response = self.post(url=url)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Type its name in Title", response.json()["error"])
        with mock.patch("journeys.activity_import.fetch_page", side_effect=BLOCKED), \
                mock.patch("journeys.link_details.geocode.search", return_value=[PALACE]):
            data = self.post(url=url, title="Forbidden City").json()
        self.assertEqual(data["fields"]["name"], "Forbidden City")
        self.assertIn("the map pin was found from the title you typed", data["note"])

    def test_other_blocked_sites_still_say_so(self):
        with mock.patch("journeys.activity_import.fetch_page", side_effect=BLOCKED):
            response = self.post(url="https://www.example.com/sight")
        self.assertEqual((response.status_code, response.json()["error"]), (422, str(BLOCKED)))

    def test_saving_a_tripcom_activity_fills_the_map_pin_instead_of_failing(self):
        with mock.patch("journeys.activity_import.fetch_page", side_effect=BLOCKED), \
                mock.patch("journeys.link_details.geocode.search", return_value=[PALACE]):
            response = self.client.post(reverse("manage:item_create", args=[self.trip.pk]), {
                "day": self.day.pk, "title": "Palace Museum", "block": "morning",
                "links": "Trip.com | https://au.trip.com/travel-guide/attraction/beijing/the-palace-museum-75595/"}, follow=True)
        item = Item.objects.get(title="Palace Museum")
        self.assertEqual(item.place.name, "The Palace Museum")  # the link's name for it
        self.assertContains(response, "Filled in from its link: map location")
        self.assertNotContains(response, "Couldn&#x27;t read its link")
        self.assertIn("Trip.com doesn't let the server read its pages", item.details_note)

    def test_pasted_tripcom_text(self):
        text = ("Trip.com\nThe Palace Museum\n4.8/5\nAddress\n4 Jingshan Front St, Dongcheng District, Beijing\n"
                "Opening Hours\nTue-Sun 08:30-17:00\nFrom A$ 12.50\n")
        found = activity_import.extract(text, name_hint="Palace Museum")
        self.assertEqual((found["name"], found["address"], found["opening_hours"]),
                         ("Palace Museum", "4 Jingshan Front St, Dongcheng District, Beijing", "Tue-Sun 08:30-17:00"))
        self.assertEqual((found["amount"], found["currency"]), ("12.50", "AUD"))
        with self.assertRaisesMessage(stay_import.PageError, "Type the activity's name in Title"):
            activity_import.extract("Nothing useful here at all")

    def test_hours_after_a_heading_need_a_time(self):
        self.assertEqual(page_details.opening_hours([], "Opening hours\nmay vary on public holidays"), "")
        self.assertEqual(page_details.address_line("Address\nView on map"), "")

    def test_read_activity_passes_the_title_on(self):
        with mock.patch("journeys.link_details.activity_import.read_link", return_value={"name": "X"}) as read, \
                mock.patch("journeys.link_details.geocode.search", return_value=[]):
            link_details.read_activity(self.trip, "https://au.trip.com/x", title="Palace")
        self.assertEqual(read.call_args.kwargs["name_hint"], "Palace")
