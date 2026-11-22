"""Planner dashboard: everything behind the login (and Cloudflare Access) at /manage/."""

import functools
import hashlib
import json
import math
import mimetypes
import re
import secrets
import urllib.parse
from collections import defaultdict
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.db.models import Count
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

from . import activity_import, booking_costs, booking_import, changes, claude_read, guides, link_details, trip_check, china_coords, flights, geocode, leg_import, metro, money, nights, presenters, routing, site_links, stay_import, transit
from .forms import (
    BookingImportForm,
    ConfirmationForm,
    BudgetItemForm,
    ChecklistItemForm,
    DayForm,
    DocumentForm,
    ItemForm,
    LegForm,
    PlaceGuideForm,
    StayForm,
    TravellerFormSet,
    TripForm,
)
from .models import AddedFrom, Airline, BookingImport, BookingStatus, BudgetItem, ChecklistItem, Day, Document, ExchangeRate, Item, Leg, Place, PlaceGuide, Stay, Trip, normalise
from .search import MODE_KINDS, find_places, match_airport, match_station, parse_leg, place_json

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff, login_url="manage:login")


# ---------- helpers ----------


def _trip(pk):
    return get_object_or_404(Trip, pk=pk)


def _context(trip, tab, **extra):
    return {"trip": trip, "tab": tab, **extra}


def _unique_slug(title, exclude_pk=None):
    base = slugify(title)[:90] or "trip"
    slug, n = base, 2
    while Trip.objects.filter(slug=slug).exclude(pk=exclude_pk).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _sync_days(trip):
    """Make sure there's a Day for every date of the trip; drop empty days outside it."""
    existing = set(trip.days.values_list("date", flat=True))
    new, current = [], trip.start_date
    while current <= trip.end_date:
        if current not in existing:
            new.append(Day(trip=trip, date=current))
        current += timedelta(days=1)
    Day.objects.bulk_create(new)
    outside = trip.days.exclude(date__range=(trip.start_date, trip.end_date)).annotate(n=Count("items"))
    kept = [day for day in outside if day.n]
    outside.filter(n=0).delete()
    return len(new), kept


def _share_links(request, trip):
    absolute = request.build_absolute_uri
    return {
        "public": absolute(reverse("journeys:trip", args=[trip.slug])) if trip.visibility == Trip.Visibility.PUBLIC else None,
        "unlisted": absolute(reverse("journeys:shared_trip", args=[trip.share_token])) if trip.is_shared else None,
        "calendar": absolute(reverse("journeys:private_calendar", args=[trip.calendar_token])),
    }


def _budget_totals(trip):
    """The budget page's figures. Spent is what's been paid; the estimate is that plus the estimated
    costs of stays, transport and activities that are still planned (booking_costs.estimate_for)."""
    items = list(trip.budget_items.select_related("paid_by", "stay", "leg", "item__day"))
    travellers = list(trip.travellers.all())
    for line in items:
        line.plan = booking_costs.plan_of(line)
        line.estimate_value, line.is_planned_estimate = booking_costs.estimate_for(line)
        line.cost_unrecorded = booking_costs.cost_unrecorded(line)  # booked, but what it cost isn't on it yet
        plan = line.plan
        line.when = (plan.check_in_date if isinstance(plan, Stay) else plan.depart_date if isinstance(plan, Leg)
                     else plan.day.date if isinstance(plan, Item) else None)
    actual = sum((i.actual_aud or Decimal(0) for i in items), Decimal(0))
    estimate = sum((i.estimate_value for i in items), Decimal(0))
    groups = defaultdict(list)
    for item in items:
        groups[item.category].append(item)
    categories = [
        {
            "label": BudgetItem.Category(key).label,
            # In date order (lines without a plan last, as entered), so planned items sit among what's booked around them.
            "items": sorted(rows, key=lambda r: (r.when is None, r.when or date.min, r.order, r.pk)),
            "estimate": sum((r.estimate_value for r in rows), Decimal(0)),
            "actual": sum((r.actual_aud or Decimal(0) for r in rows), Decimal(0)),
        }
        for key, rows in sorted(groups.items(), key=lambda kv: list(BudgetItem.Category.values).index(kv[0]))
    ]
    return {
        "categories": categories,
        "estimate": estimate,
        "actual": actual,
        "planned": sum((i.estimate_value for i in items if i.is_planned_estimate), Decimal(0)),
        "count": len(items),
        "per_person": (actual / len(travellers)).quantize(Decimal("0.01")) if travellers else None,
        "per_person_estimate": (estimate / len(travellers)).quantize(Decimal("0.01")) if travellers else None,
        "paid_by": [
            (t, sum((i.actual_aud or Decimal(0) for i in items if i.paid_by_id == t.pk), Decimal(0))) for t in travellers
        ],
        "missing_rates": [i for i in items if (i.actual is not None and i.actual_aud is None)
                          or (i.is_planned_estimate and i.estimate is not None and i.estimate_aud is None)],
        "travellers": travellers,
    }


def _plan_extra(plan):
    """For an existing stay, transport or activity's edit page: how it was added, what's missing, and
    whether Refresh details can read its link again."""
    if not plan:
        return {}
    kind = "stay" if isinstance(plan, Stay) else "leg" if isinstance(plan, Leg) else "item"
    return {"plan": plan, "plan_kind": kind, "missing": booking_costs.missing_details(plan),
            "plan_link": booking_costs.plan_link(plan) if plan.status == BookingStatus.PLANNED else ""}


def _record_booking_cost(request, form, plan, created=False):
    """After a stay, activity or transport form is saved: its one Cost onto its budget line (an estimate
    while planned, what it cost once booked), and for a new one, how it was added."""
    note = booking_costs.save_cost(plan, form.cost_changed(), status_changed="status" in form.changed_data)
    if note:
        messages.info(request, note)
    if created:
        filled_from = form.cleaned_data.get("filled_from", "").strip()
        plan.added_from = AddedFrom.LINK if filled_from else AddedFrom.HAND
        plan.added_from_url = filled_from[:500] if filled_from.startswith(("http://", "https://")) else ""
        plan.save(update_fields=["added_from", "added_from_url"])
    # A planned one with a link: read the page now (when it's new or its link changed) to fill its gaps.
    if plan.status == BookingStatus.PLANNED and booking_costs.plan_link(plan) and (created or {"url", "links"} & set(form.changed_data)):
        _report_refresh(request, *link_details.refresh(plan), note=plan.details_note)


def _report_refresh(request, filled, missing, error, note=""):
    if error:
        messages.warning(request, f"Couldn't read its link: {error} Press Refresh details on it to try again.")
        return
    found = f"Filled in from its link: {', '.join(filled)}." if filled else "Its link had nothing new to fill in."
    messages.info(request, found + (f" Still missing: {', '.join(missing)}." if missing else "") + (f" {note}" if note else ""))


@staff_required
@require_POST
def refresh_details(request, pk, kind, plan_pk):
    """Refresh details, in a planned stay, transport or activity's edit area: read its link again and fill its gaps."""
    trip = _trip(pk)
    if kind not in CONFIRM_KINDS:
        raise Http404
    model, trip_field, page = CONFIRM_KINDS[kind]
    plan = get_object_or_404(model, pk=plan_pk, **{trip_field: trip})
    _report_refresh(request, *link_details.refresh(plan), note=plan.details_note)
    edit = {"stay": "manage:stay_edit", "leg": "manage:leg_edit", "item": "manage:item_edit"}[kind]
    return redirect(reverse(edit, args=[trip.pk, plan.pk]))


def _form_page(request, trip, tab, form, title, back, delete_url=None, extra=None, template="manage/form.html"):
    return render(request, template, _context(trip, tab, form=form, title=title, back=back, delete_url=delete_url, **(extra or {})))


