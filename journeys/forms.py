"""Forms for the planner dashboard."""

from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from .models import CURRENCIES, BudgetItem, ChecklistItem, Day, Document, Item, Leg, Place, PlaceGuide, Stay, Traveller, Trip

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
TIME = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
ALLOWED_UPLOADS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif", ".txt", ".csv", ".docx", ".xlsx"}
MAX_UPLOAD_MB = 20


class PlaceWidget(forms.Widget):
    """Hidden place id plus a search box with autocomplete (see static/js/manage.js)."""

    template_name = "manage/widgets/place.html"

    def __init__(self, kinds="", attrs=None):
        super().__init__(attrs)
        self.kinds = kinds

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        place = None
        if value:
            place = value if isinstance(value, Place) else Place.objects.filter(pk=value).first()
        context["widget"].update({"kinds": self.kinds, "place": place})
        return context

    def value_from_datadict(self, data, files, name):
        return data.get(name) or None


class BookingCostMixin:
    """One "Cost" (with its currency) on the stay, activity and transport forms, placed after the link,
    plus a hidden note of the link a new one was filled in from. While the plan is Planned the cost is
    an estimate (counted in the budget's estimate); once it's Booked, it's what the booking cost. The
    view puts it on the plan's own budget line once the plan is saved (booking_costs.save_cost)."""

    COST_FIELDS = ("cost", "cost_currency")

    def add_booking_cost(self, after):
        self.fields["cost"] = forms.DecimalField(
            label="Cost", required=False, min_value=0, max_digits=12, decimal_places=2,
            help_text="While planned, what you expect it to cost: it counts towards the budget's estimate. Once booked, what the booking cost. Filled in from a booking link where the page shows a price.",
        )
        self.fields["cost_currency"] = forms.ChoiceField(label="Currency", choices=CURRENCIES, initial="AUD", required=False)
        self.fields["filled_from"] = forms.CharField(required=False, max_length=500, widget=forms.HiddenInput)
        from .booking_costs import cost_shown

        shown = cost_shown(self.instance)
        if shown:
            self.initial.setdefault("cost", shown[0])
            self.initial.setdefault("cost_currency", shown[1])
        if "status" in self.fields:  # left out (an older form or script): keep what it was
            self.fields["status"].required = False
        names = [name for name in self.fields if name not in self.COST_FIELDS]
        at = names.index(after) + 1 if after in names else len(names)
        self.order_fields(names[:at] + list(self.COST_FIELDS) + names[at:])

    def clean_status(self):
        return self.cleaned_data.get("status") or self.instance.status or Leg.Status.PLANNED

    def cost_changed(self):
        """(amount or None, currency) when the cost was entered, changed or cleared, else False."""
        if not set(self.COST_FIELDS) & set(self.changed_data):
            return False
        amount = self.cleaned_data.get("cost")
        if amount is None and "cost" not in self.changed_data:
            return False  # only the currency of an empty cost changed
        return amount, self.cleaned_data.get("cost_currency") or "AUD"


def place_field(kinds="", required=True, label=None, help_text=""):
    return forms.ModelChoiceField(
        queryset=Place.objects.all(), widget=PlaceWidget(kinds), required=required, label=label, help_text=help_text
    )


