"""Read booking confirmations: emails, pasted booking pages and PDF e-receipts.

Everything becomes plain text first, then the details are picked out: booking
number, property (English and local name), address, phone, email, check-in and
check-out dates and times, nights, room, special requests and amount paid.
Flight and train numbers already in the trip are recognised too. PINs are
stripped before anything is stored.

Besides hotel stays (Trip.com, Booking.com, TripAdvisor), activity and transfer
bookings (Klook, Viator/TripAdvisor) are read too: the activity, its date and
start or pick-up time, meeting point, pick-up and drop-off, and travellers.
"""

import email
import email.policy
import html
import io
import re
from datetime import date

from . import metro
from .models import BookingStatus, BudgetItem, Leg, Place

CURRENCY_SYMBOLS = [
    ("AU$", "AUD"), ("A$", "AUD"), ("AUD", "AUD"), ("US$", "USD"), ("USD", "USD"), ("HK$", "HKD"), ("HKD", "HKD"),
    ("MOP$", "MOP"), ("MOP", "MOP"), ("NT$", "TWD"), ("TWD", "TWD"), ("CNY", "CNY"), ("RMB", "CNY"), ("¥", "CNY"),
    ("€", "EUR"), ("EUR", "EUR"), ("£", "GBP"), ("GBP", "GBP"),
]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DATE_RE = re.compile(
    r"(?:(?P<iso>\d{4}-\d{2}-\d{2})"
    r"|(?:[A-Za-z]{3,9},?\s+)?(?P<mon1>[A-Za-z]{3,9})\.?\s+(?P<d1>\d{1,2}),?\s+(?P<y1>\d{4})"
    r"|(?:[A-Za-z]{3,9},?\s+)?(?P<d2>\d{1,2})\s+(?P<mon2>[A-Za-z]{3,9})\.?,?\s+(?P<y2>\d{4})"
    r"|(?P<d3>\d{1,2})/(?P<m3>\d{1,2})/(?P<y3>\d{4}))"
)
TIME_RE = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
# "Booking No. 1234567890", "Confirmation number: 4123.456.789" (Booking.com), "Booking reference ID: KL12345678"
# (Klook), "Booking reference: BR-1234567890" (Viator). The number has a digit in it, so words never match.
REF_RE = re.compile(
    r"(?:(?:booking|confirmation|reservation|order|itinerary|voucher)\s*(?:reference|ref\.?)?\s*(?:no\.?|number|id|code|#)"
    r"|(?:booking|reservation)\s*(?:reference|ref\.?)|PNR)"
    r"\s*[:#.]?\s*((?=[A-Z0-9.\-]*\d)[A-Z0-9][A-Z0-9.\-]{4,23})", re.I)
PIN_RE = re.compile(r"\bPIN(?:\s*(?:code|number|no\.?))?\b\s*[:#]?\s*\d{3,8}", re.I)  # Booking.com's "PIN code: 1234" too
SITES = [("booking.com", "Booking.com"), ("klook", "Klook"), ("viator", "Viator"), ("tripadvisor", "Tripadvisor"), ("trip.com", "Trip.com")]
ACTIVITY_DATE = r"(?:participation|travel|activity|tour|experience|event|visit|selected|booking)\s*date|date\s*of\s*(?:travel|experience|visit)"
PICKUP = r"pick[- ]?up"
LODGING_WORDS = re.compile(r"\b(hotel|hostel|inn|resort|suites?|apartments?|lodge|guest\s?house|villa|homestay|motel|residence|b&b)\b", re.I)


# ---------- turning files and emails into text ----------


def html_to_text(markup):
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table|section)>", "\n", markup)
    markup = re.sub(r"(?i)</t[dh]>", "  ", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return clean_text(text)


def clean_text(text):
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.replace("\r", "").split("\n"))
    return "\n".join(line for line in lines if line)


def redact(text):
    return PIN_RE.sub("PIN [removed]", text)