def _next_url(request, default):
    """Where to go after a form: the 'next' parameter if it points within this site, else the default."""
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return default


def _parse_day(day_date):
    try:
        return date.fromisoformat(day_date)
    except ValueError:
        raise Http404


# ---------- dashboard and trips ----------


@staff_required
def dashboard(request):
    today = timezone.localdate()
    trips = Trip.objects.annotate(n_legs=Count("legs", distinct=True)).order_by("-start_date")
    inbox_count = BookingImport.objects.filter(status=BookingImport.Status.NEW).count()
    return render(request, "manage/dashboard.html", {"trips": trips, "today": today, "inbox_count": inbox_count})


@staff_required
def trip_create(request):
    form = TripForm(request.POST or None)
    formset = TravellerFormSet(request.POST or None, prefix="travellers")
    form.fields["slug"].required = False
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            trip = form.save(commit=False)
            trip.slug = form.cleaned_data["slug"] or _unique_slug(trip.title)
            trip.save()
            formset.instance = trip
            formset.save()
            _sync_days(trip)
        messages.success(request, f"Created '{trip}'. It's private until you change its visibility.")
        return redirect(trip.get_manage_url())
    return render(request, "manage/trip_form.html", {"form": form, "formset": formset, "creating": True})


@staff_required
def trip_settings(request, pk):
    trip = _trip(pk)
    form = TripForm(request.POST or None, instance=trip)
    formset = TravellerFormSet(request.POST or None, instance=trip, prefix="travellers")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            trip = form.save()
            formset.save()
            added, kept = _sync_days(trip)
        messages.success(request, "Trip settings saved." + (f" Added {added} new day(s)." if added else ""))
        if kept:
            messages.warning(request, f"{len(kept)} day(s) outside the new dates still have plans: " + ", ".join(f"{d.date:%-d %b}" for d in kept))
        return redirect("manage:trip_settings", trip.pk)
    return render(request, "manage/trip_form.html", _context(trip, "settings", form=form, formset=formset, share=_share_links(request, trip)))


@staff_required
@require_POST
def trip_new_link(request, pk, which):
    trip = _trip(pk)
    if which == "share":
        trip.share_token = secrets.token_urlsafe(18)
        messages.success(request, "Made a new secret link. The old one no longer works.")
    elif which == "calendar":
        trip.calendar_token = secrets.token_urlsafe(18)
        messages.success(request, "Made a new calendar link. Re-subscribe on your devices.")
    else:
        raise Http404
    trip.save(update_fields=["share_token", "calendar_token", "updated_at"])
    return redirect("manage:trip_settings", trip.pk)


@staff_required
def trip_delete(request, pk):
    trip = _trip(pk)
    if request.method == "POST":
        title = trip.title
        trip.delete()
        messages.success(request, f"Deleted '{title}'.")
        return redirect("manage:dashboard")
    return render(request, "manage/confirm_delete.html", _context(trip, "settings", thing=f"the whole trip '{trip}'", back=reverse("manage:trip_settings", args=[trip.pk])))


@staff_required
def trip_overview(request, pk):
    trip = _trip(pk)
    today = timezone.localdate()
    context = _context(
        trip, "overview",
        days=presenters.timeline(trip, private=True),  # transport only: activities have their own tab
        summary=presenters.summary(trip),
        budget=_budget_totals(trip),
        pending_routes=trip.legs.filter(path_status=Leg.PathStatus.PENDING).count(),
        share=_share_links(request, trip),
        today=today,
    )
    return render(request, "manage/trip.html", context)


@staff_required
def trip_map(request, pk):
    return JsonResponse(presenters.map_data(_trip(pk), private=True))


# ---------- days and itinerary items ----------


@staff_required
def day_edit(request, pk, day_date):
    trip = _trip(pk)
    day = get_object_or_404(Day, trip=trip, date=_parse_day(day_date))
    form = DayForm(request.POST or None, instance=day)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {day.date:%a %-d %b}.")
        return redirect("manage:day", trip.pk, day.date.isoformat())
    timeline = [d for d in presenters.timeline(trip, private=True, activities=True) if d["day"].pk == day.pk]
    previous = trip.days.filter(date__lt=day.date).order_by("-date").first()
    following = trip.days.filter(date__gt=day.date).order_by("date").first()
    return render(request, "manage/day.html", _context(trip, "overview", day=day, form=form, days=timeline, previous=previous, following=following))


@staff_required
def item_form(request, pk, item_pk=None):
    trip = _trip(pk)
    item = get_object_or_404(Item, pk=item_pk, day__trip=trip) if item_pk else None
    initial = {}  # a new activity starts as Planned, so its cost is an estimate
    if not item and request.GET.get("date"):
        initial["day"] = trip.days.filter(date=_parse_day(request.GET["date"])).first()
    form = ItemForm(request.POST or None, instance=item, initial=initial, trip=trip)
    if request.method == "POST" and form.is_valid():
        created = item is None
        item = form.save(commit=False)
        item.directions = {"pending": True}  # the background job works out how to get there
        item.save()
        _record_booking_cost(request, form, item, created)
        messages.success(request, f"Saved '{item.title}'.")
        return redirect(_next_url(request, reverse("manage:day", args=[trip.pk, item.day.date.isoformat()])))
    back = _next_url(request, reverse("manage:day", args=[trip.pk, (item.day.date if item else initial.get("day").date if initial.get("day") else trip.start_date).isoformat()]))
    delete_url = reverse("manage:item_delete", args=[trip.pk, item.pk]) if item else None
    return _form_page(request, trip, "activities", form, "Edit activity" if item else "Add an activity", back, delete_url,
                      extra={"activity_import": True, "creating": item is None, **_plan_extra(item)})


@staff_required
def item_delete(request, pk, item_pk):
    trip = _trip(pk)
    item = get_object_or_404(Item, pk=item_pk, day__trip=trip)
    back = reverse("manage:day", args=[trip.pk, item.day.date.isoformat()])
    if request.method == "POST":
        item.delete()
        messages.success(request, "Activity deleted.")
        return redirect(back)
    return render(request, "manage/confirm_delete.html", _context(trip, "overview", thing=f"'{item.title}'", back=back))


@staff_required
def activities(request, pk):
    """Every sight and activity in the trip, by day, with how to get there from that night's hotel."""
    trip = _trip(pk)
    items = metro.for_activities(trip, Item.objects.filter(day__trip=trip).select_related("day", "place").prefetch_related("budget_items")
                                 .order_by("day__date", "order", "start_time", "id"))
    night_names = nights.night_names(trip)  # each night's city, from the stays
    travellers = trip.travellers.count()
    days = []
    for item in items:
        item.missing = booking_costs.missing_details(item)
        item.cost_view = booking_costs.activity_cost(item, travellers)  # cost per person × people = total
        if not days or days[-1]["day"] != item.day:
            days.append({"day": item.day, "night": night_names.get(item.day.date), "items": []})
        days[-1]["items"].append(item)
    trip_days = list(trip.days.all())
    for day in trip_days:
        day.night = night_names.get(day.date)
    return render(request, "manage/activities.html", _context(trip, "activities", days=days, trip_days=trip_days,
                                                              default_people=booking_costs.default_people(trip, travellers)))


