"""Calendar (.ics) export for Apple, Google and Outlook calendars.

Public: days, legs (without flight/train numbers) and public timed items.
Private (secret subscription link): adds numbers, booking references, stays
and checklist reminders with an alert at 9am on the due date.
"""

from datetime import UTC, datetime, timedelta

from django.utils import timezone

from . import nights
from .models import Place
from .presenters import leg_times, show_leg

DOMAIN = "trips.aarondaws.com.au"


def _escape(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line):
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        encoded = ch.encode("utf-8")
        if len(current) + len(encoded) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(value):
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _event(uid, summary, start, end=None, all_day=False, description="", location="", alarm=None):
    lines = ["BEGIN:VEVENT", f"UID:{uid}@{DOMAIN}", f"DTSTAMP:{_utc(timezone.now())}"]
    if all_day:
        lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{(end or start + timedelta(days=1)):%Y%m%d}"]
    else:
        lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end or start + timedelta(hours=1))}"]
    lines.append(f"SUMMARY:{_escape(summary)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if alarm:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(alarm)}", "TRIGGER:PT9H", "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def build(trip, private=False, full=False):
    """The trip as a calendar: visitors get the public items (flights and trains at rounded times),
    the secret link (full) the private ones too, and the owner's own feed (private) everything."""
    full = full or private
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Aaron Daws Trips//Journey planner//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_escape(trip.title)}",
    ]

    stays = nights.by_night(trip)  # where each night is spent: the city only, whoever's looking
    for day in trip.days.prefetch_related("items"):
        title = f"Day {day.number}: {day.title}" if day.title else f"Day {day.number}"
        details = []
        stay = stays.get(day.date)
        if stay:
            details.append(f"Overnight: {nights.city_of(stay)[0]}")
        for item in day.items.all():
            if full or item.is_public:
                details.append(f"• {item.title}")
        lines += _event(f"day-{day.pk}", f"{trip.title} · {title}", day.date, all_day=True, description="\n".join(details))
        for item in day.items.all():
            if item.start_time and (full or item.is_public):
                near = item.place or (stay and (stay.city or stay.location))  # the time zone only
                tz = near.zone if near else timezone.get_current_timezone()
                start = datetime.combine(day.date, item.start_time, tzinfo=tz)
                end = datetime.combine(day.date, item.end_time, tzinfo=tz) if item.end_time else None
                description = item.description + (f"\n\n{item.private_notes}" if private and item.private_notes else "")
                shown = item.place and (full or item.place.kind != Place.Kind.HOTEL)  # visitors never get the hotel
                lines += _event(f"item-{item.pk}", item.title, start, end, description=description.strip(),
                                location=item.place.name if shown else "")

    for leg in trip.legs.select_related("origin", "destination"):
        if not (full or leg.is_public):
            continue
        show_leg(leg, full)
        summary = f"{leg.mode_label} {leg.origin.city_name} → {leg.destination.city_name}"
        if leg.shown_number:
            summary = f"{leg.shown_number} · {summary}"
        elif leg.operator and leg.rough:
            summary = f"{summary} ({leg.operator})"

        def hub(place):
            return " ".join(filter(None, [place.name, place.name_local, f"({place.pinyin})" if place.pinyin else ""]))

        details = [f"{hub(leg.origin)} → {hub(leg.destination)}"]
        if leg_times(leg):
            details.append(f"Local times: {leg_times(leg)}")
        if leg.public_note:
            details.append(leg.public_note)
        if private:
            details += [f"{label}: {value}" for label, value in (
                ("Operator", leg.operator), ("Class", leg.seat_class), ("Booking", leg.booking_ref), ("Status", leg.get_status_display())
            ) if value]
            if leg.notes:
                details.append(leg.notes)
        if leg.depart_time:
            lines += _event(f"leg-{leg.pk}", summary, leg.shown_departs_at, leg.shown_arrives_at, description="\n".join(details), location=leg.origin.name)
        else:
            lines += _event(f"leg-{leg.pk}", summary, leg.depart_date, all_day=True, description="\n".join(details))

    if private:
        for stay in trip.stays.select_related("city"):
            details = "\n".join(filter(None, [
                stay.address, f"Booking: {stay.booking_ref}" if stay.booking_ref else "",
                f"Check-in {stay.check_in_time:%H:%M}" if stay.check_in_time else "",
                f"Check-out {stay.check_out_time:%H:%M}" if stay.check_out_time else "",
                f"Nearest metro: {stay.nearest_metro}" if stay.nearest_metro else "",
                f"Phone: {stay.phone}" if stay.phone else "", f"Email: {stay.email}" if stay.email else "", stay.url, stay.notes,
            ]))
            lines += _event(f"stay-{stay.pk}", f"Stay: {stay.name or stay.label}", stay.check_in_date, stay.check_out_date,
                            all_day=True, description=details, location=stay.address or (stay.city.name if stay.city else ""))
        for reminder in trip.checklist.filter(done=False, due_date__isnull=False):
            lines += _event(f"todo-{reminder.pk}", f"To do: {reminder.text}", reminder.due_date, all_day=True,
                            description=reminder.notes, alarm=reminder.text)

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
