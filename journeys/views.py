"""Public pages: published trips (by slug) and unlisted trips (by secret link).

Visitors see the public transport, stays and activities only. The secret link, or the owner
signed in, also see the private ones (see presenters for exactly what each sees).
"""

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.cache import patch_cache_control

from . import changes, ics, journey_views, live_flights, presenters, transit
from .models import Item, Leg, Trip

PUBLISHED = [Trip.Visibility.PUBLIC, Trip.Visibility.UNLISTED]


def _resolve(slug=None, token=None):
    if token:
        return get_object_or_404(Trip, share_token=token, visibility__in=PUBLISHED)
    return get_object_or_404(Trip, slug=slug, visibility=Trip.Visibility.PUBLIC)


def _full(request, token):
    """Whether this viewer sees the private items too: the secret link, or the owner signed in."""
    return bool(token) or request.user.is_staff


def _urls(trip, token):
    if token:
        args, prefix = [token], "journeys:shared_"
    else:
        args, prefix = [trip.slug], "journeys:"
    return {
        "trip": reverse(prefix + "trip", args=args),
        "stays": reverse(prefix + "stays", args=args),
        "activities": reverse(prefix + "activities", args=args),
        "map": reverse(prefix + "map", args=args),
        "live": reverse(prefix + "live", args=args),
        "calendar": None if trip.hide_dates else reverse(prefix + "calendar", args=args),
    }


def _respond(response, token, full):
    if token:
        response["X-Robots-Tag"] = "noindex, nofollow"
    if full:
        patch_cache_control(response, private=True, no_store=True)
    return response


def _page(request, trip, token, full, template, tab, stops=None, **extra):
    urls = _urls(trip, token)
    context = {
        "trip": trip, "tab": tab, "full": full, "urls": urls, "unlisted": bool(token),
        "changes": changes.for_page(trip, full, urls, tab),
        "timetable_credits": ", ".join(transit.CITIES[n.slug]["credit"] for n in presenters.timetable_networks(trip, full)),
        "stops": presenters.stops(trip) if stops is None else stops,
        "summary": presenters.summary(trip, full),
        "stay_count": trip.stays.count() if full else trip.stays.filter(is_public=True).count(),
        "activity_count": Item.objects.filter(day__trip=trip, **({} if full else {"is_public": True})).count(),
        **extra,
    }
    return _respond(render(request, template, context), token, full)


def trip_detail(request, slug=None, token=None):
    trip, full = _resolve(slug, token), _full(request, token)
    stops = presenters.stops(trip)
    rows, journeys = presenters.itinerary(trip, full=full, stop_list=stops)
    return _page(
        request, trip, token, full, "journeys/trip.html", "itinerary", stops=stops,
        rows=rows, journeys=journeys, travel_days=sum(1 for row in rows if not row["band"]), countdown=presenters.countdown(trip, full),
        track_sources=set((trip.legs if full else trip.legs.filter(is_public=True))  # credits for the tracks this viewer sees
                          .filter(path_source__in=[Leg.PathSource.FLOWN, Leg.PathSource.ARCHIVE, Leg.PathSource.LIVE])
                          .values_list("path_source", flat=True)),
    )


def trip_stays(request, slug=None, token=None):
    trip, full = _resolve(slug, token), _full(request, token)
    return _page(request, trip, token, full, "journeys/stays.html", "stays", stays=presenters.stay_cards(trip, full))


def trip_activities(request, slug=None, token=None):
    trip, full = _resolve(slug, token), _full(request, token)
    return _page(request, trip, token, full, "journeys/activities.html", "activities", days=presenters.activity_days(trip, full))


def trip_map(request, slug=None, token=None):
    full = _full(request, token)
    response = JsonResponse(presenters.map_data(_resolve(slug, token), full=full))
    if not full:
        patch_cache_control(response, max_age=300)
    return _respond(response, token, full)


def trip_live(request, slug=None, token=None):
    """Where this trip's flights are while they're in the air (live_flights.py), for the map. Visitors
    see the public flights too, but not on trips that hide their dates."""
    trip, full = _resolve(slug, token), _full(request, token)
    if not journey_views._allowed(request, "live"):
        response = JsonResponse({"error": "Too many requests; try again in a minute.", "poll": 60}, status=429)
    elif trip.hide_dates and not full:
        response = JsonResponse({"flights": [], "poll": None})
    else:
        legs = trip.legs.filter(mode=Leg.Mode.FLIGHT).select_related("origin", "destination")
        response = JsonResponse(live_flights.for_trip(list(legs if full else legs.filter(is_public=True))))
    patch_cache_control(response, no_store=True, **({"private": True} if full else {}))
    return _respond(response, token, full)


def trip_calendar(request, slug=None, token=None):
    trip = _resolve(slug, token)
    if trip.hide_dates:
        raise Http404
    full = bool(token)  # a calendar app doesn't sign in, so only the secret link gets private items
    response = HttpResponse(ics.build(trip, full=full), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'inline; filename="{trip.slug}.ics"'
    return _respond(response, token, full)


def private_calendar(request, token):
    """Full-detail calendar feed for subscribing from a phone; the secret token is the key."""
    trip = get_object_or_404(Trip, calendar_token=token)
    response = HttpResponse(ics.build(trip, private=True), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'inline; filename="{trip.slug}-private.ics"'
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