@staff_required
@require_POST
def activities_bulk(request, pk):
    """Act on the activities ticked on the Activities page: make them public or private, move them
    to another day, or delete them (after a page listing exactly what will go)."""
    trip = _trip(pk)
    back = reverse("manage:activities", args=[trip.pk])
    ids = [value for value in request.POST.getlist("items") if value.isdigit()]
    items = Item.objects.filter(day__trip=trip, pk__in=ids).select_related("day")
    count = items.count()
    if not count:
        messages.info(request, "Tick at least one activity first.")
        return redirect(back)
    noun = f"{count} activit{'y' if count == 1 else 'ies'}"
    action = request.POST.get("action")
    if action in ("public", "private"):
        changes.record_updates(items, is_public=action == "public")
        messages.success(request, f"Made {noun} {action}.")
    elif action == "move":
        day_id = request.POST.get("day", "")
        day = trip.days.filter(pk=day_id).first() if day_id.isdigit() else None
        if day is None:
            messages.info(request, "Pick the day to move them to.")
            return redirect(back)
        changes.record_updates(items, day=day, directions={"pending": True})  # directions set out from that night's hotel
        messages.success(request, f"Moved {noun} to {day.date:%a %-d %b}.")
    elif action == "people":
        people = request.POST.get("people", "")
        if not people.isdigit() or not 1 <= int(people) <= 99:
            messages.info(request, "Enter how many people (1 to 99).")
            return redirect(back)
        travellers = trip.travellers.count()
        for item in items:
            booking_costs.set_people(item, int(people), travellers)
        messages.success(request, f"Set {noun} to {people} {'person' if people == '1' else 'people'}: each cost per person is kept, "
                                  "and the totals on the budget now match.")
    elif action == "delete":
        if request.POST.get("confirm") != "yes":
            rows = [{"pk": item.pk, "title": item.title, "detail": f"{item.day.date:%a %-d %b} · Day {item.day.number}"} for item in items]
            return render(request, "manage/confirm_bulk_delete.html", _context(
                trip, "activities", rows=rows, field="items", noun="activity" if count == 1 else "activities", back=back))
        items.delete()
        messages.success(request, f"Deleted {noun}.")
    else:
        messages.info(request, "Choose what to do with them.")
    return redirect(back)


@staff_required
@require_POST
def item_toggle(request, pk, item_pk):
    """Make an activity public or private."""
    trip = _trip(pk)
    item = get_object_or_404(Item, pk=item_pk, day__trip=trip)
    changes.record_updates(Item.objects.filter(pk=item.pk), is_public=not item.is_public)
    return redirect(_next_url(request, reverse("manage:activities", args=[trip.pk])))


def _import_review_other(request, record, trips, trip, found, kind):
    """Reviewing an activity or transfer booking (Klook, Viator): add it to an activity or road leg."""
    target = (booking_import.match_activity if kind == "activity" else booking_import.match_transfer)(trip, found)
    if trip and kind == "activity":
        candidates = Item.objects.filter(day__trip=trip).select_related("day")
        candidates = candidates.filter(day__date=found["activity_date"]) if found["activity_date"] else candidates[:40]
    elif trip:
        candidates = trip.legs.filter(mode__in=[Leg.Mode.CAR, Leg.Mode.SHUTTLE, Leg.Mode.BUS, Leg.Mode.OTHER]).select_related("origin", "destination")
        candidates = candidates.filter(depart_date=found["activity_date"]) if found["activity_date"] else candidates[:40]
    else:
        candidates = []

    if request.method == "POST" and request.POST.get("action") == "retrip":
        record.trip = trip
        record.save(update_fields=["trip"])
        return redirect("manage:import_review", record.pk)
    if request.method == "POST" and request.POST.get("action") == "dismiss":
        record.status = BookingImport.Status.DISMISSED
        record.save(update_fields=["status"])
        messages.info(request, "Dismissed.")
        return redirect("manage:imports")
    if request.method == "POST" and request.POST.get("action") == "apply":
        if not trip:
            messages.error(request, "Choose which trip this booking belongs to.")
            return redirect("manage:import_review", record.pk)
        fields, choice = set(request.POST.getlist("fields")), request.POST.get("target", "")
        day = trip.days.filter(date=found["activity_date"]).first() if found["activity_date"] else None
        with transaction.atomic():
            chosen = None
            if kind == "activity":
                if choice == "new":
                    if not day:
                        messages.error(request, "The booking's date isn't one of this trip's days, so a new activity can't be added. Add it under Activities instead.")
                        return redirect("manage:import_review", record.pk)
                    chosen = Item(day=day, title=(found["title"] or found["name"] or "Booked activity")[:200], added_from=AddedFrom.IMPORT)
                    fields |= {"title", "meeting_point", "start_time", "name_local", "address_local"}
                elif choice.isdigit():
                    chosen = get_object_or_404(Item, pk=choice, day__trip=trip)
                if chosen:
                    booking_import.apply_to_activity(chosen, found, fields)
                    _attach_confirmation(record, chosen)
                    if request.POST.get("budget") and found["amount"]:
                        booking_costs.record(chosen, found["amount"], found["currency"] or "AUD", site=found["site"])
            else:
                if choice == "new":
                    origin, destination = _find_place(trip, found["pickup"]), _find_place(trip, found["dropoff"])
                    if not (day and origin and destination):
                        messages.error(request, "Couldn't place the pick-up and drop-off (or the date isn't in this trip). Add it under Transport instead.")
                        return redirect("manage:import_review", record.pk)
                    chosen = Leg(trip=trip, mode=leg_import.mode_for(found["title"]) or Leg.Mode.CAR, origin=origin, destination=destination,
                                 depart_date=found["activity_date"], added_from=AddedFrom.IMPORT)
                    fields |= {"booking_ref", "start_time"}
                elif choice.isdigit():
                    chosen = get_object_or_404(Leg, pk=choice, trip=trip)
                if chosen:
                    booking_import.apply_to_transfer(chosen, found, fields)
                    _attach_confirmation(record, chosen)
                    routing.refresh_path(chosen, allow_network=False)
                    if request.POST.get("budget") and found["amount"]:
                        booking_costs.record(chosen, found["amount"], found["currency"] or "AUD", site=found["site"])
            record.status, record.trip, record.applied_at = BookingImport.Status.APPLIED, trip, timezone.now()
            record.save()
        messages.success(request, f"Added to the trip: {chosen}." if chosen else "Marked as done; nothing was changed.")
        return redirect("manage:activities" if kind == "activity" else "manage:legs", trip.pk)

    def show(value):
        if hasattr(value, "strftime"):
            return value.strftime("%a %-d %b %Y") if hasattr(value, "year") else value.strftime("%H:%M")
        return value or ""

    rows = []
    for key, label in ACTIVITY_FIELDS if kind == "activity" else TRANSFER_FIELDS:
        attr = CURRENT[kind].get(key)
        rows.append({"key": key, "label": label, "value": show(found.get(key)), "current": show(getattr(target, attr, None)) if target and attr else ""})
    return render(request, "manage/import_review.html", {
        "record": record, "found": found, "rows": rows, "chosen_trip": trip, "trips": trips, "kind": kind,
        "target": target, "candidates": candidates, "legs": [],
    })


def _link_request(request):
    """(link, pasted text, the body) from a Fill in request; the body's "capture" is a page sent from
    the browser by the Send to Trips bookmarklet (see capture())."""
    try:
        data = json.loads(request.body)
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    if not isinstance(data.get("capture"), dict):
        data["capture"] = None
    return str(data.get("url", "")).strip(), str(data.get("text", "")), data


@staff_required
@require_POST
def api_activity_extract(request, pk):
    """A sight's details (name, map position, price, hours, contact) from a link or pasted page text."""
    trip = _trip(pk)
    url, text, data = _link_request(request)
    if not (url or text.strip() or data["capture"]):
        return JsonResponse({"error": "Paste a link or the page text first."}, status=400)
    day = trip.days.filter(pk=data.get("day")).first() if str(data.get("day", "")).isdigit() else None
    title = str(data.get("title", ""))[:200]  # as typed: stands in when the site won't show the page
    try:
        result = link_details.read_activity(trip, url, text, day, title=title, capture=data["capture"])
    except stay_import.PageError as exc:
        return JsonResponse({"error": str(exc)}, status=422)
    place = result["place"]
    return JsonResponse({"fields": result["fields"], "place": place_json(place) if place else None, "missing": result["missing"],
                         "note": result.get("note", "")})


