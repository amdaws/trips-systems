import json
import os
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from journeys.models import BudgetItem, ChecklistItem, Document, ExchangeRate, Leg, Stay, Trip

from .factories import sample_trip


class AccessTests(TestCase):
    def test_login_required(self):
        response = self.client.get(reverse("manage:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("manage:login"), response["Location"])

    def test_non_staff_cannot_use_the_planner(self):
        user = get_user_model().objects.create_user("visitor", password="x" * 16)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("manage:dashboard")).status_code, 302)

    def test_login_page(self):
        self.assertContains(self.client.get(reverse("manage:login")), "Planner login")


class PlannerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("aaron", "aaron@example.com", None)
        self.client.force_login(self.user)
        self.trip, self.places = sample_trip(visibility=Trip.Visibility.PRIVATE)
        self.tmp_media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_media, ignore_errors=True)

    def test_every_page_loads(self):
        pk = self.trip.pk
        names = ["trip", "trip_settings", "legs", "stays", "budget", "checklist", "documents",
                 "leg_create", "stay_create", "budget_create", "item_create"]
        self.assertEqual(self.client.get(reverse("manage:dashboard")).status_code, 200)
        for name in names:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"manage:{name}", args=[pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("manage:day", args=[pk, "2027-01-02"])).status_code, 200)

    def test_overview_shows_private_details(self):
        response = self.client.get(reverse("manage:trip", args=[self.trip.pk]))
        self.assertContains(response, "G388")
        self.assertContains(response, "SECRET-LEG-NOTE")
        self.assertNotContains(response, "Hidden plan")  # the itinerary is transport only
        self.assertContains(self.client.get(reverse("manage:activities", args=[self.trip.pk])), "Hidden plan")
        data = self.client.get(reverse("manage:trip_map", args=[self.trip.pk])).json()
        self.assertIn("G388", [leg.get("number") for leg in data["legs"]])

    def test_create_trip_makes_days(self):
        response = self.client.post(reverse("manage:trip_create"), {
            "title": "Japan 2028", "slug": "", "start_date": "2028-04-01", "end_date": "2028-04-05",
            "summary": "", "visibility": "private", "train_ticket_window": 30, "notes": "",
            "travellers-TOTAL_FORMS": 1, "travellers-INITIAL_FORMS": 0, "travellers-0-name": "Aaron", "travellers-0-initials": "AB",
        })
        trip = Trip.objects.get(title="Japan 2028")
        self.assertRedirects(response, trip.get_manage_url())
        self.assertEqual(trip.slug, "japan-2028")
        self.assertEqual(trip.days.count(), 5)
        self.assertEqual(trip.travellers.get().initials, "AB")

    def test_add_flight_fills_operator_and_draws_arc_until_the_real_track_is_found(self):
        self.client.post(reverse("manage:leg_create", args=[self.trip.pk]), {
            "mode": "flight", "number": "CX157", "operator": "", "origin": self.places["hkg"].pk,
            "destination": self.places["bne"].pk, "depart_date": "2027-01-03", "depart_time": "12:35",
            "arrive_time": "23:20", "status": "planned",
        })
        leg = Leg.objects.get(number="CX157")
        self.assertEqual(leg.operator, "Cathay Pacific")
        self.assertEqual(leg.path_status, Leg.PathStatus.PENDING)  # the background job looks it up in adsb.lol's archive
        self.assertGreater(len(leg.path), 10)

    def test_add_train_waits_for_background_tracing(self):
        self.client.post(reverse("manage:leg_create", args=[self.trip.pk]), {
            "mode": "train", "number": "G999", "origin": self.places["szn"].pk, "destination": self.places["xkl"].pk,
            "depart_date": "2027-01-02", "status": "planned",
        })
        leg = Leg.objects.get(number="G999")
        self.assertEqual(leg.path_status, Leg.PathStatus.PENDING)
        self.assertEqual(leg.operator, "China Railway")

    def test_leg_outside_trip_dates_is_rejected(self):
        response = self.client.post(reverse("manage:leg_create", args=[self.trip.pk]), {
            "mode": "flight", "origin": self.places["hkg"].pk, "destination": self.places["bne"].pk,
            "depart_date": "2027-03-01", "status": "planned",
        })
        self.assertContains(response, "outside the trip")

    def test_budget_converts_to_aud(self):
        ExchangeRate.objects.create(date=date(2026, 10, 1), currency="CNY", per_aud=Decimal("4.7"))
        self.client.post(reverse("manage:budget_create", args=[self.trip.pk]), {
            "category": "train", "description": "G388 tickets", "actual": "470", "actual_currency": "CNY",
        })
        item = BudgetItem.objects.get(description="G388 tickets")
        self.assertEqual(item.actual_aud, Decimal("100.00"))
        self.assertContains(self.client.get(reverse("manage:budget", args=[self.trip.pk])), "470.00 CNY")

    def test_ticket_reminders(self):
        self.client.post(reverse("manage:checklist_ticket_reminders", args=[self.trip.pk]))
        reminder = ChecklistItem.objects.get(trip=self.trip)
        self.assertEqual(reminder.due_date, date(2027, 1, 2) - timedelta(days=58))
        self.client.post(reverse("manage:checklist_ticket_reminders", args=[self.trip.pk]))
        self.assertEqual(ChecklistItem.objects.filter(trip=self.trip).count(), 1)  # not duplicated
        self.client.post(reverse("manage:checklist_toggle", args=[self.trip.pk, reminder.pk]))
        reminder.refresh_from_db()
        self.assertTrue(reminder.done)

    def test_document_upload_and_download(self):
        with override_settings(MEDIA_ROOT=self.tmp_media):
            response = self.client.post(reverse("manage:documents", args=[self.trip.pk]), {
                "title": "E-ticket", "file": SimpleUploadedFile("ticket.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
            })
            self.assertEqual(response.status_code, 302)
            doc = Document.objects.get()
            download = self.client.get(reverse("manage:document_download", args=[self.trip.pk, doc.pk]))
            self.assertEqual(download["Content-Type"], "application/pdf")
            self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.4 test")
            rejected = self.client.post(reverse("manage:documents", args=[self.trip.pk]), {
                "title": "Script", "file": SimpleUploadedFile("evil.html", b"<script>", content_type="text/html"),
            })
            self.assertContains(rejected, "That file type isn&#x27;t allowed.")
            self.client.logout()
            self.assertEqual(self.client.get(reverse("manage:document_download", args=[self.trip.pk, doc.pk])).status_code, 302)

    def test_document_files_are_removed_with_their_records(self):
        with override_settings(MEDIA_ROOT=self.tmp_media):
            first = Document.objects.create(trip=self.trip, title="A", file=SimpleUploadedFile("a.pdf", b"%PDF a"))
            second = Document.objects.create(trip=self.trip, title="B", file=SimpleUploadedFile("b.pdf", b"%PDF b"))
            paths = [first.file.path, second.file.path]
            self.client.post(reverse("manage:document_delete", args=[self.trip.pk, first.pk]))
            self.assertFalse(Document.objects.filter(pk=first.pk).exists())
            self.assertFalse(os.path.exists(paths[0]))
            self.assertTrue(os.path.exists(paths[1]))
            self.trip.delete()  # deleting a trip takes its documents' files too
            self.assertFalse(os.path.exists(paths[1]))

    def test_place_search_api(self):
        data = self.client.get(reverse("manage:api_places"), {"q": "深圳", "kinds": "station"}).json()
        self.assertEqual(data["results"][0]["name"], "Shenzhen North")
        data = self.client.get(reverse("manage:api_places"), {"q": "hkg"}).json()
        self.assertEqual(data["results"][0]["code"], "HKG")

    def test_quick_entry_api(self):
        url = reverse("manage:api_parse_leg")
        flight = self.client.post(url, json.dumps({"text": "CX156 BNE HKG 0050 0720"}), content_type="application/json").json()
        self.assertEqual((flight["mode"], flight["number"], flight["operator"]), ("flight", "CX156", "Cathay Pacific"))
        self.assertEqual((flight["origin"]["code"], flight["destination"]["code"]), ("BNE", "HKG"))
        self.assertEqual((flight["depart_time"], flight["arrive_time"]), ("00:50", "07:20"))
        train = self.client.post(url, json.dumps({"text": "G388 香港西九龍 → 深圳北 07:20 07:44"}), content_type="application/json").json()
        self.assertEqual((train["mode"], train["number"]), ("train", "G388"))
        self.assertEqual((train["origin"]["name"], train["destination"]["name"]), ("Hong Kong West Kowloon", "Shenzhen North"))


class SafetyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("aaron", "aaron@example.com", None)
        self.client.force_login(self.user)
        self.trip, self.places = sample_trip(visibility=Trip.Visibility.PRIVATE)

    def test_next_parameter_cannot_leave_the_site(self):
        item = ChecklistItem.objects.create(trip=self.trip, text="Pack")
        url = reverse("manage:checklist_toggle", args=[self.trip.pk, item.pk])
        response = self.client.post(url, {"next": "https://evil.example/"})
        self.assertEqual(response["Location"], reverse("manage:checklist", args=[self.trip.pk]))
        response = self.client.post(url, {"next": "/manage/"})
        self.assertEqual(response["Location"], "/manage/")

    def test_new_places_need_a_real_position(self):
        url = reverse("manage:api_place_create")
        place = {"source_id": "osm:1", "name": "Somewhere", "name_local": "", "kind": "sight", "country": "CN"}
        for lat, lon in [("nan", 114), (91, 114), (22, "inf"), (22, 181), ("north", 114)]:
            with self.subTest(lat=lat, lon=lon):
                response = self.client.post(url, json.dumps(place | {"lat": lat, "lon": lon}), content_type="application/json")
                self.assertEqual(response.status_code, 400)

    def test_hotel_places_stay_off_public_pages(self):
        from journeys.models import Item, Place

        hotel = Place.objects.create(kind=Place.Kind.HOTEL, name="SECRET-HOTEL-PLACE", lat=22.3, lon=114.17, tz="Asia/Hong_Kong")
        day = self.trip.days.first()
        Item.objects.create(day=day, title="Check in", place=hotel, is_public=True)
        self.trip.visibility = Trip.Visibility.PUBLIC
        self.trip.save()
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("journeys:activities", args=[self.trip.slug])), "SECRET-HOTEL-PLACE")
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("manage:activities", args=[self.trip.pk])), "SECRET-HOTEL-PLACE")


