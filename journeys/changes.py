"""The trip's "Recently updated" list: transport, stays and activities added, changed or removed.

Every save or delete of a leg, stay or activity is compared with what it was before (signals.py), and
a TripChange is kept when something worth telling changed. The list then shows the last 30 days, one
line per plan:

- The owner and the secret link see every change.
- Visitors see only what they could see anyway: changes to public plans, without train or flight
  numbers or hotel names. A plan made public is "added" for them, and one made private "removed".

Each page lists its own kind of plan (the itinerary stays transport only) and links to the others.
Stays read by their label and city everywhere, never the hotel itself.
"""

from datetime import date, timedelta

from django.utils import timezone

from .models import Item, Leg, Stay, TripChange
from .presenters import date_label

WINDOW = timedelta(days=30)
MOST = 30

KINDS = {Leg: TripChange.Kind.LEG, Stay: TripChange.Kind.STAY, Item: TripChange.Kind.ITEM}

# How each changed detail is described ("Changed: times, route").
WORDS = {
    "mode": "type", "from": "route", "to": "route", "operator": "operator", "depart_date": "date",
    "arrive_date": "date", "depart_time": "times", "arrive_time": "times", "status": "status",
    "note": "note", "number": "number", "stations": "stations", "seat_class": "class",
    "label": "name", "kind": "type", "city": "city", "check_in": "dates", "check_out": "dates",
    "times": "check-in times", "property": "property", "address": "address",
    "title": "name", "description": "description", "date": "day", "block": "time", "start_time": "time",
    "end_time": "time", "place": "place", "links": "links", "notes": "notes",
}


def _text(value):
    return value.isoformat() if hasattr(value, "isoformat") else ("" if value is None else str(value))


def state(obj):
    """What a plan looks like now: what visitors see of it, what only the owner sees, and whether it's public."""
    if isinstance(obj, Leg):
        public = {
            "mode": obj.mode_class, "mode_label": obj.mode_label, "from": obj.origin.visitor_city_name,
            "to": obj.destination.visitor_city_name, "operator": obj.operator, "depart_date": obj.depart_date,
            "depart_time": obj.depart_time, "arrive_date": obj.arrive_date, "arrive_time": obj.arrive_time,
            "status": obj.status, "note": obj.public_note,
        }
        private = {"number": obj.number, "stations": f"{obj.origin.name} → {obj.destination.name}", "seat_class": obj.seat_class}
    elif isinstance(obj, Stay):
        public = {
            "label": obj.label, "kind": obj.kind, "city": obj.city.name if obj.city else "",
            "check_in": obj.check_in_date, "check_out": obj.check_out_date,
        }
        private = {"property": obj.name, "address": obj.address, "times": f"{_text(obj.check_in_time)} {_text(obj.check_out_time)}"}
    else:
        public = {
            "title": obj.title, "description": obj.description, "date": obj.day.date, "block": obj.block,
            "start_time": obj.start_time, "end_time": obj.end_time,
            "place": obj.place.name if obj.place else "", "address": obj.address, "links": obj.links,
        }
        private = {"notes": obj.private_notes}
    return {
        "public": {key: _text(value) for key, value in public.items()},
        "private": {key: _text(value) for key, value in private.items()},
        "is_public": obj.is_public,
    }


def _trip_id(obj):
    return obj.day.trip_id if isinstance(obj, Item) else obj.trip_id


def record(obj, action, before=None):
    """Keep a TripChange for this plan. For a change, `before` is its state() beforehand; nothing is
    kept if nothing in the list's terms changed (a background job filling in a route, say)."""
    if action == TripChange.Action.REMOVED:
        before, after = state(obj), None
    else:
        after = state(obj)
    changed = []
    if action == TripChange.Action.CHANGED:
        for part in ("public", "private"):
            changed += [key for key, value in after[part].items() if before[part].get(key) != value and key in WORDS]
        if before["is_public"] != after["is_public"]:
            changed.append("is_public")
        if not changed:
            return None
    shown = after or before
    data = {"public": shown["public"], "private": shown["private"]}
    if action == TripChange.Action.CHANGED:
        data["was"] = before["public"]  # what visitors saw until now, should this make it private
    return TripChange.objects.create(
        trip_id=_trip_id(obj), kind=KINDS[type(obj)], object_id=obj.pk, action=action, data=data, changed=changed,
        public_before=bool(before and before["is_public"]), public_after=bool(after and after["is_public"]),
    )