@staff_required
@require_POST
def api_leg_extract(request, pk):
    """A bus, coach, shuttle or car trip's route and details from a booking link (or pasted page text)."""
    trip = _trip(pk)
    url, text, data = _link_request(request)
    if not (url or text.strip() or data["capture"]):
        return JsonResponse({"error": "Paste a link or the page text first."}, status=400)
    try:
        result = link_details.read_leg(trip, url, text, capture=data["capture"])
    except stay_import.PageError as exc:
        return JsonResponse({"error": str(exc)}, status=422)
    return JsonResponse({"fields": result["fields"], "missing": result["missing"],
                         "origin": place_json(result["origin"]) if result["origin"] else None,
                         "destination": place_json(result["destination"]) if result["destination"] else None})


_find_place = link_details.find_place  # the booking imports place pick-ups and drop-offs the same way


# ---------- transport legs ----------


@staff_required
def legs(request, pk):
    trip = _trip(pk)
    legs = presenters.legs_for(trip)
    for leg in legs:
        leg.missing = booking_costs.missing_details(leg)
    return render(request, "manage/legs.html", _context(trip, "transport", legs=legs))


@staff_required
def leg_form(request, pk, leg_pk=None):
    trip = _trip(pk)
    leg = get_object_or_404(Leg, pk=leg_pk, trip=trip) if leg_pk else None
    before = leg.route_key() if leg else None
    before_dates = (leg.depart_date, leg.arrive_date) if leg else ()
    initial = {}
    if not leg:
        initial["depart_date"] = _parse_day(request.GET["date"]) if request.GET.get("date") else trip.start_date
        if request.GET.get("mode") in Leg.Mode.values:
            initial["mode"] = request.GET["mode"]
    form = LegForm(request.POST or None, instance=leg, initial=initial, trip=trip)
    if request.method == "POST" and form.is_valid():
        leg = form.save(commit=False)
        leg.trip = trip
        if not leg.operator and leg.number:
            if leg.mode == Leg.Mode.FLIGHT:
                airline = Airline.objects.filter(code=leg.number[:2].upper()).first()
                leg.operator = airline.name if airline else ""
            elif leg.mode == Leg.Mode.TRAIN and leg.origin.country in ("CN", "HK"):
                leg.operator = "China Railway"
        if leg.mode in routing.TIMETABLE_MODES:
            transit.fill_leg(leg)  # Brisbane, Melbourne and Sydney: line operator and arrival time
        leg.save()
        _record_booking_cost(request, form, leg, created=before is None)
        if leg.route_key() != before or not leg.path:
            routing.refresh_path(leg, allow_network=False)
        if leg.route_key() != before or before_dates != (leg.depart_date, leg.arrive_date):
            metro.legs_changed(trip, *before_dates, leg.depart_date, leg.arrive_date)  # metro or taxi to the hotels
        if leg.path_status == Leg.PathStatus.PENDING:
            messages.info(request, "Saved. The route along the track or road is being traced and will appear on the map within a few minutes.")
        else:
            messages.success(request, f"Saved {leg}.")
        return redirect(_next_url(request, reverse("manage:legs", args=[trip.pk])))
    back = _next_url(request, reverse("manage:legs", args=[trip.pk]))
    delete_url = reverse("manage:leg_delete", args=[trip.pk, leg.pk]) if leg else None
    return _form_page(request, trip, "transport", form, f"Edit {leg}" if leg else "Add transport", back, delete_url,
                      extra={"quick_entry": True, "mode_kinds": json.dumps({k: v for k, v in MODE_KINDS.items()}), "leg": leg,
                             "live_flights": flights.enabled(), "leg_import": True, "creating": leg is None, **_plan_extra(leg)})


@staff_required
@require_POST
def leg_toggle(request, pk, leg_pk):
    """Make a transport leg public or private."""
    trip = _trip(pk)
    leg = get_object_or_404(Leg, pk=leg_pk, trip=trip)
    changes.record_updates(Leg.objects.filter(pk=leg.pk), is_public=not leg.is_public)
    return redirect(_next_url(request, reverse("manage:legs", args=[trip.pk])))


@staff_required
def leg_delete(request, pk, leg_pk):
    trip = _trip(pk)
    leg = get_object_or_404(Leg, pk=leg_pk, trip=trip)
    back = reverse("manage:legs", args=[trip.pk])
    if request.method == "POST":
        leg.delete()
        metro.legs_changed(trip, leg.depart_date, leg.arrive_date)
        messages.success(request, "Transport deleted.")
        return redirect(back)
    return render(request, "manage/confirm_delete.html", _context(trip, "transport", thing=str(leg), back=back))


@staff_required
@require_POST
def leg_retrace(request, pk, leg_pk):
    trip = _trip(pk)
    leg = get_object_or_404(Leg, pk=leg_pk, trip=trip)
    routing.refresh_path(leg, allow_network=False)
    if leg.path_status == Leg.PathStatus.PENDING:
        messages.info(request, f"Queued {leg} to be traced again; it'll update within a few minutes.")
    return redirect(_next_url(request, reverse("manage:legs", args=[trip.pk])))


# ---------- stays ----------


@staff_required
def stays(request, pk):
    trip = _trip(pk)
    stays = presenters.stay_extras(trip, metro.for_stays(trip, trip.stays.select_related("city", "location").prefetch_related("budget_items")), full=True)
    for stay in stays:
        stay.missing = booking_costs.missing_details(stay)
    return render(request, "manage/stays.html", _context(trip, "stays", stays=stays))


@staff_required
def stay_form(request, pk, stay_pk=None):
    trip = _trip(pk)
    stay = get_object_or_404(Stay, pk=stay_pk, trip=trip) if stay_pk else None
    form = StayForm(request.POST or None, instance=stay, initial={} if stay else {"check_in_date": trip.start_date, "check_out_date": trip.start_date + timedelta(days=1)})
    if request.method == "POST" and form.is_valid():
        created = stay is None
        stay = form.save(commit=False)
        stay.trip = trip
        metro.reset(stay, address_changed="address" in form.changed_data, location_changed="location" in form.changed_data)
        stay.save()
        _record_booking_cost(request, form, stay, created)
        messages.success(request, f"Saved {stay}.")
        return redirect("manage:stays", trip.pk)
    delete_url = reverse("manage:stay_delete", args=[trip.pk, stay.pk]) if stay else None
    return _form_page(request, trip, "stays", form, f"Edit {stay}" if stay else "Add a stay", reverse("manage:stays", args=[trip.pk]), delete_url,
                      extra={"stay_import": True, "creating": stay is None, **_plan_extra(stay)})


@staff_required
@require_POST
def stay_toggle(request, pk, stay_pk):
    """Make a stay public or private."""
    trip = _trip(pk)
    stay = get_object_or_404(Stay, pk=stay_pk, trip=trip)
    changes.record_updates(Stay.objects.filter(pk=stay.pk), is_public=not stay.is_public)
    return redirect(_next_url(request, reverse("manage:stays", args=[trip.pk])))


@staff_required
def stay_delete(request, pk, stay_pk):
    trip = _trip(pk)
    stay = get_object_or_404(Stay, pk=stay_pk, trip=trip)
    back = reverse("manage:stays", args=[trip.pk])
    if request.method == "POST":
        stay.delete()
        messages.success(request, "Stay deleted.")
        return redirect(back)
    return render(request, "manage/confirm_delete.html", _context(trip, "stays", thing=str(stay), back=back))


