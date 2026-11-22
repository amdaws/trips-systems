"""Public pages: published trips (by slug) and unlisted trips (by secret link).

Visitors see the public transport, stays and activities only. The secret link, or the owner
signed in, also see the private ones (see presenters for exactly what each sees).
"""

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.cache import patch_cache_control

from datetime import timedelta

from . import changes, guides, ics, journey_views, live_flights, presenters, transit
from .models import Item, Leg, PlaceGuide, Trip

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


def _with_guides(request, trip, token, stops):
    """Give each stop whose guide this viewer may see (published, or a draft for the owner) its page's address."""
    places = [stop["place"] for stop in stops if stop["place"]]
    found = {guide.place_id: guide for guide in PlaceGuide.objects.filter(place__in=places)}
    for stop in stops:
        guide = found.get(stop["place"].pk) if stop["place"] else None
        stop["guide"] = guide
        stop["guide_url"] = (reverse("journeys:shared_stop", args=[token, stop["place"].pk]) if token else
                             reverse("journeys:stop", args=[trip.slug, stop["place"].pk])) if guides.viewable(guide, request.user.is_staff) else ""
    return stops


def _page(request, trip, token, full, template, tab, stops=None, **extra):
    urls = _urls(trip, token)
    context = {
        "trip": trip, "tab": tab, "full": full, "urls": urls, "unlisted": bool(token),
        "changes": changes.for_page(trip, full, urls, tab),
        "timetable_credits": ", ".join(transit.CITIES[n.slug]["credit"] for n in presenters.timetable_networks(trip, full)),
        "stops": _with_guides(request, trip, token, presenters.stops(trip) if stops is None else stops),
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
    stops = _with_guides(request, trip, token, presenters.stops(trip))
    guide_urls = {stop["place"].pk: stop["guide_url"] for stop in stops if stop["place"] and stop["guide_url"]}
    cards = presenters.stay_cards(trip, full)
    for card in cards:
        card.guide_url = guide_urls.get(card.city_id, "")
    return _page(request, trip, token, full, "journeys/stays.html", "stays", stops=stops, stays=cards)


def trip_activities(request, slug=None, token=None):
    trip, full = _resolve(slug, token), _full(request, token)
    return _page(request, trip, token, full, "journeys/activities.html", "activities", days=presenters.activity_days(trip, full))


def trip_stop(request, place_pk, slug=None, token=None):
    """A stop's guide (guides.py): every viewer sees a published one, the owner a draft too. It names
    the city and the days there, never the hotel; activities are the public ones unless the viewer has
    the secret link or is the owner."""
    trip, full = _resolve(slug, token), _full(request, token)
    stops = _with_guides(request, trip, token, presenters.stops(trip))
    index = next((i for i, stop in enumerate(stops) if stop["place"] and stop["place"].pk == place_pk and stop["guide_url"]), None)
    if index is None:
        raise Http404
    stop = stops[index]
    items = Item.objects.filter(day__trip=trip, day__date__range=(stop["first"], stop["last"] + timedelta(days=1))).select_related("day")
    planned = [{"title": item.title, "when": presenters.date_label(trip, item.day.date, full, "%a %-d %b")}
               for item in (items if full else items.filter(is_public=True)).order_by("day__date", "start_time", "id")]
    return _page(
        request, trip, token, full, "journeys/stop.html", "stays", stops=stops, stop=stop, guide=stop["guide"],
        pinyin=guides.pinyin(stop["place"]), previous=stops[index - 1] if index else None,
        following=stops[index + 1] if index + 1 < len(stops) else None, planned=planned,
        weather=presenters.weather_text(stop["weather"]),
        dates=presenters.date_label(trip, stop["first"], full, "%a %-d %b") + " – " + presenters.date_label(
            trip, stop["last"] + timedelta(days=1), full, "%a %-d %b"),
    )


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
