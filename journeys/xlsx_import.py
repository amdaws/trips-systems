"""Import a trip from the 'China 2027 Trip (Multi-Tab)' workbook layout.

Sheets: 'Intinerary (Overview)' (days and overnight stops), 'Intinerary
(Detailed)' (four time blocks a day: transport, activities, notes, links),
'Accomodations', 'Budgets' and 'Overview' (general notes). Transport text such
as 'Train: G388 0720hrs Hong Kong -> Shenzhenbei 0744hrs' becomes legs, with
places matched against the airport and station tables (run load_places first).
"""

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from difflib import get_close_matches

from django.db import transaction
from openpyxl import load_workbook

from . import geocode, routing
from .models import Block, BudgetItem, ChecklistItem, Day, Item, Leg, Place, Stay, Traveller, Trip, normalise

BLOCKS = {"0000-0600": Block.NIGHT, "0600-1200": Block.MORNING, "1200-1800": Block.AFTERNOON, "1800-2400": Block.EVENING}

# Spreadsheet spelling -> (English, Chinese, geocoder query, fallback lat/lon, kind)
CITIES = {
    "brisbane": ("Brisbane", "布里斯班", "Brisbane, Queensland, Australia", (-27.4698, 153.0251)),
    "taipei": ("Taipei", "台北", "Taipei, Taiwan", (25.0330, 121.5654)),
    "hong kong": ("Hong Kong", "香港", "Tsim Sha Tsui, Hong Kong", (22.2976, 114.1722)),
    "guilin": ("Guilin", "桂林", "桂林市", (25.2736, 110.2900)),
    "yangshou": ("Yangshuo", "阳朔", "阳朔镇, 阳朔县", (24.7788, 110.4966)),
    "lijiang": ("Lijiang", "丽江", "丽江古城区", (26.8721, 100.2299)),
    "shangri la": ("Shangri-La", "香格里拉", "香格里拉市", (27.8297, 99.7068)),
    "chengdu": ("Chengdu", "成都", "成都市", (30.5728, 104.0668)),
    "jiuzhaigao": ("Jiuzhaigou", "九寨沟", "漳扎镇", (33.2600, 103.9180)),
    "chongqing": ("Chongqing", "重庆", "渝中区", (29.5630, 106.5516)),
    "zhangjiajie": ("Zhangjiajie", "张家界", "张家界市", (29.1171, 110.4792)),
    "furong": ("Furong Town", "芙蓉镇", "芙蓉镇, 永顺县", (28.7900, 109.9600)),
    "wangxian": ("Wangxian", "望仙", "望仙乡, 上饶", (28.6400, 117.6500)),
    "qingdao": ("Qingdao", "青岛", "青岛市", (36.0671, 120.3826)),
    "shenzhen": ("Shenzhen", "深圳", "深圳市", (22.5431, 114.0579)),
    "macau": ("Macau", "澳门", "Macau", (22.1987, 113.5439)),
}

# Spreadsheet station spelling -> (station's Chinese name in OpenStreetMap, note if it's an assumption)
STATIONS = {
    "hong kong": ("香港西九龍", ""),
    "shenzhenbei": ("深圳北", ""),
    "shenzhenbei (north)": ("深圳北", ""),
    "guilin": ("桂林", "The spreadsheet says 'Guilin'; Guilin station is assumed (it could be Guilin North or Guilin West). Please confirm."),
    "shangri la city": ("香格里拉", ""),
    "lijiang": ("丽江", ""),
    "chengdu": ("成都东", "The spreadsheet says 'Chengdu'; Chengdu East is assumed (it could be Chengdu West). Please confirm."),
    "huanglongjiuzhai": ("黄龙九寨", ""),
    "chongqing": ("重庆北", "The spreadsheet says 'Chongqing'; Chongqing North is assumed (it could be Chongqing West). Please confirm."),
    "chongqing east": ("重庆东", ""),
    "zhnagjaijeixi": ("张家界西", ""),
    "furongzhen": ("芙蓉镇", ""),
    "shangrao": ("上饶", ""),
    "yiwu": ("义乌", ""),
    "hangzhouxi": ("杭州西", ""),
    "qingdaoxi": ("青岛西", ""),
    "qingdao": ("青岛", ""),
    "shijiazhuang": ("石家庄", ""),
}

AIRLINES = {"CX": "Cathay Pacific", "GT": "Air Guilin", "SC": "Shandong Airlines"}