@staff_required
@require_POST
def stays_bulk(request, pk):
    """Act on the stays ticked on the Stays page: make them public or private, or delete them
    (after a page listing exactly what will go)."""
    trip = _trip(pk)
    back = reverse("manage:stays", args=[trip.pk])
    ids = [value for value in request.POST.getlist("stays") if value.isdigit()]
    stays = trip.stays.filter(pk__in=ids).select_related("city").order_by("check_in_date", "id")
    count = stays.count()
    if not count:
        messages.info(request, "Tick at least one stay first.")
        return redirect(back)
    noun = f"{count} stay{'s' if count != 1 else ''}"
    action = request.POST.get("action")
    if action in ("public", "private"):
        changes.record_updates(stays, is_public=action == "public")
        messages.success(request, f"Made {noun} {action}.")
    elif action == "delete":
        if request.POST.get("confirm") != "yes":
            rows = [{"pk": stay.pk, "title": stay.label, "detail": f"{stay.check_in_date:%a %-d %b} – {stay.check_out_date:%a %-d %b}"
                     + (f" · {stay.name}" if stay.name else "")} for stay in stays]
            return render(request, "manage/confirm_bulk_delete.html", _context(
                trip, "stays", rows=rows, field="stays", noun="stay" if count == 1 else "stays", back=back))
        stays.delete()
        messages.success(request, f"Deleted {noun}.")
    else:
        messages.info(request, "Choose what to do with them.")
    return redirect(back)


# ---------- budget ----------


@staff_required
@require_POST
def budget_bulk(request, pk):
    """Set who paid for the budget lines ticked on the Budget page (or clear it)."""
    trip = _trip(pk)
    back = reverse("manage:budget", args=[trip.pk])
    ids = [value for value in request.POST.getlist("lines") if value.isdigit()]
    lines = trip.budget_items.filter(pk__in=ids)
    count = lines.count()
    if not count:
        messages.info(request, "Tick at least one line first.")
        return redirect(back)
    choice = request.POST.get("paid_by", "")
    if choice == "none":
        traveller = None
    elif choice.isdigit() and (traveller := trip.travellers.filter(pk=choice).first()):
        pass
    else:
        messages.info(request, "Choose who paid.")
        return redirect(back)
    lines.update(paid_by=traveller)
    noun = f"{count} line{'s' if count != 1 else ''}"
    messages.success(request, f"Set {noun} as paid by {traveller.initials}." if traveller else f"Cleared who paid for {noun}.")
    return redirect(back)


@staff_required
def budget(request, pk):
    trip = _trip(pk)
    latest = ExchangeRate.objects.order_by("-date").first()
    rates = ExchangeRate.objects.filter(date=latest.date) if latest else []
    return render(request, "manage/budget.html", _context(trip, "budget", totals=_budget_totals(trip), rates=rates, rates_date=latest.date if latest else None))


@staff_required
def budget_form(request, pk, item_pk=None):
    trip = _trip(pk)
    item = get_object_or_404(BudgetItem, pk=item_pk, trip=trip) if item_pk else None
    form = BudgetItemForm(request.POST or None, instance=item, trip=trip)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.trip = trip
        item.save()
        messages.success(request, f"Saved '{item}'.")
        return redirect("manage:budget", trip.pk)
    delete_url = reverse("manage:budget_delete", args=[trip.pk, item.pk]) if item else None
    return _form_page(request, trip, "budget", form, f"Edit '{item}'" if item else "Add a budget item", reverse("manage:budget", args=[trip.pk]), delete_url)


@staff_required
def budget_delete(request, pk, item_pk):
    trip = _trip(pk)
    item = get_object_or_404(BudgetItem, pk=item_pk, trip=trip)
    back = reverse("manage:budget", args=[trip.pk])
    if request.method == "POST":
        item.delete()
        messages.success(request, "Budget item deleted.")
        return redirect(back)
    return render(request, "manage/confirm_delete.html", _context(trip, "budget", thing=f"'{item}'", back=back))


@staff_required
@require_POST
def refresh_rates(request, pk):
    try:
        count = money.refresh_rates()
        messages.success(request, f"Updated {count} exchange rates.")
    except Exception as exc:
        messages.error(request, f"Couldn't update exchange rates: {exc}")
    return redirect("manage:budget", pk)


# ---------- checklist ----------


@staff_required
def checklist(request, pk):
    trip = _trip(pk)
    form = ChecklistItemForm(request.POST or None, trip=trip)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.trip = trip
        item.save()
        messages.success(request, f"Added '{item}'.")
        return redirect("manage:checklist", trip.pk)
    items = trip.checklist.select_related("leg__origin", "leg__destination", "stay", "item")
    return render(request, "manage/checklist.html", _context(trip, "checklist", items=items, form=form, today=timezone.localdate()))


@staff_required
def checklist_edit(request, pk, item_pk):
    trip = _trip(pk)
    item = get_object_or_404(ChecklistItem, pk=item_pk, trip=trip)
    form = ChecklistItemForm(request.POST or None, instance=item, trip=trip)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect("manage:checklist", trip.pk)
    return _form_page(request, trip, "checklist", form, "Edit to-do", reverse("manage:checklist", args=[trip.pk]),
                      reverse("manage:checklist_delete", args=[trip.pk, item.pk]))


@staff_required
@require_POST
def checklist_toggle(request, pk, item_pk):
    item = get_object_or_404(ChecklistItem, pk=item_pk, trip_id=pk)
    item.done = not item.done
    item.done_at = timezone.now() if item.done else None
    item.save(update_fields=["done", "done_at"])
    return redirect(_next_url(request, reverse("manage:checklist", args=[pk])))


@staff_required
def checklist_delete(request, pk, item_pk):
    trip = _trip(pk)
    item = get_object_or_404(ChecklistItem, pk=item_pk, trip=trip)
    back = reverse("manage:checklist", args=[trip.pk])
    if request.method == "POST":
        item.delete()
        messages.success(request, "To-do deleted.")
        return redirect(back)
    return render(request, "manage/confirm_delete.html", _context(trip, "checklist", thing=f"'{item}'", back=back))


@staff_required
@require_POST
def checklist_ticket_reminders(request, pk):
    trip = _trip(pk)
    created = 0
    for leg in trip.legs.filter(mode=Leg.Mode.TRAIN).exclude(reminders__days_before__isnull=False).select_related("origin", "destination"):
        ChecklistItem.objects.create(
            trip=trip, leg=leg, days_before=trip.train_ticket_window,
            text=f"Train tickets open: {leg.number} {leg.origin.name} → {leg.destination.name}".replace("  ", " "),
        )
        created += 1
    messages.success(request, f"Added {created} ticket reminder(s)." if created else "Every train already has a reminder.")
    return redirect("manage:checklist", trip.pk)


# ---------- documents ----------


@staff_required
def documents(request, pk):
    trip = _trip(pk)
    form = DocumentForm(request.POST or None, request.FILES or None, trip=trip)
    if request.method == "POST" and form.is_valid():
        doc = form.save(commit=False)
        doc.trip = trip
        doc.save()
        messages.success(request, f"Uploaded '{doc}'.")
        return redirect("manage:documents", trip.pk)
    docs = trip.documents.select_related("leg__origin", "leg__destination", "stay")
    return render(request, "manage/documents.html", _context(trip, "documents", documents=docs, form=form))


@staff_required
def document_download(request, pk, doc_pk):
    doc = get_object_or_404(Document, pk=doc_pk, trip_id=pk)
    try:
        handle = doc.file.open("rb")
    except FileNotFoundError:
        raise Http404
    content_type = mimetypes.guess_type(doc.file.name)[0] or "application/octet-stream"
    inline = content_type == "application/pdf" or content_type.startswith("image/")
    response = FileResponse(handle, content_type=content_type, as_attachment=not inline, filename=doc.file.name.rsplit("/", 1)[-1])
    response["Cache-Control"] = "private, no-store"
    return response


