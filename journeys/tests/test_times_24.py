"""Times are typed and shown in 24-hour form (forms.Time24Field, config/formats/en_AU), never am or pm."""

from datetime import time

from django.template import Context, Template
from django.urls import reverse

from journeys.forms import LegForm, Time24Field
from journeys.models import Leg

from .test_plans_costs import PlannerCase


class Time24Tests(PlannerCase):
    def test_what_counts_as_a_time(self):
        field = Time24Field(required=False)
        for typed in ("720", "7:20", "07:20", "07.20", "0720", "07:20:00"):
            self.assertEqual(field.clean(typed), time(7, 20), typed)
        self.assertEqual(field.clean("2142"), time(21, 42))
        self.assertIsNone(field.clean(""))
        for typed in ("9:55pm", "24:00", "7", "21:60"):
            with self.assertRaisesMessage(Exception, "Use 24-hour time, like 07:20 or 21:42."):
                field.clean(typed)

    def test_the_transport_form_is_24_hour(self):
        leg = Leg.objects.get(number="G388")
        form = LegForm(instance=leg, trip=self.trip)
        box = str(form["depart_time"])
        self.assertIn('type="text"', box)
        self.assertIn('value="07:20"', box)
        self.assertIn('placeholder="HH:MM"', box)
        self.assertNotIn('type="time"', box)
        page = self.page("leg_edit", leg.pk)
        self.assertIn("data-time-24", page)
        for name in ("item_create", "stay_create"):
            self.assertIn("data-time-24", self.page(name))

    def test_saving_a_typed_time(self):
        leg = Leg.objects.get(number="G388")
        data = {"mode": "train", "number": "G388", "origin": leg.origin_id, "destination": leg.destination_id,
                "depart_date": "2027-01-02", "depart_time": "0720", "arrive_time": "2142", "status": "planned", "is_public": "on"}
        form = LegForm(data, instance=leg, trip=self.trip)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data["depart_time"], form.cleaned_data["arrive_time"]), (time(7, 20), time(21, 42)))
        bad = LegForm({**data, "depart_time": "9:55pm"}, instance=leg, trip=self.trip)
        self.assertFalse(bad.is_valid())
        self.assertIn("Use 24-hour time", str(bad.errors["depart_time"]))

    def test_times_on_pages_are_24_hour(self):
        self.assertEqual(Template("{{ t }}").render(Context({"t": time(21, 42)})), "21:42")
        self.assertNotIn("p.m.", self.page("legs"))
        self.assertNotIn('type="time"', self.client.get(reverse("core:journey")).content.decode())