def record_updates(queryset, **values):
    """queryset.update(**values), keeping a TripChange for each plan it changes."""
    objects = list(queryset)
    before = {obj.pk: state(obj) for obj in objects}
    queryset.model.objects.filter(pk__in=before).update(**values)
    for obj in queryset.model.objects.filter(pk__in=before):
        record(obj, TripChange.Action.CHANGED, before=before[obj.pk])


def _seen(group, full):
    """Whether this viewer could see the plan before the first change and after the last, and the
    details they saw change in between."""
    first, last = group[0], group[-1]
    before = first.action != TripChange.Action.ADDED and (full or first.public_before)
    after = last.action != TripChange.Action.REMOVED and (full or last.public_after)
    details = []
    for change in group:
        if change.action != TripChange.Action.CHANGED or not (full or (change.public_before and change.public_after)):
            continue
        public_keys = change.data["public"].keys()
        for key in change.changed:
            if key == "is_public" or not (full or key in public_keys):
                continue
            if WORDS[key] not in details:
                details.append(WORDS[key])
    return before, after, details


def _last_public(group):
    """What visitors last saw of a plan they can no longer see (not any edit made with hiding it)."""
    seen = None
    for change in group:
        if change.public_before:
            seen = change.data["was"] if change.action == TripChange.Action.CHANGED else change.data["public"]
        if change.public_after:
            seen = change.data["public"]
    return seen


def _describe(trip, change, full, urls, public=None):
    """How one line of the list reads, from the plan's latest snapshot (or `public`, what visitors saw)."""
    public, private = public or change.data["public"], change.data["private"]
    if change.kind == TripChange.Kind.LEG:
        label = public["mode_label"] + (f" {private['number']}" if full and private.get("number") else "")
        title = public["from"] if public["from"] == public["to"] else f"{public['from']} → {public['to']}"
        on, mode, link = public["depart_date"], public["mode"], f"{urls['trip']}#journey-{change.object_id}"
    elif change.kind == TripChange.Kind.STAY:
        label = "Stay"
        title = public["label"] + (f" · {public['city']}" if public["city"] and public["city"] not in public["label"] else "")
        on, mode, link = public["check_in"], "stay", f"{urls['stays']}#stay-{change.object_id}"
    else:
        label, title = "Activity", public["title"]
        on, mode, link = public["date"], "activity", f"{urls['activities']}#activity-{change.object_id}"
    return {"label": label, "title": title, "mode": mode, "link": link,
            "date": date_label(trip, date.fromisoformat(on), full, "%a %-d %b") if on else ""}


def recent(trip, full, urls):
    """The last 30 days of changes this viewer may see, newest first, one line per plan (all kinds)."""
    groups = {}
    for change in trip.changes.filter(created_at__gte=timezone.now() - WINDOW):
        groups.setdefault((change.kind, change.object_id), []).append(change)
    lines = []
    for group in groups.values():
        before, after, details = _seen(group, full)
        last = group[-1]
        if after and not before:
            action, details = "Added", []  # new to this viewer (for a visitor, that includes made public)
        elif before and not after:
            action, details = "Removed", []
        elif before and after:
            if details:
                action = "Changed"
                if full and group[0].public_before != last.public_after:
                    details.append("now public" if last.public_after else "now private")
            elif full and group[0].public_before != last.public_after:
                action = "Made public" if last.public_after else "Made private"
            else:
                continue
        else:
            continue
        line = _describe(trip, last, full, urls, public=None if full or action != "Removed" else _last_public(group))
        if action == "Removed":
            line["link"] = None
        line.update(action=action, details=details, when=last.created_at, kind=last.kind)
        lines.append(line)
    lines.sort(key=lambda line: line["when"], reverse=True)
    return lines


PAGES = {"itinerary": TripChange.Kind.LEG, "stays": TripChange.Kind.STAY, "activities": TripChange.Kind.ITEM}
NOUNS = {TripChange.Kind.LEG: ("transport", "trip"), TripChange.Kind.STAY: ("stay", "stays"), TripChange.Kind.ITEM: ("activity", "activities")}


def for_page(trip, full, urls, tab):
    """The list for one trip page: this page's kind of plan, and how many changes the other pages have."""
    lines = recent(trip, full, urls)
    kind = PAGES[tab]
    others = []
    for other in TripChange.Kind.values:
        count = sum(1 for line in lines if line["kind"] == other)
        if other != kind and count:
            noun, page = NOUNS[other]
            others.append({"text": f"{count} {noun} update{'s' if count != 1 else ''}", "url": urls[page]})
    return {"lines": [line for line in lines if line["kind"] == kind][:MOST], "total": len(lines),
            "others": others, "noun": NOUNS[kind][0]}