@staff_required
def document_delete(request, pk, doc_pk):
    trip = _trip(pk)
    doc = get_object_or_404(Document, pk=doc_pk, trip=trip)
    back = reverse("manage:documents", args=[trip.pk])
    if request.method == "POST":
        doc.file.delete(save=False)
        doc.delete()
        messages.success(request, "Document deleted.")
        return redirect(back)
    return render(request, "manage/confirm_delete.html", _context(trip, "documents", thing=f"'{doc}'", back=back))


# ---------- booking confirmations ----------

IMPORT_FIELDS = [
    ("booking_ref", "Booking number"), ("name", "Property"), ("address", "Address"), ("name_local", "Local name"),
    ("address_local", "Local address"), ("phone", "Phone"), ("email", "Email"),
    ("check_in_date", "Check-in date"), ("check_in_time", "Check-in time"), ("check_out_date", "Check-out date"),
    ("check_out_time", "Check-out time"), ("room", "Room"), ("special_requests", "Special requests"),
]
ACTIVITY_FIELDS = [
    ("booking_ref", "Booking number"), ("title", "Activity"), ("activity_date", "Date"), ("start_time", "Start time"),
    ("meeting_point", "Meeting point"), ("name_local", "Local name"), ("address_local", "Local address"), ("travellers", "Travellers"),
]
TRANSFER_FIELDS = [
    ("booking_ref", "Booking number"), ("title", "Service"), ("activity_date", "Pick-up date"), ("start_time", "Pick-up time"),
    ("pickup", "Pick-up"), ("dropoff", "Drop-off"), ("travellers", "Travellers"),
]
CURRENT = {  # what each detail is called on the activity or leg it's added to
    "activity": {"title": "title", "meeting_point": "address", "start_time": "start_time", "name_local": "name_local",
                 "address_local": "address_local"},
    "transfer": {"booking_ref": "booking_ref", "start_time": "depart_time", "activity_date": "depart_date"},
}


CONFIRM_KINDS = {"stay": (Stay, "trip", "stays"), "leg": (Leg, "trip", "legs"), "item": (Item, "day__trip", "activities")}
CONFIRM_TABS = {"stays": "stays", "legs": "transport", "activities": "activities"}
LEG_FIELDS = [("booking_ref", "Booking number"), ("start_time", "Departure or pick-up time"), ("travellers", "Travellers")]
CONFIRM_FIELDS = {"stay": IMPORT_FIELDS, "item": ACTIVITY_FIELDS, "leg": LEG_FIELDS}
CONFIRM_CURRENT = {  # what each detail is called on the plan now
    "stay": {key: key for key, _ in IMPORT_FIELDS if key not in ("room", "special_requests")},
    "item": CURRENT["activity"] | {"booking_ref": None},
    "leg": {"booking_ref": "booking_ref", "start_time": "depart_time"},
}


def _keep_upload(record, upload):
    """Keep an uploaded confirmation with its import, so the plan it's added to can open it."""
    if upload:
        upload.seek(0)
        record.file.save(upload.name.rsplit("/", 1)[-1][:100], upload, save=True)


def _attach_confirmation(record, plan):
    """Once a confirmation is added to a plan: what View confirmation opens, the link it was read from
    or its uploaded file (kept as one of the trip's documents). A pasted confirmation gives neither."""
    from django.core.files import File

    fields = []
    if record.link:
        plan.confirmation_url, fields = record.link[:500], fields + ["confirmation_url"]
    if record.file:
        kind = "stay" if isinstance(plan, Stay) else "leg" if isinstance(plan, Leg) else "item"
        doc = Document(trip=booking_costs.trip_of(plan), title=f"Confirmation: {plan}"[:200], **{kind: plan})
        with record.file.open("rb") as handle:
            doc.file.save(record.file.name.rsplit("/", 1)[-1], File(handle), save=True)
        plan.confirmation_document, fields = doc, fields + ["confirmation_document"]
    if fields:
        plan.save(update_fields=fields)


@staff_required
def confirm_booking(request, pk, kind, plan_pk):
    """"Add confirmation" on a booked stay, transport or activity: read its confirmation (a link, pasted
    text or a file), show what was found next to what's there now, then fill in the details chosen and
    put what it cost on the budget (which keeps it booked)."""
    trip = _trip(pk)
    if kind not in CONFIRM_KINDS:
        raise Http404
    model, trip_field, page = CONFIRM_KINDS[kind]
    plan = get_object_or_404(model, pk=plan_pk, **{trip_field: trip})
    back = reverse(f"manage:{page}", args=[trip.pk])
    record = None
    if request.GET.get("import", "").isdigit():
        record = get_object_or_404(BookingImport, pk=request.GET["import"], trip=trip)

    if record and request.method == "POST" and request.POST.get("action") == "apply":
        found = booking_import.parse(record.text, trip, record.subject)
        fields = set(request.POST.getlist("fields"))
        with transaction.atomic():
            if kind == "stay":
                booking_import.apply_to_stay(plan, found, fields)
            elif kind == "item":
                booking_import.apply_to_activity(plan, found, fields)
            else:
                booking_import.apply_to_transfer(plan, found, fields)
            if request.POST.get("budget") and found["amount"]:
                booking_costs.record(plan, found["amount"], found["currency"] or "AUD", site=found["site"])
            _attach_confirmation(record, plan)
            record.status, record.applied_at = BookingImport.Status.APPLIED, timezone.now()
            setattr(record, kind, plan)
            record.save()
        messages.success(request, f"Added the confirmed details to {plan}.")
        return redirect(back)

    if record:
        found = booking_import.parse(record.text, trip, record.subject)

        def show(value):
            if hasattr(value, "strftime"):
                return value.strftime("%a %-d %b %Y") if hasattr(value, "year") else value.strftime("%H:%M")
            return value or ""

        current = CONFIRM_CURRENT[kind]
        rows = [{"key": key, "label": label, "value": show(found.get(key)),
                 "current": show(getattr(plan, current[key], None)) if current.get(key) else ""} for key, label in CONFIRM_FIELDS[kind]]
        return render(request, "manage/confirm_booking.html", _context(
            trip, CONFIRM_TABS[page], plan=plan, kind=kind, back=back, record=record, found=found, rows=rows,
            nothing=not any(row["value"] for row in rows) and not found["amount"]))

    form = ConfirmationForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        link, upload = form.cleaned_data.get("link"), form.cleaned_data.get("file")
        subject = ""
        if upload:
            subject, _sender, text = booking_import.text_from_upload(upload)
            source = BookingImport.Source.UPLOAD
        elif form.cleaned_data.get("text", "").strip():
            text, source = form.cleaned_data["text"], BookingImport.Source.PASTE
        else:
            try:
                text = booking_import.html_to_text(stay_import.fetch_page(link)[:2_000_000])
            except stay_import.PageError as exc:
                text = ""
                form.add_error("link", f"{exc} Booking sites usually need you signed in: paste the confirmation or upload it instead.")
            source = BookingImport.Source.LINK
        if text.strip() and not form.errors:
            record = BookingImport.objects.create(trip=trip, source=source, subject=(subject or link or "")[:300], link=(link or "")[:500],
                                                  text=booking_import.redact(booking_import.clean_text(text))[:200_000])
            _keep_upload(record, upload)
            return redirect(f"{reverse('manage:confirm_booking', args=[trip.pk, kind, plan.pk])}?import={record.pk}")
        if not form.errors:
            form.add_error(None, "Couldn't read any text from that.")
    return render(request, "manage/confirm_booking.html", _context(trip, CONFIRM_TABS[page], plan=plan, kind=kind, back=back, form=form))


