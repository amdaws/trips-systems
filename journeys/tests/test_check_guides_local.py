"""Local names and addresses (with taxi cards), the trip check (trip_check.py) and stop guides
(guides.py). Claude is never called: its client is replaced by a stand-in."""

from datetime import date, time
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from journeys import booking_import, claude_read, guides, link_details, trip_check
from journeys.models import Item, Leg, PlaceGuide, Stay, Trip, TripCheck

from .test_claude_and_capture import KEY, answer, claude
from .test_plans_costs import PlannerCase

GUIDE = {
    "region": "Hong Kong SAR", "lede": "A harbour city of skyscrapers and steep green hills.",
    "facts": [{"label": "Population", "value": "≈ 7.5 million"}, {"label": "Known for", "value": "Its harbour and dim sum"}],
    "history": ["A fishing coast that became a British colony in 1842.", "Returned to China in 1997."],
    "decade": [{"year": "2018", "text": "The high-speed railway to the mainland opens."}],
    "wonders": [{"name": "Lion Rock", "text": "A ridge-top walk with the whole city below."}],
}


class LocalNameTests(PlannerCase):
    def test_claude_keeps_only_names_in_the_local_script(self):
        fields = claude_read.clean("stay", {"name": "Hotel Example", "name_local": "Hotel Example",
                                            "address_local": "深圳市福田区福华路1号", "address": "1 Fuhua Rd"})
        self.assertNotIn("name_local", fields)  # the English name again
        self.assertEqual(fields["address_local"], "深圳市福田区福华路1号")
        self.assertIn("name_local", claude_read.schema("activity")["properties"])
        self.assertIn("address_local", claude_read.schema("confirmation")["properties"])

    def test_a_confirmation_fills_them(self):
        stay = self.planned_stay()
        found = {"name_local": "示例酒店", "address_local": "深圳市福田区福华路1号"}
        booking_import.apply_to_stay(stay, found, {"name_local", "address_local"})
        stay.refresh_from_db()
        self.assertEqual((stay.name_local, stay.address_local), ("示例酒店", "深圳市福田区福华路1号"))
        item = Item.objects.get(title="Hidden plan")
        booking_import.apply_to_activity(item, {"name_local": "海洋公园", "address_local": "香港仔黄竹坑道180号"}, {"name_local", "address_local"})
        item.refresh_from_db()
        self.assertEqual(item.name_local, "海洋公园")

    def test_refresh_details_fills_them_without_overwriting(self):
        stay = self.planned_stay(url="https://example.com/hotel", address_local="已有地址")
        read = {"fields": {"name_local": "示例酒店", "address_local": "别的地址"}, "city": None, "location": None, "missing": []}
        with mock.patch("journeys.link_details.read_stay", return_value=read):
            filled, _missing, error = link_details.refresh(stay)
        stay.refresh_from_db()
        self.assertEqual(error, "")
        self.assertIn("local name", filled)
        self.assertEqual((stay.name_local, stay.address_local), ("示例酒店", "已有地址"))

    def test_taxi_cards(self):
        self.planned_stay(name="Hotel Example", name_local="示例酒店", address_local="深圳市福田区福华路1号", phone="+86 755 1234 5678")
        page = self.page("taxi_cards")
        self.assertIn("示例酒店", page)
        self.assertIn("深圳市福田区福华路1号", page)
        self.assertIn('href="tel:+86 755 1234 5678"', page)
        self.assertIn("No local name or address yet", page)  # the activities, and the Hong Kong stay
        self.assertIn("Harbour walk", page)
        self.assertIn(reverse("manage:taxi_cards", args=[self.trip.pk]), self.page("stays"))

    def test_never_on_public_pages(self):
        Trip.objects.filter(pk=self.trip.pk).update(visibility=Trip.Visibility.PUBLIC)
        Stay.objects.filter(label="Hong Kong 1").update(name_local="秘密酒店", address_local="秘密地址", is_public=True)
        self.client.logout()
        for name in ("trip", "stays", "activities"):
            page = self.client.get(reverse(f"journeys:{name}", args=[self.trip.slug])).content.decode()
            self.assertNotIn("秘密", page, name)

    def test_a_label_naming_the_hotel_in_chinese_is_refused(self):
        stay = Stay(trip=self.trip, label="香港秘密酒店", name_local="秘密酒店", city=self.places["hongkong"],
                    check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3))
        self.assertTrue(stay.label_names_hotel())


