import re
import secrets
import unicodedata
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.db import models
from django.db.models import Q
from django.urls import reverse


def new_token():
    return secrets.token_urlsafe(18)


VISIBILITY_HELP = "Public: anyone can see it on the trip's pages. Private: only you (signed in) and people with the secret link."


class BookingStatus(models.TextChoices):
    """Where a plan stands, for transport, stays and activities alike (shown in the planner only).
    Planned: its cost is an estimate (counted in the budget's estimate). Booked: its cost is what was
    paid. Adding a confirmation books it. (Ideas were merged into Planned on 15 Nov 2026.)"""

    PLANNED = "planned", "Planned"
    BOOKED = "booked", "Booked"


class AddedFrom(models.TextChoices):
    """How a stay, transport or activity was added, shown in its edit area. Blank: before this was kept."""

    HAND = "hand", "Added by hand"
    LINK = "link", "Added from a booking link"
    IMPORT = "import", "Added from an imported booking confirmation"
    SPREADSHEET = "spreadsheet", "Imported from the planning spreadsheet"


def confirmation_link(plan):
    """View confirmation shows only for a confirmation that was imported: the link given to Add
    confirmation, else the confirmation file uploaded (Add confirmation or Import a booking). Not a
    pasted confirmation, and not the plan's own booking page."""
    from django.urls import reverse

    if plan.confirmation_url:
        return plan.confirmation_url
    if plan.confirmation_document_id:
        trip_id = plan.day.trip_id if hasattr(plan, "day_id") else plan.trip_id
        return reverse("manage:document_download", args=[trip_id, plan.confirmation_document_id])
    return ""


def _added_from(field):
    """The same three fields on stays, transport and activities: how it was added, and its confirmation link."""
    return {
        "added_from": lambda: models.CharField(max_length=12, choices=AddedFrom.choices, blank=True, editable=False),
        "added_from_url": lambda: models.URLField(max_length=500, blank=True, editable=False, help_text="The link it was filled in from."),
        "confirmation_url": lambda: models.URLField(
            "confirmation link", max_length=500, blank=True, editable=False,
            help_text="The confirmation link given to Add confirmation (Trip.com or similar), opened by View confirmation.",
        ),
        "confirmation_document": lambda: models.ForeignKey(
            "Document", on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name="+",
            help_text="The confirmation file uploaded to Add confirmation or Import a booking, opened by View confirmation.",
        ),
        "details_checked_at": lambda: models.DateTimeField(null=True, blank=True, editable=False),
        "details_note": lambda: models.CharField(max_length=300, blank=True, editable=False,
                                                 help_text="Why the link couldn't be read, the last time it was tried."),
    }[field]()