@functools.cache
def _bookmarklet_source():
    """journeys/bookmarklet.js without its comment lines."""
    text = Path(__file__).with_name("bookmarklet.js").read_text()
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _bookmarklet(request):
    """The Send to Trips bookmarklet: a javascript: link to drag to the bookmarks bar."""
    target = request.build_absolute_uri(reverse("manage:capture"))
    return "javascript:" + urllib.parse.quote(_bookmarklet_source().replace("__CAPTURE_URL__", target), safe="")


@staff_required
def capture(request):
    """Send to Trips: where the bookmarklet brings a page from the browser, in the address's #fragment
    (which only this page's script reads, static/js/capture.js). Pick the trip, whether it's an activity,
    a stay or transport, and the day; the page is read (by Claude too, when it's set up), and the new
    plan's form opens filled in. Without a page, it shows how to add the bookmarklet."""
    trips = [{"id": trip.pk, "title": trip.title, "start": trip.start_date.isoformat(), "end": trip.end_date.isoformat(),
              "days": [{"id": day.pk, "date": day.date.isoformat(), "label": f"{day.date:%a %-d %b} · Day {day.number}"}
                       for day in trip.days.all()]}
             for trip in Trip.objects.prefetch_related("days")]
    urls = {"activity": reverse("manage:api_activity_extract", args=[0]), "stay": reverse("manage:api_stay_extract", args=[0]),
            "leg": reverse("manage:api_leg_extract", args=[0]), "activity_form": reverse("manage:item_create", args=[0]),
            "stay_form": reverse("manage:stay_create", args=[0]), "leg_form": reverse("manage:leg_create", args=[0])}
    return render(request, "manage/capture.html", {"trips": trips, "urls": urls, "bookmarklet": _bookmarklet(request),
                                                   "claude": claude_read.enabled()})


@staff_required
def imports(request):
    """The bookings inbox: forwarded emails waiting for review, and a form to paste or upload a confirmation."""
    initial = {"trip": request.GET.get("trip")} if request.GET.get("trip", "").isdigit() else {}
    form = BookingImportForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data.get("file")
        if upload:
            subject, sender, text = booking_import.text_from_upload(upload)
            source = BookingImport.Source.UPLOAD
            subject = subject or upload.name
        else:
            subject, sender, text = "", "", form.cleaned_data["text"]
            source = BookingImport.Source.PASTE
        if not text.strip():
            messages.error(request, "Couldn't read any text from that file.")
            return redirect("manage:imports")
        trip = form.cleaned_data.get("trip") or booking_import.guess_trip(booking_import.parse(text), Trip.objects.all())
        record = BookingImport.objects.create(trip=trip, source=source, subject=subject[:300], sender=sender[:200],
                                              text=booking_import.redact(booking_import.clean_text(text))[:200_000])
        _keep_upload(record, upload)
        return redirect("manage:import_review", record.pk)
    waiting = BookingImport.objects.filter(status=BookingImport.Status.NEW).select_related("trip")
    done = BookingImport.objects.exclude(status=BookingImport.Status.NEW).select_related("trip", "stay", "leg", "item")[:20]
    inbound = request.build_absolute_uri(reverse("inbound_email", args=[settings.INBOUND_EMAIL_TOKEN])) if settings.INBOUND_EMAIL_TOKEN else None
    return render(request, "manage/imports.html", {"form": form, "waiting": waiting, "done": done, "inbound_url": inbound,
                                                   "allowed_senders": settings.INBOUND_ALLOWED_SENDERS})


@staff_required
def import_review(request, import_pk):
    record = get_object_or_404(BookingImport, pk=import_pk)
    trips = Trip.objects.all()
    trip = record.trip
    if request.method == "POST" and request.POST.get("trip", "").isdigit():
        trip = get_object_or_404(Trip, pk=request.POST["trip"])
    found = booking_import.parse(record.text, trip, record.subject)
    kind = found.get("kind", "stay")
    stay = booking_import.match_stay(trip, found) if trip and kind == "stay" else None
    legs = list(Leg.objects.filter(pk__in=found["legs"]).select_related("origin", "destination"))
    if kind != "stay":
        return _import_review_other(request, record, trips, trip, found, kind)

    if request.method == "POST" and request.POST.get("action") == "retrip":
        record.trip = trip
        record.save(update_fields=["trip"])
        return redirect("manage:import_review", record.pk)

    if request.method == "POST" and request.POST.get("action") == "dismiss":
        record.status = BookingImport.Status.DISMISSED
        record.save(update_fields=["status"])
        messages.info(request, "Dismissed.")
        return redirect("manage:imports")

    if request.method == "POST" and request.POST.get("action") == "apply":
        if not trip:
            messages.error(request, "Choose which trip this booking belongs to.")
            return redirect("manage:import_review", record.pk)
        fields = set(request.POST.getlist("fields"))
        target = request.POST.get("stay", "")
        chosen = None
        with transaction.atomic():
            if target == "new":
                if not found["check_in_date"]:
                    messages.error(request, "No check-in date was found, so a new stay can't be created. Add it under Stays instead.")
                    return redirect("manage:import_review", record.pk)
                city = booking_import.city_for(found)
                chosen = Stay(trip=trip, label=(city.name if city else "New stay")[:100], city=city, added_from=AddedFrom.IMPORT,  # visitors see the label: never the hotel
                              check_in_date=found["check_in_date"],
                              check_out_date=found["check_out_date"] or found["check_in_date"] + timedelta(days=found["nights"] or 1))
                fields |= {"check_in_date", "check_out_date"}
            elif target.isdigit():
                chosen = get_object_or_404(Stay, pk=target, trip=trip)
            if chosen:
                booking_import.apply_to_stay(chosen, found, fields)
                _attach_confirmation(record, chosen)
                if request.POST.get("budget") and found["amount"]:
                    booking_costs.record(chosen, found["amount"], found["currency"] or "AUD")
            updated_legs = booking_import.apply_to_legs(found, fields) if request.POST.get("legs") else []
            record.status, record.trip, record.stay, record.applied_at = BookingImport.Status.APPLIED, trip, chosen, timezone.now()
            record.save()
        parts = ([f"updated stay '{chosen}'"] if chosen else []) + ([f"marked {len(updated_legs)} leg(s) booked"] if updated_legs else [])
        messages.success(request, "Added to the trip: " + (", ".join(parts) or "nothing to change") + ".")
        return redirect("manage:stays", trip.pk) if chosen else redirect("manage:trip", trip.pk)

    def show(value):
        if hasattr(value, "strftime"):
            return value.strftime("%a %-d %b %Y") if hasattr(value, "year") else value.strftime("%H:%M")
        return value or ""

    rows = []
    for key, label in IMPORT_FIELDS:
        current = getattr(stay, key, None) if stay and key not in ("room", "special_requests") else None
        rows.append({"key": key, "label": label, "value": show(found.get(key)), "current": show(current)})
    return render(request, "manage/import_review.html", {
        "record": record, "found": found, "rows": rows, "trip": None, "chosen_trip": trip, "trips": trips,
        "stay": stay, "stays": trip.stays.all() if trip else [], "legs": legs,
        "budget_item": booking_import.budget_item_for(stay),
    })


# ---------- JSON helpers for the forms ----------


@staff_required
@require_GET
def api_places(request):
    kinds = [k for k in request.GET.get("kinds", "").split(",") if k in Place.Kind.values]
    return JsonResponse({"results": [place_json(p) for p in find_places(request.GET.get("q", ""), kinds or None)]})