class TripCheckRuleTests(PlannerCase):
    today = date(2026, 11, 15)

    def texts(self):
        return [f["text"] for f in trip_check.rules(self.trip, today=self.today)]

    def test_a_night_with_nowhere_to_stay(self):
        self.assertIn("Nowhere to stay on the night of Sat 2 Jan.", self.texts())
        self.planned_stay()
        self.assertFalse([t for t in self.texts() if t.startswith("Nowhere")])

    def test_two_stays_on_one_night(self):
        Stay.objects.create(trip=self.trip, label="Hong Kong 2", city=self.places["hongkong"],
                            check_in_date=date(2027, 1, 1), check_out_date=date(2027, 1, 2))
        self.assertIn("Two stays on the night of Fri 1 Jan: Hong Kong 1 and Hong Kong 2.", self.texts())

    def test_train_tickets_on_sale(self):
        self.assertTrue([t for t in self.texts() if "have been on sale since Thu 5 Nov" in t])
        Leg.objects.filter(number="G388").update(status="booked")
        self.assertFalse([t for t in self.texts() if "on sale" in t])

    def test_tight_and_impossible_connections(self):
        Leg.objects.create(trip=self.trip, mode=Leg.Mode.TRAIN, origin=self.places["xkl"], destination=self.places["szn"],
                           depart_date=date(2027, 1, 1), depart_time=time(7, 50), arrive_time=time(8, 20), status="booked")
        self.assertTrue([t for t in self.texts() if t.startswith("Only 30 min from arriving at Hong Kong International Airport")])
        Leg.objects.filter(depart_time=time(7, 50)).update(depart_time=time(7, 0))
        self.assertTrue([t for t in self.texts() if "leaves at 07:00, before the flight from Brisbane to Hong Kong" in t])

    def test_a_gap_between_cities(self):
        far = Leg.objects.create(trip=self.trip, mode=Leg.Mode.FLIGHT, origin=self.places["bne"], destination=self.places["hkg"],
                                 depart_date=date(2027, 1, 3), depart_time=time(9, 0), status="planned")
        gap = "Nothing gets you from Shenzhen North to Brisbane International Airport"
        self.assertTrue([t for t in self.texts() if t.startswith("Nothing gets you from Shenzhen")])
        Leg.objects.create(trip=self.trip, mode=Leg.Mode.FLIGHT, origin=self.places["hkg"], destination=self.places["bne"],
                           depart_date=date(2027, 1, 3), status="planned")  # no time: comes first that day, and gets you there
        self.assertFalse([t for t in self.texts() if t.startswith("Nothing gets you")], gap)
        far.delete()

    def test_an_activity_during_a_journey(self):
        Item.objects.create(day=self.day, title="Breakfast", start_time=time(7, 30))
        self.assertTrue([t for t in self.texts() if t.startswith("Breakfast at 07:30 is during the train")])

    def test_the_page(self):
        page = self.page("check")
        self.assertIn("Nowhere to stay on the night of Sat 2 Jan.", page)
        self.assertIn('aria-current="page">Check</a>', page)


@KEY
class TripCheckReviewTests(PlannerCase):
    REVIEW = {"findings": [{"date": "2027-01-02", "level": "problem", "text": "The park is usually closed on Saturdays."},
                           {"date": "", "level": "nonsense", "text": "Pack for rain."}]}

    def test_claude_reviews_the_plan_without_private_details(self):
        patch, client = claude(answer(self.REVIEW))
        with patch:
            response = self.client.post(reverse("manage:check", args=[self.trip.pk]), follow=True)
        page = response.content.decode()
        self.assertIn("Claude has reviewed the plan.", page)
        self.assertIn("The park is usually closed on Saturdays.", page)
        check = TripCheck.objects.get(trip=self.trip)
        self.assertEqual([f["level"] for f in check.findings], ["problem", "note"])  # by day; an unknown level is a note
        sent = client.beta.messages.create.call_args.kwargs
        text = sent["messages"][0]["content"]
        self.assertIn("Day 2, Saturday 02 January 2027. Night: no stay planned.", text)
        self.assertIn("Activity: Harbour walk, 19:00, planned.", text)
        self.assertIn("Activity: Hidden plan, some time in the morning, no time set, planned.", text)
        for secret in ("SECRET", "CX156", "G388", "Jack", "Aaron"):
            self.assertNotIn(secret, text)
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("thinking", sent)  # the test model is Opus, which can't turn thinking off

    @override_settings(CLAUDE_MODEL="claude-haiku-5-5")
    def test_haiku_reviews_without_thinking(self):
        patch, client = claude(answer(self.REVIEW))
        with patch:
            trip_check.review(self.trip)
        sent = client.beta.messages.create.call_args.kwargs
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertNotIn("betas", sent)

    def test_the_page_says_when_the_plan_has_changed(self):
        patch, _client = claude(answer(self.REVIEW))
        with patch:
            trip_check.review(self.trip)
        self.assertNotIn("the plan has changed since", self.page("check"))
        Item.objects.create(day=self.day, title="Dim sum", start_time=time(12, 0))
        self.assertIn("the plan has changed since", self.page("check"))

    @override_settings(ANTHROPIC_API_KEY="")
    def test_without_a_key(self):
        with mock.patch("journeys.claude_read._client", side_effect=AssertionError("asked Claude")):
            page = self.client.post(reverse("manage:check", args=[self.trip.pk]), follow=True).content.decode()
        self.assertIn("Claude isn&#x27;t set up on this site", page)
        self.assertFalse(TripCheck.objects.exists())