def text_from_email(raw):
    """(subject, sender, text) from an RFC 822 email (bytes)."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = html_to_text(content) if body.get_content_type() == "text/html" else clean_text(content)
    for part in message.iter_attachments():
        if part.get_content_type() == "application/pdf":
            text += "\n" + text_from_pdf(part.get_payload(decode=True))
    return str(message.get("subject", "")), str(message.get("from", "")), text


def text_from_pdf(data):
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return clean_text("\n".join(page.extract_text() or "" for page in reader.pages[:20]))
    except Exception:
        return ""


def text_from_upload(upload):
    """(subject, sender, text) from an uploaded .eml, .pdf, .html or .txt file."""
    name = upload.name.lower()
    data = upload.read(10 * 1024 * 1024)
    if name.endswith(".eml"):
        return text_from_email(data)
    if name.endswith(".pdf"):
        return "", "", text_from_pdf(data)
    decoded = data.decode("utf-8", errors="replace")
    if name.endswith((".html", ".htm")):
        return "", "", html_to_text(decoded)
    return "", "", clean_text(decoded)


# ---------- picking out the details ----------


def _date(match):
    try:
        if match["iso"]:
            return date.fromisoformat(match["iso"])
        if match["mon1"]:
            return date(int(match["y1"]), MONTHS[match["mon1"][:3].lower()], int(match["d1"]))
        if match["mon2"]:
            return date(int(match["y2"]), MONTHS[match["mon2"][:3].lower()], int(match["d2"]))
        return date(int(match["y3"]), int(match["m3"]), int(match["d3"]))
    except (KeyError, ValueError):
        return None


def _date_after(text, label_pattern, window=140):
    """The first date (and time) following a label like 'Check-in', skipping mentions with no date after them."""
    for label in re.finditer(label_pattern, text, re.I):
        chunk = text[label.end(): label.end() + window]
        found = next((d for d in (_date(m) for m in DATE_RE.finditer(chunk)) if d), None)
        if found:
            time_match = TIME_RE.search(chunk)
            return found, (f"{int(time_match[1]):02d}:{time_match[2]}" if time_match else ""), label.start()
    return None, "", None


def _amount(text):
    """The amount actually paid: a currency amount on a line about the total or payment, preferring the last."""
    best = None
    for line in text.split("\n"):
        if not re.search(r"\b(paid|total|prepay|amount|charged|grand)\b", line, re.I):
            continue
        for symbol, code in CURRENCY_SYMBOLS:
            for match in re.finditer(re.escape(symbol) + r"\s?([\d,]+(?:\.\d{1,2})?)", line):
                if re.search(r"-\s*$", line[: match.start()]):
                    continue  # a discount line ("- AU$72.30")
                best = (match[1].replace(",", ""), code)
                break
            else:
                continue
            break
    return best


def _property(lines):
    """(English name, local name): Trip.com puts the English name right above the Chinese one."""
    for i, line in enumerate(lines[1:], start=1):
        if re.fullmatch(r"[　-鿿＀-￯·\s()（）]{2,40}", line) and len(lines[i - 1]) < 90:
            previous = lines[i - 1]
            if not re.search(r"\d{4}|check|booking|confirm", previous, re.I):
                return re.sub(r"\s*[★☆*]+\s*$", "", previous).strip(), line.strip()
    for line in lines:
        labelled = re.match(r"(?:hotel|property|accommodation)\s*(?:name)?\s*[:：]\s*(.{3,90})$", line, re.I)
        if labelled:
            return labelled[1].strip(), ""
    for line in lines[:40]:
        if LODGING_WORDS.search(line) and len(line) < 70 and not re.search(r"\d{4}|policy|room\b|check", line, re.I):
            return line.strip(), ""
    return "", ""


def parse(text, trip=None, subject=""):
    """Booking details found in confirmation text, plus which trip legs it mentions."""
    text = redact(clean_text(text))
    lines = text.split("\n")
    found = {"booking_ref": "", "name": "", "name_local": "", "address": "", "phone": "", "email": "",
             "check_in_date": None, "check_in_time": "", "check_out_date": None, "check_out_time": "",
             "nights": None, "room": "", "special_requests": "", "amount": None, "currency": "", "legs": []}

    ref = REF_RE.search(text) or REF_RE.search(subject)
    found["booking_ref"] = ref[1].strip(".-") if ref else ""
    lowered = (subject + "\n" + text).lower()
    found["site"] = next((name for key, name in SITES if key in lowered), "")
    found["name"], found["name_local"] = _property(lines)
    if not found["name"]:
        found["name"] = re.sub(r"^.*?(?:confirmed|confirmation|booking)\s*[:\-–]\s*", "", subject, flags=re.I).strip()[:120]

    found["check_in_date"], found["check_in_time"], in_at = _date_after(text, r"check[- ]?in\b")
    found["check_out_date"], found["check_out_time"], out_at = _date_after(text, r"check[- ]?out\b")
    if found["check_in_date"] and found["check_out_date"] == found["check_in_date"]:
        # Headers side by side ("Check-in  Check-out") with the two dates below them.
        dates = [d for d in (_date(m) for m in DATE_RE.finditer(text[in_at: in_at + 300])) if d]
        if len(dates) >= 2:
            found["check_in_date"], found["check_out_date"] = dates[0], dates[1]
    nights = re.search(r"\b(\d{1,2})\s+nights?\b", text, re.I)
    found["nights"] = int(nights[1]) if nights else None
    if found["check_in_date"] and not found["check_out_date"] and found["nights"]:
        from datetime import timedelta

        found["check_out_date"] = found["check_in_date"] + timedelta(days=found["nights"])

    start = next((i for i, line in enumerate(lines) if found["name"] and line.startswith(found["name"])), None)
    if start is not None:
        for line in lines[start + 1: start + 6]:
            if line.count(",") >= 2 and not re.search(r"@|\+\d", line):
                found["address"] = re.sub(r"\s+(Transfer service|Map|View map|Get directions)$", "", line, flags=re.I)
                break
    if not found["address"]:
        labelled = re.search(r"(?im)^address\s*[:：]\s*(.{8,200})$", text)
        found["address"] = labelled[1].strip() if labelled else ""
    phone = re.search(r"(\+\d{1,3}[\s-]?\(?\d[\d\s()-]{5,}\d)", text)
    found["phone"] = phone[1].strip() if phone else ""
    mail = re.search(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", text)
    found["email"] = mail[1] if mail else ""
    room = re.search(r"(?im)^(.{4,80}\broom\b.{0,40})$", text)
    found["room"] = room[1].strip() if room and "nights" not in room[1].lower() else ""
    requests = re.search(r"(?im)^special requests?\s*[:：]?\s*(.{3,200})$", text)
    found["special_requests"] = requests[1].strip() if requests else ""
    amount = _amount(text)
    if amount:
        found["amount"], found["currency"] = amount

    _activity_details(found, text, lines, subject)

    if trip:
        numbers = {leg.number.upper(): leg for leg in trip.legs.exclude(number="")}
        for token in set(re.findall(r"\b([A-Z]{1,2}\d{1,4}|[A-Z]\d{1,5})\b", text.upper().replace(" ", " "))):
            if token in numbers:
                found["legs"].append(numbers[token].pk)
    return found


def _labelled(text, labels, length=160):
    found = re.search(rf"(?im)^(?:{labels})\s*[:：]?\s*(.{{2,{length}}})$", text)
    return found[1].strip() if found else ""


def _activity_details(found, text, lines, subject):
    """For a booking that isn't a hotel stay: whether it's an activity or a transfer, and its details."""
    found.update(kind="stay", title="", activity_date=None, start_time="", meeting_point="", pickup="", dropoff="", travellers="")
    if found["check_in_date"] and found["check_out_date"]:
        return
    pickup = re.search(rf"(?i)\b{PICKUP}\b|\bdrop[- ]?off\b|\btransfer\b|\bchauffeur\b", text)
    activity = re.search(rf"(?i){ACTIVITY_DATE}|\btour\b|\bticket|\bactivity\b|\bexperience\b|\bvoucher\b", text)
    if not (pickup or activity) and found["check_in_date"]:
        return
    found["kind"] = "transfer" if pickup and re.search(rf"(?i)\b{PICKUP}\s*(?:location|point|address|from)|\bdrop[- ]?off\b|\btransfer\b", text) else "activity"
    found["title"] = _labelled(text, r"(?:activity|tour|product|experience|package|service|event)\s*(?:name)?", 120) or re.sub(
        r"(?i)^.*?(?:booking\s*(?:is\s*)?confirmed|confirmation|your\s*booking|you're\s*going)\s*(?:for|to|[:\-–])\s*", "", subject).strip()[:120]
    when = _date_after(text, rf"{ACTIVITY_DATE}|{PICKUP}\s*(?:date|time)|\bdate\b")
    found["activity_date"], found["start_time"] = when[0], when[1]
    if not found["start_time"]:
        start = re.search(rf"(?i)(?:start(?:ing)?\s*time|{PICKUP}\s*time|departure\s*time|time)\s*[:：]?\s*([01]?\d|2[0-3]):([0-5]\d)", text)
        found["start_time"] = f"{int(start[1]):02d}:{start[2]}" if start else ""
    found["meeting_point"] = _labelled(text, r"meeting\s*point|meet(?:ing)?\s*(?:at|location)|location|venue|redemption\s*(?:location|address)")
    found["pickup"] = _labelled(text, rf"{PICKUP}\s*(?:location|point|address|from)|from")
    found["dropoff"] = _labelled(text, r"drop[- ]?off\s*(?:location|point|address)?|to")
    people = re.findall(r"(?i)\b(\d{1,2})\s*(?:x\s*)?(adults?|children|child|seniors?|infants?|persons?|people|travell?ers?|participants?)\b", text)
    found["travellers"] = ", ".join(dict.fromkeys(f"{n} {kind.lower()}" for n, kind in people))[:80]
    if not found["name"] or found["kind"] != "stay":
        found["name"] = found["title"] or found["name"]


def match_activity(trip, found):
    """The activity this booking is for: on its date, with the most words of its name in common."""
    if not trip or not found.get("activity_date"):
        return None
    from .models import Item

    wanted = set(re.findall(r"[a-z]{3,}", (found.get("title") or "").lower()))
    best = None
    for item in Item.objects.filter(day__trip=trip, day__date=found["activity_date"]):
        shared = len(wanted & set(re.findall(r"[a-z]{3,}", item.title.lower())))
        if shared and (best is None or shared > best[0]):
            best = (shared, item)
    return best[1] if best else None


def match_transfer(trip, found):
    """The road leg this transfer is for: on its date, nearest its pick-up time."""
    if not trip or not found.get("activity_date"):
        return None
    road = [Leg.Mode.CAR, Leg.Mode.SHUTTLE, Leg.Mode.BUS, Leg.Mode.OTHER]
    legs = list(trip.legs.filter(depart_date=found["activity_date"], mode__in=road))
    if not legs:
        return None
    if found.get("start_time"):
        hour, minute = (int(part) for part in found["start_time"].split(":"))
        legs.sort(key=lambda leg: abs((leg.depart_time.hour * 60 + leg.depart_time.minute) - (hour * 60 + minute)) if leg.depart_time else 9999)
    return legs[0]


def _note_lines(found):
    lines = [f"Booked{' with ' + found['site'] if found.get('site') else ''}" + (f": ref {found['booking_ref']}" if found.get("booking_ref") else "")]
    if found.get("travellers"):
        lines.append(f"Travellers: {found['travellers']}")
    if found.get("amount"):
        lines.append(f"Paid: {found['currency']} {found['amount']}".strip())
    return lines


def apply_to_activity(item, found, fields):
    """Copy the chosen details onto an activity: name, meeting point, start time; the booking in its private notes."""
    from datetime import time

    if "title" in fields and found.get("title"):
        item.title = found["title"][:200]
    if "meeting_point" in fields and found.get("meeting_point"):
        item.address = found["meeting_point"][:300]
    if "start_time" in fields and found.get("start_time"):
        hour, minute = found["start_time"].split(":")
        item.start_time = time(int(hour), int(minute))
    for line in _note_lines(found) if "booking_ref" in fields or not found.get("booking_ref") else []:
        if line not in item.private_notes:
            item.private_notes = (item.private_notes + "\n" + line).strip()
    if "travellers" in fields and found.get("travellers"):  # "2 adults, 1 child": who the total paid was for
        count = sum(int(n) for n in re.findall(r"\d+", found["travellers"]))
        if 1 <= count <= 99:
            item.people = count
    item.status = BookingStatus.BOOKED
    item.save()
    return item


def apply_to_transfer(leg, found, fields):
    """Copy the chosen details onto a road leg (and mark it booked)."""
    from datetime import time

    if "booking_ref" in fields and found.get("booking_ref"):
        leg.booking_ref = found["booking_ref"][:60]
    if "start_time" in fields and found.get("start_time"):
        hour, minute = found["start_time"].split(":")
        leg.depart_time = time(int(hour), int(minute))
    if found.get("site") and not leg.operator:
        leg.operator = found["site"]
    for line in _note_lines(found)[1:]:
        if line not in leg.notes:
            leg.notes = (leg.notes + "\n" + line).strip()
    leg.status = Leg.Status.BOOKED
    leg.save()
    return leg


def match_stay(trip, found):
    """The stay this booking belongs to: same check-in date, else overlapping dates in the same city."""
    if not trip or not found.get("check_in_date"):
        return None
    stays = list(trip.stays.select_related("city"))
    same_day = [s for s in stays if s.check_in_date == found["check_in_date"]]
    if len(same_day) == 1:
        return same_day[0]
    address = (found.get("address") or "").lower()
    for stay in same_day or stays:
        overlaps = stay.check_in_date <= found["check_in_date"] < stay.check_out_date
        if overlaps and stay.city and stay.city.name.lower() in address:
            return stay
    return same_day[0] if same_day else None


def guess_trip(found, trips):
    when = found.get("check_in_date") or found.get("activity_date")
    if not when:
        return None
    for trip in trips:
        if trip.start_date <= when <= trip.end_date:
            return trip
    return None


def city_for(found):
    address = (found.get("address") or "").lower()
    for city in Place.objects.filter(kind=Place.Kind.CITY):
        if city.name.lower() in address:
            return city
    return None


def budget_item_for(stay):
    return stay.budget_items.first() if stay else None


def apply_to_stay(stay, found, fields):
    """Copy the chosen fields onto the stay (and mark it booked)."""
    mapping = {
        "booking_ref": "booking_ref", "name": "name", "address": "address", "phone": "phone", "email": "email",
        "check_in_date": "check_in_date", "check_in_time": "check_in_time",
        "check_out_date": "check_out_date", "check_out_time": "check_out_time",
    }
    if "address" in fields and found.get("address") and found["address"] != stay.address:
        metro.reset(stay, address_changed=True)
    for key, attr in mapping.items():
        if key in fields and found.get(key):
            value = found[key]
            if attr.endswith("_time") and isinstance(value, str):
                hour, minute = value.split(":")
                from datetime import time

                value = time(int(hour), int(minute))
            setattr(stay, attr, value)
    extra = []
    if "room" in fields and found.get("room"):
        extra.append(f"Room: {found['room']}")
    if "special_requests" in fields and found.get("special_requests"):
        extra.append(f"Special requests: {found['special_requests']}")
    for line in extra:
        if line not in stay.notes:
            stay.notes = (stay.notes + "\n" + line).strip()
    stay.status = BookingStatus.BOOKED
    stay.save()
    return stay


def apply_to_legs(found, ref_fields):
    updated = []
    for leg in Leg.objects.filter(pk__in=found.get("legs", [])):
        if "booking_ref" in ref_fields and found.get("booking_ref") and not leg.booking_ref:
            leg.booking_ref = found["booking_ref"]
        leg.status = Leg.Status.BOOKED
        leg.save(update_fields=["booking_ref", "status"])
        updated.append(leg)
    return updated