class BulkStayTests(TestCase):
    """Ticking stays on the Stays page to make them public or private, or delete them together."""

    def setUp(self):
        self.trip, self.places = sample_trip()
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.first = Stay.objects.get(trip=self.trip)
        self.second = Stay.objects.create(trip=self.trip, label="Shenzhen", city=self.places["shenzhen"],
                                          check_in_date=date(2027, 1, 2), check_out_date=date(2027, 1, 3))
        other = Trip.objects.create(title="Other", slug="other", start_date=date(2027, 5, 1), end_date=date(2027, 5, 2))
        self.stranger = Stay.objects.create(trip=other, label="Not this trip", check_in_date=date(2027, 5, 1), check_out_date=date(2027, 5, 2))
        self.url = reverse("manage:stays_bulk", args=[self.trip.pk])

    def post(self, **data):
        return self.client.post(self.url, data)

    def test_the_page_has_tickboxes(self):
        page = self.client.get(reverse("manage:stays", args=[self.trip.pk])).content.decode()
        self.assertIn('id="bulk-form"', page)
        self.assertEqual(page.count('form="bulk-form" name="stays"'), 2)

    def test_delete_asks_first_and_only_this_trip(self):
        page = self.post(stays=[self.first.pk, self.second.pk, self.stranger.pk], action="delete")
        self.assertContains(page, "Delete 2 stays?")
        self.assertContains(page, "Hong Kong 1")
        self.assertNotContains(page, "Not this trip")
        self.assertEqual(Stay.objects.count(), 3)  # nothing gone yet
        response = self.post(stays=[self.first.pk, self.second.pk, self.stranger.pk], action="delete", confirm="yes")
        self.assertRedirects(response, reverse("manage:stays", args=[self.trip.pk]))
        self.assertEqual(list(Stay.objects.values_list("label", flat=True)), ["Not this trip"])

    def test_public_and_private_together(self):
        self.post(stays=[self.first.pk, self.second.pk], action="public")
        self.assertEqual(Stay.objects.filter(trip=self.trip, is_public=True).count(), 2)
        self.post(stays=[self.second.pk], action="private")
        self.assertFalse(Stay.objects.get(pk=self.second.pk).is_public)

    def test_nothing_ticked_and_visitors(self):
        self.assertRedirects(self.post(action="delete"), reverse("manage:stays", args=[self.trip.pk]))
        self.client.logout()
        self.post(stays=[self.first.pk], action="delete", confirm="yes")
        self.assertTrue(Stay.objects.filter(pk=self.first.pk).exists())  # signed out: sent to sign in, nothing deleted