@staff_required
@require_GET
def api_transit_journey(request):
    """The timetabled journey between two Brisbane, Melbourne or Sydney stations, for the transport form."""
    ids = [request.GET.get(key, "") for key in ("from", "to")]
    places = {p.pk: p for p in Place.objects.filter(pk__in=[i for i in ids if i.isdigit()])}
    origin, destination = (places.get(int(i)) if i.isdigit() else None for i in ids)
    journey = transit.journey_between(origin, destination) if origin and destination else None
    if not journey:
        return JsonResponse({"found": False})
    found = journey[1]
    operators = list(dict.fromkeys(ride["operator"] for ride in found["rides"] if ride.get("operator")))
    return JsonResponse({"found": True, "summary": transit.summary(found), "minutes": round(found["minutes"]), "operator": " / ".join(operators)})


@staff_required
@require_GET
def api_place_search(request):
    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    try:
        return JsonResponse({"results": geocode.search(query)})
    except Exception as exc:
        return JsonResponse({"results": [], "error": f"Map search failed: {exc}"}, status=502)


@staff_required
@require_POST
def api_place_create(request):
    try:
        data = json.loads(request.body)
        result = {key: data[key] for key in ("source_id", "name", "name_local", "kind", "lat", "lon", "country")}
        result["city"] = str(data.get("city", ""))[:120]
        result["lat"], result["lon"] = float(result["lat"]), float(result["lon"])
        if not (math.isfinite(result["lat"]) and math.isfinite(result["lon"]) and -90 <= result["lat"] <= 90 and -180 <= result["lon"] <= 180):
            raise ValueError("not a position on Earth")
        if result["kind"] not in Place.Kind.values:
            result["kind"] = Place.Kind.OTHER
    except (KeyError, ValueError, TypeError):
        return JsonResponse({"error": "Bad place data"}, status=400)
    return JsonResponse(place_json(geocode.save_result(result)))


@staff_required
@require_POST
def api_stay_extract(request, pk):
    """Stay details from a hotel or booking page link, or from page text pasted in."""
    _trip(pk)
    url, text, data = _link_request(request)
    if not (url or text.strip() or data["capture"]):
        return JsonResponse({"error": "Paste a link or the page text first."}, status=400)
    try:
        result = link_details.read_stay(url, text, capture=data["capture"])
    except stay_import.PageError as exc:
        return JsonResponse({"error": str(exc)}, status=422)
    return JsonResponse({"fields": result["fields"], "missing": result["missing"],
                         "city": place_json(result["city"]) if result["city"] else None,
                         "location": place_json(result["location"]) if result["location"] else None})


@staff_required
@require_POST
def api_parse_leg(request):
    try:
        data = json.loads(request.body)
    except ValueError:
        data = {}
    trip = Trip.objects.filter(pk=data.get("trip")).first() if str(data.get("trip", "")).isdigit() else None
    try:
        depart = date.fromisoformat(data.get("date") or "")
    except (TypeError, ValueError):
        depart = None
    return JsonResponse(parse_leg(str(data.get("text", ""))[:300], trip=trip, depart_date=depart))


# ---------- taxi cards, the trip check and stop guides ----------


@staff_required
def taxi_cards(request, pk):
    """Each stay's and activity's name and address as written locally, big enough to show a driver.
    Private: hotels' names and addresses never reach visitors."""
    trip = _trip(pk)
    stays = list(trip.stays.select_related("city").order_by("check_in_date", "id"))
    items = list(Item.objects.filter(day__trip=trip).select_related("day").order_by("day__date", "start_time", "id"))
    cards, without = [], []
    for plan in stays + items:
        is_stay = isinstance(plan, Stay)
        entry = {
            "plan": plan, "is_stay": is_stay, "name": (plan.name or plan.label) if is_stay else plan.title,
            "when": (f"{plan.check_in_date:%a} {plan.check_in_date.day} – {plan.check_out_date:%a} {plan.check_out_date.day} {plan.check_out_date:%b}"
                     if is_stay else f"{plan.day.date:%a} {plan.day.date.day} {plan.day.date:%b}"),
            "edit": reverse("manage:stay_edit" if is_stay else "manage:item_edit", args=[trip.pk, plan.pk]),
        }
        (cards if plan.name_local or plan.address_local else without).append(entry)
    return render(request, "manage/taxi_cards.html", _context(trip, "stays", cards=cards, without=without))


@staff_required
def trip_check_page(request, pk):
    """The trip check: the rules' findings (always fresh) and Claude's last review, run again on request."""
    trip = _trip(pk)
    if request.method == "POST":
        if not claude_read.enabled():
            messages.error(request, "Claude isn't set up on this site, so only the checks below ran.")
        elif trip_check.review(trip):
            messages.success(request, "Claude has reviewed the plan.")
        else:
            messages.error(request, "Claude didn't answer in time. Try again in a minute.")
        return redirect("manage:check", trip.pk)
    review = getattr(trip, "claude_check", None)
    context = _context(
        trip, "check", findings=trip_check.with_dates(trip_check.rules(trip)), review=review,
        review_findings=trip_check.with_dates(list(review.findings)) if review else [],
        review_stale=bool(review) and review.plan_key != trip_check.plan_key(trip_check.plan_text(trip)),
        claude=claude_read.enabled(),
    )
    return render(request, "manage/check.html", context)


def _stop_places(trip):
    """The trip's overnight stops that are places (cities), each with its guide (or None)."""
    stops = [stop for stop in presenters.stops(trip) if stop["place"]]
    found = {guide.place_id: guide for guide in PlaceGuide.objects.filter(place__in=[stop["place"] for stop in stops])}
    for stop in stops:
        stop["guide"] = found.get(stop["place"].pk)
    return stops


@staff_required
def guides_page(request, pk):
    trip = _trip(pk)
    public = trip.get_public_url()
    stops = _stop_places(trip)
    for stop in stops:
        stop["public_url"] = reverse("journeys:stop", args=[trip.slug, stop["place"].pk]) if trip.visibility == Trip.Visibility.PUBLIC else (
            reverse("journeys:shared_stop", args=[trip.share_token, stop["place"].pk]) if public else "")
    return render(request, "manage/guides.html", _context(trip, "guides", stops=stops, claude=claude_read.enabled()))


def _trip_stop(trip, place_pk):
    for stop in _stop_places(trip):
        if stop["place"].pk == place_pk:
            return stop
    raise Http404


@staff_required
@require_POST
def guide_draft(request, pk, place_pk):
    """Draft with Claude (or Draft again): the guide is saved unpublished, for checking."""
    trip = _trip(pk)
    stop = _trip_stop(trip, place_pk)
    if not claude_read.enabled():
        messages.error(request, "Claude isn't set up on this site, so guides have to be written by hand.")
        return redirect("manage:guide_edit", trip.pk, place_pk)
    guide = guides.draft(stop["place"], when=stop["first"], again=bool(stop["guide"]))
    if guide:
        messages.success(request, f"Claude drafted the guide to {stop['name']}. Check the facts, change anything that's off, then publish it.")
    else:
        messages.error(request, "Claude didn't answer in time. Try again in a minute.")
    return redirect("manage:guide_edit", trip.pk, place_pk)


@staff_required
def guide_edit(request, pk, place_pk):
    trip = _trip(pk)
    stop = _trip_stop(trip, place_pk)
    guide = stop["guide"] or PlaceGuide(place=stop["place"])
    form = PlaceGuideForm(request.POST or None, instance=guide)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved the guide to {stop['name']}" + (" (published)." if form.instance.is_published else " as a draft."))
        return redirect("manage:guides", trip.pk)
    return render(request, "manage/guide_form.html", _context(
        trip, "guides", form=form, stop=stop, guide=guide, claude=claude_read.enabled(), pinyin=guides.pinyin(stop["place"])))