TRANSPORT_RE = re.compile(
    r"(?P<mode>Flight|Train)\s*[:\-]\s*(?P<number>[A-Z]{1,2}\d{1,4})\s+(?P<depart>\d{4})(?:hrs)?\s+"
    r"(?P<origin>.+?)\s*->\s*(?P<destination>.+?)"
    r"(?:\s+(?P<arrive>\d{4})(?:hrs)?(?P<next_day>\+1)?)?(?:\s*\((?P<seat>[^)]*Class)\))?\s*$",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s)]+")


class ImportProblem(Exception):
    pass


def import_workbook(path, title="China 2027", slug="china-2027", replace=False, log=print):
    wb = load_workbook(path, data_only=True)
    with transaction.atomic():
        existing = Trip.objects.filter(slug=slug).first()
        if existing:
            if not replace:
                raise ImportProblem(f"A trip with the slug '{slug}' already exists (use --replace).")
            existing.delete()
        importer = _Importer(wb, log)
        trip = importer.run(title, slug)
    # Draw flights now; trains, boats and road legs are traced by the background job.
    for leg in trip.legs.select_related("origin", "destination"):
        routing.refresh_path(leg, allow_network=False)
    return trip


class _Importer:
    def __init__(self, wb, log):
        self.wb = wb
        self.log = log
        self.cities = {}

    # ---- helpers ----

    def rows(self, sheet, first_row):
        for row in self.wb[sheet].iter_rows(min_row=first_row, values_only=True):
            yield [("" if v is None else v) for v in row]

    @staticmethod
    def text(value):
        """Cell text with spacing tidied; line breaks are kept (cells use them to separate entries)."""
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in str(value).splitlines())
        return "\n".join(line for line in lines if line)

    @staticmethod
    def parts(value):
        """Split a cell into entries on line breaks or ' / '."""
        return [part.strip() for part in re.split(r"\n|\s/\s", value) if part.strip()]

    @staticmethod
    def parse_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        value = str(value).strip()
        if not value:
            return None
        d, m, y = (int(part) for part in value.split("/"))
        return date(y + 2000 if y < 100 else y, m, d)

    @staticmethod
    def parse_time(value):
        return time(int(value[:2]) % 24, int(value[2:])) if value else None

    @staticmethod
    def money(value):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01")) if str(value).strip() else None
        except InvalidOperation:
            return None

    def city(self, key):
        key = key.lower().strip()
        if key not in CITIES:
            return None
        if key not in self.cities:
            english, chinese, query, fallback = CITIES[key]
            place = Place.objects.filter(kind=Place.Kind.CITY, name=english, name_local=chinese).first()
            if not place:
                try:
                    results = geocode.search(query, limit=1)
                except Exception as exc:
                    results = []
                    self.log(f"  Geocoding '{query}' failed ({exc}); using approximate coordinates")
                if results:
                    place = geocode.save_result(results[0], kind=Place.Kind.CITY, name=english, name_local=chinese)
                else:
                    place = Place.objects.create(
                        kind=Place.Kind.CITY, name=english, name_local=chinese, lat=fallback[0], lon=fallback[1]
                    )
            self.cities[key] = place
        return self.cities[key]

    def station(self, raw):
        local, note = STATIONS.get(raw.lower().strip(), ("", ""))
        candidates = Place.objects.filter(kind=Place.Kind.STATION)
        place = candidates.filter(name_local=local).first() if local else None
        if not place:
            place = candidates.filter(search__contains=normalise(raw)).first()
            note = f"Matched '{raw}' automatically to {place.name}; please check." if place else note
        if not place:
            raise ImportProblem(f"Couldn't find a station for '{raw}'. Run load_places first, or add it to STATIONS.")
        return place, note

    def airport(self, code):
        place = Place.objects.filter(kind=Place.Kind.AIRPORT, code=code.upper()).first()
        if not place:
            raise ImportProblem(f"Unknown airport code '{code}'. Run load_places first.")
        return place

    # ---- the import ----

    def run(self, title, slug):
        overview = [r for r in self.rows("Intinerary (Overview)", 3) if self.parse_date(r[1])]
        dates = [self.parse_date(r[1]) for r in overview]
        trip = Trip.objects.create(
            title=title,
            slug=slug,
            start_date=min(dates),
            end_date=max(dates),
            summary=(
                "Five weeks through Taiwan, Hong Kong, mainland China and Macau: Taipei at New Year, the karst "
                "peaks of Guilin and Yangshuo, snow mountains around Lijiang, the lakes of Jiuzhaigou, Chongqing, "
                "Zhangjiajie and the old river towns of Hunan, with China's high-speed rail network in between."
            ),
        )
        self.log(f"Trip: {trip.title} ({trip.start_date} to {trip.end_date})")
        self.import_notes(trip)
        days = self.import_days(trip, overview)
        self.import_details(trip, days)
        self.import_stays(trip, days)
        self.import_budget(trip)
        self.add_ticket_reminders(trip)
        return trip

    def import_notes(self, trip):
        lines = []
        for row in self.rows("Overview", 1):
            cells = [self.text(v) for v in row if self.text(v) and self.text(v) != "Table 1"]
            if not cells:
                continue
            joined = " ".join(c.rstrip(">-").strip() for c in cells)
            lines.append(joined)
            match = re.search(r"tickets open (\d+) days", joined, re.I)
            if match:
                trip.train_ticket_window = int(match.group(1))
        trip.notes = "\n".join(lines)
        trip.save()

    def import_days(self, trip, overview):
        days = {}
        for row in overview:
            day_date = self.parse_date(row[1])
            stay = self.text(row[3])
            overnight = self.city(stay)
            notes = f"Overnight: {stay}" if stay and not overnight else ""
            days[day_date] = Day.objects.create(
                trip=trip, date=day_date, title=self.text(row[2]), overnight=overnight, notes=notes
            )
        self.log(f"  {len(days)} days")
        return days

    def import_details(self, trip, days):
        current = None
        seen_legs = set()
        items = legs = 0
        for row in self.rows("Intinerary (Detailed)", 3):
            current = self.parse_date(row[0]) or current
            block = BLOCKS.get(self.text(row[2]))
            if not current or not block or current not in days:
                continue
            day = days[current]
            transport, activities, notes, links = (self.text(v) for v in row[3:7])
            for segment in self.parts(transport):
                created = self.add_leg(trip, current, block, segment, notes, links, seen_legs)
                legs += created
            items += self.add_items(day, block, activities, notes if not transport else "", links if not transport else "")
        self.log(f"  {legs} transport legs, {items} itinerary items")

    def add_items(self, day, block, activities, notes, links):
        titles = self.parts(activities)
        link_lines = []
        for part in self.parts(links):
            url = URL_RE.search(part)
            if url:
                label = part[: url.start()].strip(" -–:") or ""
                link_lines.append(f"{label} | {url.group(0)}" if label else url.group(0))
        created = []
        for position, title in enumerate(titles):
            url = URL_RE.search(title)
            if url:
                link_lines.append(url.group(0))
                domain = re.sub(r"^www\.", "", url.group(0).split("/")[2])
                title = URL_RE.sub(domain, title).replace("(" + domain + ")", "").strip()
            created.append(Item(day=day, block=block, title=title[:200], order=position, added_from="spreadsheet"))
        if not created and (notes or link_lines):
            created.append(Item(day=day, block=block, title="Ideas and notes", is_public=False, added_from="spreadsheet"))
        if created:
            created[0].private_notes = "\n".join(self.parts(notes))
            created[0].links = "\n".join(link_lines)
        for item in created:
            item.save()
        return len(created)

    def add_leg(self, trip, day_date, block, text, notes, links, seen):
        match = TRANSPORT_RE.search(text)
        if match:
            number = match["number"].upper()
            key = (number, match["depart"])
            if key in seen:
                return 0
            seen.add(key)
            depart, arrive = self.parse_time(match["depart"]), self.parse_time(match["arrive"])
            if match["mode"].lower() == "flight":
                origin, destination = self.airport(match["origin"]), self.airport(match["destination"])
                mode, operator, note = Leg.Mode.FLIGHT, AIRLINES.get(number[:2], ""), ""
            else:
                (origin, note_a), (destination, note_b) = self.station(match["origin"]), self.station(match["destination"])
                mode, operator, note = Leg.Mode.TRAIN, "China Railway", " ".join(filter(None, [note_a, note_b]))
            arrive_date = None
            if arrive:
                arrive_date = day_date + timedelta(days=1) if (match["next_day"] or arrive < depart) else day_date
            Leg.objects.create(added_from="spreadsheet",
                trip=trip, mode=mode, number=number, operator=operator, origin=origin, destination=destination,
                depart_date=day_date, depart_time=depart, arrive_date=arrive_date, arrive_time=arrive,
                seat_class=(match["seat"] or "").replace(" Class", ""), status=Leg.Status.BOOKED if mode == Leg.Mode.FLIGHT else Leg.Status.PLANNED,
                notes=note,
            )
            return 1
        return self.add_other_leg(trip, day_date, text, notes, links)

    def add_other_leg(self, trip, day_date, text, notes, links):
        lowered = text.lower()
        if lowered.startswith("boat") and "li river" in lowered:
            leg = dict(mode=Leg.Mode.BOAT, origin=self.city("guilin"), destination=self.city("yangshou"),
                       public_note="Li River cruise", notes=notes)
        elif lowered.startswith("didi to guilin"):
            leg = dict(mode=Leg.Mode.CAR, origin=self.city("yangshou"), destination=self.airport("KWL"),
                       notes=text, status=Leg.Status.PLANNED)
        elif "shuttle to huanglong" in lowered:
            leg = dict(mode=Leg.Mode.SHUTTLE, origin=self.station("huanglongjiuzhai")[0], destination=self.city("jiuzhaigao"),
                       public_note="Shuttle via Huanglong", notes=text, status=Leg.Status.PLANNED)
        else:
            self.log(f"  Skipped transport text (add it in the planner): {text!r}")
            return 0
        leg.setdefault("status", Leg.Status.PLANNED)
        Leg.objects.create(trip=trip, depart_date=day_date, added_from="spreadsheet", **leg)
        return 1

    def import_stays(self, trip, days):
        runs = []
        for day in sorted(days.values(), key=lambda d: d.date):
            key = day.overnight.name if day.overnight else None
            if key and runs and runs[-1][0] == key and runs[-1][2] == day.date - timedelta(days=1):
                runs[-1][2] = day.date
            elif key:
                runs.append([key, day.date, day.date, day.overnight])
        used = set()
        count = 0
        for row in self.rows("Accomodations", 3):
            label = self.text(row[0])
            if not label:
                continue
            base = re.sub(r"\s*\d+$", "", label)
            names = [run[0] for run in runs]
            match = get_close_matches(CITIES.get(base.lower(), (base,))[0], names, n=1, cutoff=0.6)
            run = next((r for i, r in enumerate(runs) if match and r[0] == match[0] and i not in used), None)
            if not run:
                self.log(f"  Couldn't place stay '{label}' on the itinerary; skipped")
                continue
            used.add(runs.index(run))
            times = re.findall(r"(\d{4})hrs", self.text(row[5]))
            Stay.objects.create(added_from="spreadsheet",
                trip=trip, label=label, name=self.text(row[3]), address=self.text(row[4]), city=run[3],
                check_in_date=run[1], check_out_date=run[2] + timedelta(days=1),
                check_in_time=self.parse_time(times[0]) if times else None,
                check_out_time=self.parse_time(times[1]) if len(times) > 1 else None,
                booking_ref=self.text(row[2]), status="booked" if self.text(row[1]).upper() == "Y" else "planned",
                nearest_metro=self.text(row[6]),
            )
            count += 1
        self.log(f"  {count} stays")

    def import_budget(self, trip):
        travellers = {}
        stays = {s.label.lower(): s for s in trip.stays.all()}
        count = 0
        for order, row in enumerate(self.rows("Budgets", 3)):
            description = self.text(row[1])
            if not description or description.lower() == "train-x":
                continue
            prefix, _, rest = description.partition("-")
            category = {
                "flights": BudgetItem.Category.FLIGHT, "hotel": BudgetItem.Category.ACCOMMODATION,
                "train": BudgetItem.Category.TRAIN, "currency": BudgetItem.Category.CASH,
            }.get(prefix.lower(), BudgetItem.Category.ACTIVITY if "disney" in description.lower() else BudgetItem.Category.OTHER)
            paid_by = None
            initials = self.text(row[8]).upper()
            if initials:
                if initials not in travellers:
                    travellers[initials] = Traveller.objects.create(trip=trip, name=initials, initials=initials)
                paid_by = travellers[initials]
            stay = None
            if category == BudgetItem.Category.ACCOMMODATION:
                match = get_close_matches(rest.lower(), list(stays), n=1, cutoff=0.6)
                stay = stays[match[0]] if match else None
            flag = lambda v: self.text(v).upper() == "Y"
            BudgetItem.objects.create(
                trip=trip, category=category, description=f"{prefix}: {rest}" if rest else description,
                estimate=self.money(row[2]), actual=self.money(row[3]),
                budgeted=flag(row[4]), booked=flag(row[5]), purchased=flag(row[6]), funded=flag(row[7]),
                paid_by=paid_by, stay=stay, order=order,
            )
            count += 1
        self.log(f"  {count} budget items, travellers: {', '.join(travellers) or 'none'}")

    def add_ticket_reminders(self, trip):
        count = 0
        for leg in trip.legs.filter(mode=Leg.Mode.TRAIN).select_related("origin", "destination"):
            ChecklistItem.objects.create(
                trip=trip, leg=leg, days_before=trip.train_ticket_window,
                text=f"Train tickets open: {leg.number} {leg.origin.name} → {leg.destination.name}",
            )
            count += 1
        self.log(f"  {count} ticket reminders ({trip.train_ticket_window} days before each train)")