@KEY
class GuideTests(PlannerCase):
    def setUp(self):
        super().setUp()
        self.hong_kong = self.places["hongkong"]
        self.stop_url = reverse("journeys:stop", args=[self.trip.slug, self.hong_kong.pk])

    def draft(self):
        patch, client = claude(answer(GUIDE))
        with patch:
            response = self.client.post(reverse("manage:guide_draft", args=[self.trip.pk, self.hong_kong.pk]), follow=True)
        return response, client

    def test_claude_drafts_an_unpublished_guide_from_the_place_alone(self):
        response, client = self.draft()
        self.assertIn("Claude drafted the guide to Hong Kong", response.content.decode())
        guide = PlaceGuide.objects.get(place=self.hong_kong)
        self.assertFalse(guide.is_published)
        self.assertEqual(guide.fact_list, [("Population", "≈ 7.5 million"), ("Known for", "Its harbour and dim sum")])
        self.assertEqual(len(guide.paragraphs), 2)
        self.assertEqual(guide.decade_list, [("2018", "The high-speed railway to the mainland opens.")])
        sent = client.beta.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Hong Kong (香港)", sent)
        self.assertNotIn("SECRET", sent)

    def test_drafts_are_for_the_owner_only(self):
        Trip.objects.filter(pk=self.trip.pk).update(visibility=Trip.Visibility.PUBLIC)
        self.draft()
        page = self.client.get(self.stop_url).content.decode()
        self.assertIn("Draft: only you can see this page", page)
        self.client.logout()
        self.assertEqual(self.client.get(self.stop_url).status_code, 404)
        self.assertNotIn(self.stop_url, self.client.get(reverse("journeys:stays", args=[self.trip.slug])).content.decode())

    def test_a_published_guide(self):
        Trip.objects.filter(pk=self.trip.pk).update(visibility=Trip.Visibility.PUBLIC)
        self.draft()
        guide = PlaceGuide.objects.get(place=self.hong_kong)
        data = {key: getattr(guide, key) for key in ("region", "lede", "facts", "history", "decade", "wonders")}
        self.client.post(reverse("manage:guide_edit", args=[self.trip.pk, self.hong_kong.pk]), {**data, "is_published": "on"})
        self.client.logout()
        page = self.client.get(self.stop_url).content.decode()
        self.assertIn("A harbour city of skyscrapers and steep green hills.", page)
        self.assertIn("Lion Rock", page)
        self.assertIn("Xiānggǎng", page)
        for secret in ("SECRET-HOTEL", "SECRET-ADDRESS", "SECRETBOOKING"):
            self.assertNotIn(secret, page)
        self.assertNotIn("Draft:", page)
        self.assertIn(f'href="{self.stop_url}"', self.client.get(reverse("journeys:stays", args=[self.trip.slug])).content.decode())
        self.assertIn(f'href="{self.stop_url}"', self.client.get(reverse("journeys:trip", args=[self.trip.slug])).content.decode())

    def test_a_stop_not_on_the_trip(self):
        PlaceGuide.objects.create(place=self.places["shenzhen"], lede="A young city.", is_published=True)
        Trip.objects.filter(pk=self.trip.pk).update(visibility=Trip.Visibility.PUBLIC)
        url = reverse("journeys:stop", args=[self.trip.slug, self.places["shenzhen"].pk])
        self.assertEqual(self.client.get(url).status_code, 404)  # no night is spent there

    def test_the_planner_list(self):
        page = self.page("guides")
        self.assertIn("Hong Kong", page)
        self.assertIn("Draft with Claude", page)

    def test_pinyin(self):
        self.assertEqual(guides.pinyin(self.hong_kong), "Xiānggǎng")