class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ["title", "slug", "start_date", "end_date", "summary", "visibility", "hide_dates", "train_ticket_window",
                  "show_countdown", "countdown_to", "countdown_label", "notes"]
        widgets = {"start_date": DATE, "end_date": DATE, "summary": forms.Textarea(attrs={"rows": 3}), "notes": forms.Textarea(attrs={"rows": 4}),
                   "countdown_to": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")}
        help_texts = {"slug": "Used in the public address: /trips/<slug>/"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.zone = self.instance.countdown_zone() if self.instance.pk else None
        first = self.instance.first_departure() if self.instance.pk else None
        if first:
            self.fields["countdown_to"].help_text = f"In {first.origin.city_name} time. Leave empty to count down to the first flight or train."
        if self.zone and self.instance.countdown_to:
            self.initial["countdown_to"] = self.instance.countdown_to.astimezone(self.zone).replace(tzinfo=None)

    def clean_countdown_to(self):
        """The time as typed, in the first departure city's time (not the server's)."""
        value = self.cleaned_data.get("countdown_to")
        if value and self.zone:
            from django.utils import timezone

            value = timezone.make_naive(value).replace(tzinfo=self.zone)
        return value

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end:
            if end < start:
                raise ValidationError("The trip can't end before it starts.")
            if (end - start).days > 366:
                raise ValidationError("Trips can be at most a year long.")
        return cleaned


TravellerFormSet = inlineformset_factory(Trip, Traveller, fields=["name", "initials"], extra=1, can_delete=True)


class DayForm(forms.ModelForm):
    """A day's title and notes. Where the night is spent comes from the trip's stays."""

    class Meta:
        model = Day
        fields = ["title", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class TripDateChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, day):
        return f"{day.date:%a %-d %b} · Day {day.number}"


class ItemForm(BookingCostMixin, forms.ModelForm):
    day = TripDateChoiceField(queryset=Day.objects.none())
    place = place_field(required=False, help_text="Where it is: directions from that night's hotel are worked out from this.")

    class Meta:
        model = Item
        fields = ["day", "title", "block", "start_time", "end_time", "place", "address", "name_local", "address_local", "opening_hours",
                  "phone", "email", "description",
                  "links", "people", "status", "private_notes", "is_public"]
        widgets = {
            "start_time": TIME, "end_time": TIME,
            "description": forms.Textarea(attrs={"rows": 3}),
            "links": forms.Textarea(attrs={"rows": 3, "placeholder": "Label | https://…"}),
            "private_notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {"block": "Used when there's no start time; a start time sets the block automatically."}

    # The cost is per person: People beside it (starting at the trip's travellers) and the Total it
    # comes to, which is what goes on the budget (booking_costs: "activities").
    COST_FIELDS = ("cost", "cost_currency", "people", "cost_total")

    def __init__(self, *args, trip, **kwargs):
        from .booking_costs import cost_shown, default_people, per_person

        super().__init__(*args, **kwargs)
        self.fields["day"].queryset = trip.days.select_related("trip")
        self.default_people = default_people(trip)
        people = self.instance.people or self.default_people
        self.fields["people"].label = "People"
        self.fields["people"].widget.attrs.update({"min": 1, "max": 99, "data-cost-people": ""})
        self.fields["people"].help_text = (f"How many people the cost is for. Starts at the trip's {self.default_people} "
                                           f"traveller{'s' if self.default_people != 1 else ''} (Settings).")
        self.initial["people"] = people
        self.fields["cost_total"] = forms.CharField(
            label="Total", required=False, disabled=True, widget=forms.TextInput(attrs={"data-cost-total": "", "tabindex": "-1"}),
            help_text="Cost per person × people: what goes on the budget.")
        self.add_booking_cost(after="links")
        self.fields["cost"].label = "Cost per person"
        self.fields["cost"].widget.attrs["data-cost-each"] = ""
        self.fields["cost"].help_text = ("While planned, what you expect it to cost each person (a booking link's price is per person); "
                                         "once booked, what each person's place cost. The total counts towards the budget.")
        shown = cost_shown(self.instance) if self.instance.pk else None
        if shown:
            self.initial["cost"] = per_person(shown[0], people)
            self.initial["cost_total"] = f"{shown[1]} {shown[0]:,.2f}"

    def clean_people(self):
        return self.cleaned_data.get("people") or self.default_people

    def cost_changed(self):
        """The total (cost per person × people) when the cost or people changed, else False."""
        change = super().cost_changed()
        if change is False or change[0] is None:
            return change
        return change[0] * self.cleaned_data["people"], change[1]


class LegForm(BookingCostMixin, forms.ModelForm):
    origin = place_field(label="From")
    destination = place_field(label="To")

    class Meta:
        model = Leg
        fields = [
            "mode", "number", "operator", "origin", "destination", "depart_date", "depart_time", "arrive_date",
            "arrive_time", "seat_class", "status", "booking_ref", "url", "public_note", "notes", "is_public",
        ]
        widgets = {
            "depart_date": DATE, "arrive_date": DATE, "depart_time": TIME, "arrive_time": TIME,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {"arrive_date": "Leave empty if it arrives the same day."}

    def __init__(self, *args, trip, **kwargs):
        super().__init__(*args, **kwargs)
        self.trip = trip
        self.add_booking_cost(after="url")

    def clean(self):
        cleaned = super().clean()
        depart, arrive = cleaned.get("depart_date"), cleaned.get("arrive_date")
        if depart and not (self.trip.start_date <= depart <= self.trip.end_date):
            self.add_error("depart_date", "This date is outside the trip.")
        if depart and arrive and arrive < depart:
            self.add_error("arrive_date", "Arrival can't be before departure.")
        if cleaned.get("origin") and cleaned.get("origin") == cleaned.get("destination"):
            self.add_error("destination", "From and to are the same place.")
        return cleaned


class StayForm(BookingCostMixin, forms.ModelForm):
    city = place_field("city", required=False)
    location = place_field("hotel,attraction,other,city", required=False, label="Exact location",
                           help_text="Optional. Use 'Search the map' to find the property.")

    class Meta:
        model = Stay
        fields = [
            "label", "kind", "name", "city", "location", "address", "name_local", "address_local", "check_in_date", "check_in_time",
            "check_out_date", "check_out_time", "status", "booking_ref", "nearest_metro", "url", "phone", "email", "notes", "is_public",
        ]
        widgets = {
            "check_in_date": DATE, "check_out_date": DATE, "check_in_time": TIME, "check_out_time": TIME,
            "address": forms.Textarea(attrs={"rows": 2}), "address_local": forms.Textarea(attrs={"rows": 2, "lang": "zh"}),
            "name_local": forms.TextInput(attrs={"lang": "zh"}), "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "nearest_metro": "Optional, e.g. 'Futian, Exit D'. Left empty, the station nearest the hotel is found from its address.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_booking_cost(after="url")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("check_in_date") and cleaned.get("check_out_date") and cleaned["check_out_date"] <= cleaned["check_in_date"]:
            self.add_error("check_out_date", "Check-out must be after check-in.")
        if Stay(label=cleaned.get("label") or "", name=cleaned.get("name") or "", city=cleaned.get("city")).label_names_hotel():
            self.add_error("label", "Visitors see the label, so use the city or a short name like 'Hong Kong 1', not the property's name or part of it.")
        return cleaned


class BudgetItemForm(forms.ModelForm):
    class Meta:
        model = BudgetItem
        fields = [
            "category", "description", "actual", "actual_currency", "actual_date",
            "paid_by", "leg", "stay", "item", "notes",
        ]
        widgets = {"actual_date": DATE, "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, trip, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paid_by"].queryset = trip.travellers.all()
        self.fields["leg"].queryset = trip.legs.select_related("origin", "destination")
        self.fields["stay"].queryset = trip.stays.all()
        self.fields["item"].queryset = Item.objects.filter(day__trip=trip).select_related("day")
        self.fields["actual"].label = "Cost"


class ChecklistItemForm(forms.ModelForm):
    class Meta:
        model = ChecklistItem
        fields = ["text", "due_date", "leg", "days_before", "notes"]
        widgets = {"due_date": DATE, "notes": forms.Textarea(attrs={"rows": 2})}
        help_texts = {"due_date": "Or pick a leg and 'days before' to work the date out automatically."}

    def __init__(self, *args, trip, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["leg"].queryset = trip.legs.select_related("origin", "destination")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("days_before") is not None and not cleaned.get("leg"):
            self.add_error("leg", "Choose the leg the reminder counts back from.")
        return cleaned


class BookingImportForm(forms.Form):
    trip = forms.ModelChoiceField(queryset=Trip.objects.all(), required=False, empty_label="Work it out from the dates",
                                  help_text="Which trip this booking belongs to.")
    text = forms.CharField(widget=forms.Textarea(attrs={"rows": 8, "placeholder": "Paste the confirmation email, or the booking page (select all, copy, paste)…"}),
                           required=False, label="Paste the confirmation")
    file = forms.FileField(required=False, label="Or upload it",
                           help_text="An email saved as .eml, a PDF e-receipt, or a saved web page (.html). Up to 10 MB.")

    def clean(self):
        cleaned = super().clean()
        upload = cleaned.get("file")
        if not cleaned.get("text", "").strip() and not upload:
            raise ValidationError("Paste the confirmation or choose a file.")
        if upload:
            if not upload.name.lower().endswith((".eml", ".pdf", ".html", ".htm", ".txt")):
                self.add_error("file", "Use an .eml, .pdf, .html or .txt file.")
            elif upload.size > 10 * 1024 * 1024:
                self.add_error("file", "Files can be at most 10 MB.")
        return cleaned


class ConfirmationForm(forms.Form):
    """A booked stay, transport or activity's confirmation: its link, or the text or file when the
    site needs you signed in (booking sites usually do)."""

    link = forms.URLField(required=False, max_length=500, label="Confirmation link", assume_scheme="https",
                          widget=forms.URLInput(attrs={"placeholder": "https://…"}),
                          help_text="The booking's confirmation or itinerary page. It's also saved as the link.")
    text = forms.CharField(required=False, label="Or paste the confirmation",
                           widget=forms.Textarea(attrs={"rows": 6, "placeholder": "Paste the confirmation email or page (select all, copy, paste)…"}))
    file = forms.FileField(required=False, label="Or upload it", help_text="An .eml email, PDF e-receipt or saved web page (.html). Up to 10 MB.")

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("link") or cleaned.get("text", "").strip() or cleaned.get("file")):
            raise ValidationError("Add the confirmation link, paste the confirmation, or choose a file.")
        upload = cleaned.get("file")
        if upload and not upload.name.lower().endswith((".eml", ".pdf", ".html", ".htm", ".txt")):
            self.add_error("file", "Use an .eml, .pdf, .html or .txt file.")
        elif upload and upload.size > 10 * 1024 * 1024:
            self.add_error("file", "Files can be at most 10 MB.")
        return cleaned


class PlaceGuideForm(forms.ModelForm):
    class Meta:
        model = PlaceGuide
        fields = ["region", "lede", "facts", "history", "decade", "wonders", "is_published"]
        widgets = {
            "lede": forms.Textarea(attrs={"rows": 2}),
            "facts": forms.Textarea(attrs={"rows": 5}),
            "history": forms.Textarea(attrs={"rows": 8}),
            "decade": forms.Textarea(attrs={"rows": 4}),
            "wonders": forms.Textarea(attrs={"rows": 5}),
        }


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["title", "file", "leg", "stay"]

    def __init__(self, *args, trip, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["leg"].queryset = trip.legs.select_related("origin", "destination")
        self.fields["stay"].queryset = trip.stays.all()
        self.fields["file"].help_text = f"PDF, image, text or Office file, up to {MAX_UPLOAD_MB} MB."

    def clean_file(self):
        upload = self.cleaned_data["file"]
        name = upload.name.lower()
        if not any(name.endswith(ext) for ext in ALLOWED_UPLOADS):
            raise ValidationError("That file type isn't allowed.")
        if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValidationError(f"Files can be at most {MAX_UPLOAD_MB} MB.")
        return upload