def normalise(text):
    """Lowercase, accent-free text used for place search ("Shenzhen Běi" -> "shenzhen bei")."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


def place_search_text(name, name_local="", code="", aliases=""):
    """Everything a place can be found by, including the pinyin spellings people type for Chinese
    stations ("Shenzhen North" -> "shenzhen bei", "shenzhenbei"; 芙蓉镇 -> "furongzhen") and names without spaces."""
    parts = [name, name_local, code, aliases, name.replace(" ", "")]
    if name_local and re.search(r"[\u4e00-\u9fff]", name_local):
        from pypinyin import lazy_pinyin

        parts.append("".join(lazy_pinyin(name_local)))  # 芙蓉镇 -> furongzhen
    match = re.fullmatch(r"(.+?)\s+(North|South|East|West)", name)
    if match:
        pinyin = {"North": "bei", "South": "nan", "East": "dong", "West": "xi"}[match[2]]
        parts += [f"{match[1]} {pinyin}", f"{match[1]}{pinyin}".replace(" ", "")]
    return normalise(" ".join(filter(None, parts)))[:500]


class Block(models.TextChoices):
    """The four parts of a day used throughout the planner."""

    NIGHT = "night", "Night (00:00–06:00)"
    MORNING = "morning", "Morning (06:00–12:00)"
    AFTERNOON = "afternoon", "Afternoon (12:00–18:00)"
    EVENING = "evening", "Evening (18:00–24:00)"

    @classmethod
    def for_time(cls, value):
        if value is None:
            return None
        if value.hour < 6:
            return cls.NIGHT
        if value.hour < 12:
            return cls.MORNING
        if value.hour < 18:
            return cls.AFTERNOON
        return cls.EVENING


BLOCK_ORDER = {block.value: index for index, block in enumerate(Block)}

CURRENCIES = [
    ("AUD", "AUD – Australian dollar"),
    ("CNY", "CNY – Chinese yuan"),
    ("HKD", "HKD – Hong Kong dollar"),
    ("TWD", "TWD – New Taiwan dollar"),
    ("MOP", "MOP – Macanese pataca"),
    ("USD", "USD – US dollar"),
    ("EUR", "EUR – Euro"),
    ("GBP", "GBP – British pound"),
    ("JPY", "JPY – Japanese yen"),
]


class Place(models.Model):
    class Kind(models.TextChoices):
        CITY = "city", "City or town"
        AIRPORT = "airport", "Airport"
        STATION = "station", "Railway station"
        METRO = "metro", "Metro station"
        TRAM = "tram", "Tram or light rail stop"
        PIER = "pier", "Pier or port"
        ATTRACTION = "attraction", "Attraction"
        HOTEL = "hotel", "Accommodation"
        OTHER = "other", "Other"

    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    name = models.CharField("English name", max_length=200)
    name_local = models.CharField("local name", max_length=200, blank=True, help_text="For example 深圳北")
    code = models.CharField(max_length=10, blank=True, db_index=True, help_text="IATA code for airports")
    country = models.CharField(max_length=2, blank=True)
    lat = models.FloatField("latitude")
    lon = models.FloatField("longitude")
    tz = models.CharField("time zone", max_length=64, blank=True)
    source = models.CharField(max_length=20, default="manual")
    source_id = models.CharField(max_length=60, blank=True)
    aliases = models.CharField(max_length=300, blank=True, help_text="Other spellings that should find this place.")
    city = models.CharField(max_length=120, blank=True, help_text="The city or town it serves, e.g. 'Hong Kong'.")
    city_local = models.CharField("local city name", max_length=120, blank=True)
    search = models.CharField(max_length=500, blank=True, db_index=True, editable=False)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_id"], condition=~Q(source_id=""), name="unique_place_per_source"
            )
        ]

    def __str__(self):
        label = self.name
        if self.code:
            label += f" ({self.code})"
        if self.name_local and self.name_local != self.name:
            label += f" {self.name_local}"
        return label

    def save(self, *args, **kwargs):
        self.refresh_derived()
        super().save(*args, **kwargs)

    def refresh_derived(self):
        """Fill in the time zone, city and search text (also used by the bulk dataset loaders)."""
        if not self.city:
            from .cities import derive_city

            self.city, derived_local = derive_city(self)
            self.city_local = self.city_local or derived_local
        if not self.tz:
            from .geo import timezone_for

            self.tz = timezone_for(self.lat, self.lon)
        self.search = place_search_text(self.name, self.name_local, self.code, self.aliases)

    @property
    def zone(self):
        return ZoneInfo(self.tz or "UTC")

    @property
    def city_name(self):
        return self.city or self.name

    @property
    def visitor_city_name(self):
        """city_name as visitors may see it: a hotel is only ever its city (or just "Hotel")."""
        return self.city or ("Hotel" if self.kind == self.Kind.HOTEL else self.name)

    @property
    def pinyin(self):
        """'Chongqingdong' for mainland China stations, when it adds something to the English name."""
        if self.country != "CN" or self.kind not in (self.Kind.STATION, self.Kind.METRO):
            return ""
        if not re.search(r"[\u4e00-\u9fff]", self.name_local or ""):
            return ""
        from pypinyin import lazy_pinyin

        spelled = "".join(lazy_pinyin(self.name_local))
        if spelled.lower() == normalise(self.name).replace(" ", "").replace("-", ""):
            return ""
        return spelled[:1].upper() + spelled[1:]

    @property
    def coords(self):
        return [self.lat, self.lon]


class Airline(models.Model):
    """IATA airline codes (OpenFlights, ODbL), used to fill in the operator from a flight number."""

    code = models.CharField(max_length=2, unique=True)
    icao = models.CharField("ICAO code", max_length=3, blank=True)
    name = models.CharField(max_length=200)
    country = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class Trip(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private: only you"
        UNLISTED = "unlisted", "Unlisted: anyone with the secret link"
        PUBLIC = "public", "Public: listed on the home page"

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    summary = models.TextField(blank=True, help_text="Public introduction shown at the top of the trip page.")
    notes = models.TextField("private notes", blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE)
    hide_dates = models.BooleanField(
        "hide exact dates on public pages", default=False, help_text="Show 'Day 1, Day 2…' instead of calendar dates."
    )
    train_ticket_window = models.PositiveSmallIntegerField(
        "train tickets open (days before travel)", default=58, help_text="Used for automatic ticket reminders."
    )
    show_countdown = models.BooleanField("show a countdown", default=True, help_text="A live countdown above the itinerary.")
    countdown_to = models.DateTimeField(
        "count down to", null=True, blank=True,
        help_text="In the first departure city's time. Leave empty to count down to the first flight or train.")
    countdown_label = models.CharField(
        "countdown words", max_length=80, blank=True, help_text="Shown with the countdown, e.g. 'until we fly to Hong Kong'.")
    share_token = models.CharField(max_length=40, unique=True, default=new_token, editable=False)
    calendar_token = models.CharField(max_length=40, unique=True, default=new_token, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.title

    @property
    def visibility_short(self):
        return self.get_visibility_display().split(":")[0]

    @property
    def is_shared(self):
        return self.visibility != self.Visibility.PRIVATE

    def get_public_url(self):
        if self.visibility == self.Visibility.PUBLIC:
            return reverse("journeys:trip", args=[self.slug])
        if self.visibility == self.Visibility.UNLISTED:
            return reverse("journeys:shared_trip", args=[self.share_token])
        return None

    def get_manage_url(self):
        return reverse("manage:trip", args=[self.pk])

    def first_departure(self, public_only=False):
        """The first leg; for visitors (public_only), the first public one, so a private leg's date,
        time and city never reach them."""
        legs = self.legs.filter(is_public=True) if public_only else self.legs
        return legs.select_related("origin").order_by("depart_date", "depart_time", "id").first()

    def countdown_zone(self, public_only=False):
        """The time zone the countdown is set in: the first departure's, else the site's."""
        from django.utils import timezone

        leg = self.first_departure(public_only)
        return leg.origin.zone if leg else timezone.get_default_timezone()

    def countdown_target(self, public_only=False):
        """When the countdown ends: the time set, else the first departure, else the start of day one."""
        if self.countdown_to:
            return self.countdown_to
        leg = self.first_departure(public_only)
        if leg:
            return leg.departs_at
        return datetime.combine(self.start_date, time(0), tzinfo=self.countdown_zone(public_only))

    @property
    def duration_days(self):
        return (self.end_date - self.start_date).days + 1


class Traveller(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="travellers")
    name = models.CharField(max_length=100)
    initials = models.CharField(max_length=5)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.initials if self.initials == self.name else f"{self.name} ({self.initials})"


class Day(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="days")
    date = models.DateField()
    title = models.CharField(max_length=200, blank=True, help_text="Short public summary, e.g. 'Train to Guilin'.")
    overnight = models.ForeignKey(
        Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name="overnight city"
    )
    notes = models.TextField("private notes", blank=True)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["trip", "date"], name="one_day_per_date")]

    def __str__(self):
        return f"{self.trip}: {self.date:%a %d %b %Y}"

    @property
    def number(self):
        return (self.date - self.trip.start_date).days + 1


class Item(models.Model):
    """Something planned for a day: an activity, a meal, a reminder."""

    Status = BookingStatus

    day = models.ForeignKey(Day, on_delete=models.CASCADE, related_name="items")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, help_text="Public description.")
    block = models.CharField(max_length=10, choices=Block.choices, default=Block.MORNING)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    place = models.ForeignKey(Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    address = models.CharField(max_length=300, blank=True)
    links = models.TextField(blank=True, help_text="One link per line. Optionally 'Label | https://…'.")
    opening_hours = models.CharField(max_length=300, blank=True, help_text="When it's open, e.g. 'Daily 09:00–17:30'.")
    phone = models.CharField(max_length=60, blank=True)
    email = models.EmailField(blank=True)
    private_notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=BookingStatus.choices, default=BookingStatus.PLANNED)
    added_from = _added_from("added_from")
    added_from_url = _added_from("added_from_url")
    confirmation_url = _added_from("confirmation_url")
    confirmation_document = _added_from("confirmation_document")
    details_checked_at = _added_from("details_checked_at")
    details_note = _added_from("details_note")
    is_public = models.BooleanField("public", default=False, help_text=VISIBILITY_HELP)
    order = models.PositiveIntegerField(default=0)
    directions = models.JSONField(
        null=True, blank=True, editable=False,
        help_text="How to get there from that night's hotel, worked out by the background job (journeys/metro.py).",
    )

    class Meta:
        ordering = ["day", "order", "id"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        self.block = Block.for_time(self.start_time) or self.block
        super().save(*args, **kwargs)

    @property
    def sort_key(self):
        return (BLOCK_ORDER[self.block], self.start_time or time(0), self.order, self.id or 0)

    def confirmation_link(self):
        """What View confirmation opens (see BookingStatus): its imported confirmation, or ""."""
        return confirmation_link(self)

    def link_list(self):
        result = []
        for line in self.links.splitlines():
            line = line.strip()
            if not line:
                continue
            label, _, url = line.rpartition("|") if "|" in line else ("", "", line)
            url = url.strip()
            if url.startswith(("http://", "https://")):
                result.append({"label": label.strip() or re.sub(r"^https?://(www\.)?", "", url).split("/")[0], "url": url})
        return result


class Leg(models.Model):
    """A journey from one place to another: a flight, train, boat, ride…"""

    class Mode(models.TextChoices):
        FLIGHT = "flight", "Flight"
        TRAIN = "train", "Train"
        BOAT = "boat", "Boat or river cruise"
        FERRY = "ferry", "Ferry"
        BUS = "bus", "Bus or coach"
        SHUTTLE = "shuttle", "Shuttle"
        CAR = "car", "Taxi, Didi or car"
        METRO = "metro", "Metro or tram"
        WALK = "walk", "Walk"
        OTHER = "other", "Other"

    Status = BookingStatus

    class PathSource(models.TextChoices):
        FLOWN = "flown", "Real flight track (Flightradar24)"
        ARCHIVE = "adsb", "Real flight track (adsb.lol)"
        LIVE = "live", "Real flight track, recorded live (adsb.lol)"
        GREAT_CIRCLE = "great_circle", "Great-circle flight path"
        RAIL = "rail", "Railway track (OpenStreetMap)"
        TIMETABLE = "timetable", "Timetable route (official open data)"
        WATER = "water", "River (OpenStreetMap)"
        ROAD = "road", "Road route (OSRM)"
        DRIVE = "drive", "Road route (OpenStreetMap, worked out here)"
        STRAIGHT = "straight", "Straight line"

    class PathStatus(models.TextChoices):
        PENDING = "pending", "Waiting to be calculated"
        OK = "ok", "Calculated"
        APPROX = "approx", "Approximate (straight line)"

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="legs")
    mode = models.CharField(max_length=10, choices=Mode.choices)
    number = models.CharField("flight or train number", max_length=20, blank=True)
    operator = models.CharField(max_length=100, blank=True)
    origin = models.ForeignKey(Place, on_delete=models.PROTECT, related_name="+", verbose_name="from")
    destination = models.ForeignKey(Place, on_delete=models.PROTECT, related_name="+", verbose_name="to")
    depart_date = models.DateField()
    depart_time = models.TimeField(null=True, blank=True)
    arrive_date = models.DateField(null=True, blank=True)
    arrive_time = models.TimeField(null=True, blank=True)
    seat_class = models.CharField("class", max_length=50, blank=True)
    booking_ref = models.CharField("booking reference", max_length=60, blank=True)
    url = models.URLField("link", max_length=500, blank=True, help_text="The booking or product page (Trip.com, Klook, 12Go…).")
    is_public = models.BooleanField(
        "public", default=False,
        help_text=VISIBILITY_HELP + " Public flights and trains show the airline or railway and times rounded to the quarter hour, not the number.",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    added_from = _added_from("added_from")
    added_from_url = _added_from("added_from_url")
    confirmation_url = _added_from("confirmation_url")
    confirmation_document = _added_from("confirmation_document")
    details_checked_at = _added_from("details_checked_at")
    details_note = _added_from("details_note")
    public_note = models.CharField(max_length=200, blank=True, help_text="Optional note shown on public pages.")
    notes = models.TextField("private notes", blank=True)
    path = models.JSONField(null=True, blank=True, editable=False)
    path_source = models.CharField(max_length=20, choices=PathSource.choices, blank=True, editable=False)
    path_status = models.CharField(
        max_length=10, choices=PathStatus.choices, default=PathStatus.PENDING, editable=False
    )
    path_message = models.CharField(max_length=300, blank=True, editable=False)
    path_updated = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["depart_date", "depart_time", "id"]

    def __str__(self):
        label = f"{self.get_mode_display()} {self.origin.city_name} → {self.destination.city_name}"
        return f"{self.number} {label}" if self.number else label

    @property
    def same_city(self):
        return self.origin.city_name == self.destination.city_name

    @property
    def is_high_speed(self):
        """G, D and C trains (China's high-speed services), shown in red."""
        return self.mode == self.Mode.TRAIN and self.number[:1].upper() in ("G", "D", "C")

    @property
    def mode_class(self):
        return "hsr" if self.is_high_speed else self.mode

    @property
    def mode_label(self):
        return "High-speed train" if self.is_high_speed else self.get_mode_display()

    def lookup_links(self, numbers=True, dates=True):
        """Links for checking this leg's timetable or fares on other sites (opened by you, not fetched).
        numbers=False leaves out links and searches that would show the flight or train number;
        dates=False leaves the date out (for trips that hide their dates from visitors)."""
        from urllib.parse import quote

        from django.utils.text import slugify

        a, b = self.origin, self.destination
        links = []
        if self.mode == self.Mode.FLIGHT and a.code and b.code:
            query = f"Flights from {a.code} to {b.code}" + (f" on {self.depart_date:%Y-%m-%d}" if dates else "") + (f" {self.number}" if self.number and numbers else "")
            links.append(("Google Flights", f"https://www.google.com/travel/flights?q={quote(query)}"))
            links.append(("Trip.com", f"https://au.trip.com/flights/{slugify(a.city_name)}-to-{slugify(b.city_name)}/airfares-{a.code.lower()}-{b.code.lower()}/"))
        elif self.mode == self.Mode.TRAIN:
            if self.number and numbers:
                links.append(("gaotie.net timetable", f"https://www.gaotie.net/train/{quote(self.number.upper())}/"))
            links.append(("Trip.com trains", f"https://au.trip.com/trains/china/route/{slugify(a.city_name)}-to-{slugify(b.city_name)}/"))
            links.append(("12306 (China Railway)", "https://www.12306.cn/en/index.html"))
        return links

    @property
    def block(self):
        return Block.for_time(self.depart_time) or Block.MORNING

    @property
    def sort_key(self):
        return (BLOCK_ORDER[self.block], self.depart_time or time(0), 0, self.id or 0)

    @property
    def departs_at(self):
        return datetime.combine(self.depart_date, self.depart_time or time(0), tzinfo=self.origin.zone)

    @property
    def arrives_at(self):
        if not self.arrive_time:
            return None
        return datetime.combine(self.arrive_date or self.depart_date, self.arrive_time, tzinfo=self.destination.zone)

    @property
    def arrives_next_day(self):
        return bool(self.arrive_date and self.arrive_date > self.depart_date)

    def route_key(self):
        return (self.mode, self.origin_id, self.destination_id)

    def confirmation_link(self):
        """What View confirmation opens (see BookingStatus): its imported confirmation, or ""."""
        return confirmation_link(self)


class Stay(models.Model):
    class Kind(models.TextChoices):
        HOTEL = "hotel", "Hotel"
        HOSTEL = "hostel", "Hostel"
        APARTMENT = "apartment", "Apartment"
        TRAIN = "train", "Overnight train"
        OTHER = "other", "Other"

    Status = BookingStatus

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="stays")
    label = models.CharField(max_length=100, help_text="Short name, e.g. 'Hong Kong 1'.")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.HOTEL)
    name = models.CharField("property name", max_length=200, blank=True)
    address = models.TextField(blank=True)
    city = models.ForeignKey(Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    location = models.ForeignKey(
        Place, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="Exact map location."
    )
    check_in_date = models.DateField()
    check_in_time = models.TimeField(null=True, blank=True)
    check_out_date = models.DateField()
    check_out_time = models.TimeField(null=True, blank=True)
    booking_ref = models.CharField("booking reference", max_length=60, blank=True)
    status = models.CharField(max_length=10, choices=BookingStatus.choices, default=BookingStatus.PLANNED)
    added_from = _added_from("added_from")
    added_from_url = _added_from("added_from_url")
    confirmation_url = _added_from("confirmation_url")
    confirmation_document = _added_from("confirmation_document")
    details_checked_at = _added_from("details_checked_at")
    details_note = _added_from("details_note")
    nearest_metro = models.CharField(max_length=200, blank=True)
    is_public = models.BooleanField(
        "public", default=False,
        help_text=VISIBILITY_HELP + " Public stays show the city, dates and metro or taxi directions, never the hotel's name or address.",
    )
    metro = models.JSONField(
        null=True, blank=True, editable=False,
        help_text="The hotel's nearest metro station, worked out by the background job (journeys/metro.py).",
    )
    url = models.URLField("link", max_length=500, blank=True, help_text="The property's page on Trip.com, Booking.com or its own site.")
    phone = models.CharField(max_length=60, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["check_in_date", "id"]

    def __str__(self):
        return self.label

    def confirmation_link(self):
        """What View confirmation opens (see BookingStatus): its imported confirmation, or ""."""
        return confirmation_link(self)

    def label_names_hotel(self):
        """Whether the label gives the property away. Every viewer sees the label, so it must not."""
        name, label = self.name.strip().casefold(), self.label.strip().casefold()
        return bool(name and label and name in label)

    def save(self, *args, **kwargs):
        # The stay form refuses such a label; this catches imports and anything else that saves directly.
        if self.label_names_hotel():
            self.label = (self.city.name if self.city else "Stay")[:100]
        super().save(*args, **kwargs)

    @property
    def nights(self):
        return (self.check_out_date - self.check_in_date).days


class MetroNetwork(models.Model):
    """A city's metro stations and lines: from MetroMan (metroman.cn), placed on the map with
    OpenStreetMap, or for Brisbane, Melbourne and Sydney from the official timetables (transit.py)."""

    class Source(models.TextChoices):
        METROMAN = "metroman", "MetroMan"
        TIMETABLE = "timetable", "Official timetable (GTFS)"

    slug = models.SlugField(unique=True, help_text="MetroMan's name for the city, e.g. 'hongkong'.")
    name = models.CharField(max_length=100)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.METROMAN)
    stations = models.JSONField(default=list, help_text="Empty when MetroMan has no map of the city.")
    lines = models.JSONField(default=dict)
    graph = models.JSONField(default=list, blank=True, help_text="Timetables: [from, to, line, minutes]; an empty line is a walk.")
    shapes = models.JSONField(default=dict, blank=True, help_text="Timetables: each line's real paths.")
    overview = models.JSONField(default=list, blank=True, help_text="Timetables: one simplified path per line, for the trip map.")
    status = models.JSONField(default=dict, blank=True, help_text="Timetables: when built, or why the last try failed.")
    fetched_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class ExchangeRate(models.Model):
    date = models.DateField()
    currency = models.CharField(max_length=3)
    per_aud = models.DecimalField(max_digits=16, decimal_places=6, help_text="Units of this currency per 1 AUD.")

    class Meta:
        ordering = ["-date", "currency"]
        constraints = [models.UniqueConstraint(fields=["date", "currency"], name="one_rate_per_day")]

    def __str__(self):
        return f"{self.date}: 1 AUD = {self.per_aud} {self.currency}"


class BudgetItem(models.Model):
    class Category(models.TextChoices):
        FLIGHT = "flight", "Flights"
        ACCOMMODATION = "accommodation", "Accommodation"
        TRAIN = "train", "Trains"
        TRANSPORT = "transport", "Other transport"
        ACTIVITY = "activity", "Activities and tours"
        FOOD = "food", "Food and drink"
        CASH = "cash", "Cash and currency"
        OTHER = "other", "Other"

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="budget_items")
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    description = models.CharField(max_length=200)
    estimate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    estimate_currency = models.CharField(max_length=3, choices=CURRENCIES, default="AUD")
    actual = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_currency = models.CharField(max_length=3, choices=CURRENCIES, default="AUD")
    actual_date = models.DateField(null=True, blank=True, help_text="Date paid; used for the exchange rate.")
    estimate_aud = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, editable=False)
    actual_aud = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, editable=False)
    budgeted = models.BooleanField(default=False)
    booked = models.BooleanField(default=False)
    purchased = models.BooleanField(default=False)
    funded = models.BooleanField(default=False)
    paid_by = models.ForeignKey(Traveller, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    leg = models.ForeignKey(Leg, on_delete=models.SET_NULL, null=True, blank=True, related_name="budget_items")
    stay = models.ForeignKey(Stay, on_delete=models.SET_NULL, null=True, blank=True, related_name="budget_items")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True, related_name="budget_items",
                             verbose_name="activity")
    notes = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.description

    def save(self, *args, **kwargs):
        from .money import to_aud

        self.estimate_aud = to_aud(self.estimate, self.estimate_currency)
        self.actual_aud = to_aud(self.actual, self.actual_currency, self.actual_date)
        super().save(*args, **kwargs)


class ChecklistItem(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="checklist")
    text = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    leg = models.ForeignKey(Leg, on_delete=models.CASCADE, null=True, blank=True, related_name="reminders")
    stay = models.ForeignKey(Stay, on_delete=models.CASCADE, null=True, blank=True, related_name="reminders")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True, related_name="reminders", verbose_name="activity")
    to_review = models.BooleanField(default=False, help_text="A plan to look over (ideas became plans on 15 Nov 2026).")
    days_before = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="With a leg: due this many days before it departs."
    )
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["done", models.F("due_date").asc(nulls_last=True), "id"]

    def __str__(self):
        return self.text

    def save(self, *args, **kwargs):
        if self.leg_id and self.days_before is not None:
            from datetime import timedelta

            self.due_date = self.leg.depart_date - timedelta(days=self.days_before)
        super().save(*args, **kwargs)


def document_path(instance, filename):
    return f"trips/{instance.trip_id}/{uuid.uuid4().hex}/{filename}"


class Document(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to=document_path)
    leg = models.ForeignKey(Leg, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents")
    stay = models.ForeignKey(Stay, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents", verbose_name="activity")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.title


class BookingImport(models.Model):
    """A booking confirmation waiting to be reviewed and added to a trip (pasted, uploaded or emailed in)."""

    class Source(models.TextChoices):
        PASTE = "paste", "Pasted"
        UPLOAD = "upload", "Uploaded file"
        EMAIL = "email", "Forwarded email"
        LINK = "link", "Confirmation link"

    class Status(models.TextChoices):
        NEW = "new", "Waiting for review"
        APPLIED = "applied", "Added to the trip"
        DISMISSED = "dismissed", "Dismissed"

    trip = models.ForeignKey(Trip, on_delete=models.SET_NULL, null=True, blank=True, related_name="imports")
    source = models.CharField(max_length=10, choices=Source.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    subject = models.CharField(max_length=300, blank=True)
    sender = models.CharField(max_length=200, blank=True)
    text = models.TextField(help_text="The confirmation as plain text (PINs removed).")
    received_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    stay = models.ForeignKey(Stay, on_delete=models.SET_NULL, null=True, blank=True, related_name="imports")
    leg = models.ForeignKey(Leg, on_delete=models.SET_NULL, null=True, blank=True, related_name="imports")
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True, related_name="imports", verbose_name="activity")
    link = models.URLField(max_length=500, blank=True, help_text="The confirmation link it was read from.")
    file = models.FileField(upload_to="imports/%Y/%m/", blank=True, help_text="The uploaded confirmation, kept for View confirmation.")

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return self.subject or f"{self.get_source_display()} {self.received_at:%d %b %Y %H:%M}"


class FlightLookup(models.Model):
    """Cached live schedule lookups (AeroDataBox), so the free monthly allowance goes a long way."""

    number = models.CharField(max_length=10)
    date = models.DateField()
    data = models.JSONField()
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["number", "date"], name="one_lookup_per_flight_date")]


class WeatherCache(models.Model):
    class Kind(models.TextChoices):
        FORECAST = "forecast", "Forecast"
        TYPICAL = "typical", "Typical for these dates"

    place = models.ForeignKey(Place, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    data = models.JSONField()
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["place", "kind", "start_date", "end_date"], name="one_weather_entry")
        ]


class TripChange(models.Model):
    """A transport leg, stay or activity added, changed or removed, for the trip's "Recently updated" list.
    `data` is what it looked like afterwards (before, for a removal); see changes.py."""

    class Kind(models.TextChoices):
        LEG = "leg", "Transport"
        STAY = "stay", "Stay"
        ITEM = "item", "Activity"

    class Action(models.TextChoices):
        ADDED = "added", "Added"
        CHANGED = "changed", "Changed"
        REMOVED = "removed", "Removed"

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="changes")
    kind = models.CharField(max_length=4, choices=Kind.choices)
    object_id = models.PositiveIntegerField()
    action = models.CharField(max_length=7, choices=Action.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    data = models.JSONField(default=dict)
    changed = models.JSONField(default=list, help_text="Which details changed.")
    public_before = models.BooleanField(default=False)
    public_after = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["trip", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.object_id} {self.action}"
