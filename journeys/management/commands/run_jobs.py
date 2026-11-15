import fcntl
import logging
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from journeys import adsb_archive, china_rail, driving, flight_tracks, live_flights, metro, money, routing, transit, walking, weather
from journeys.models import ExchangeRate, Leg, MetroNetwork, Stay, Trip

log = logging.getLogger("journeys")


class Command(BaseCommand):
    help = "Background work: trace waiting routes, find hotels' metro stations, refresh exchange rates and weather. Run by trips-jobs.timer."

    def add_arguments(self, parser):
        parser.add_argument("--max-seconds", type=int, default=1500)

    def handle(self, *args, **options):
        lock_path = Path(settings.DATABASES["default"]["NAME"]).with_name("jobs.lock")
        with open(lock_path, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.stdout.write("Another run is still going; skipping.")
                return
            deadline = time.monotonic() + options["max_seconds"]
            self.record_live_flights()
            self.expire_flight_tracks()
            self.trace_flights()
            self.trace_routes(deadline)
            self.refresh_walking(deadline)
            self.refresh_driving(deadline)
            self.refresh_transit(deadline)
            self.refresh_china_stations()
            self.find_metro(deadline)
            self.refresh_rates()
            self.refresh_weather(deadline)

    def record_live_flights(self):
        """Positions of flights in the air now, so the map can draw the path flown so far even when
        nobody's looking; a landed flight's recording becomes its route (live_flights.py)."""
        try:
            for line in live_flights.record_all():
                self.stdout.write(f"Live flight {line}")
        except Exception as exc:
            log.warning("Recording live flights failed: %s", exc)

    def expire_flight_tracks(self):
        """Flightradar24 data may be kept for 30 days at most: redraw older tracks as arcs, and
        queue them to be fetched afresh (when a token is set)."""
        cutoff = timezone.now() - timedelta(days=flight_tracks.KEEP_DAYS)
        stale = Leg.objects.filter(path_source=Leg.PathSource.FLOWN).filter(Q(path_updated__lt=cutoff) | Q(path_updated__isnull=True))
        for leg in stale.select_related("origin", "destination"):
            routing.refresh_path(leg, allow_network=False)
            self.stdout.write(f"Flight track for {leg} expired: {leg.get_path_status_display()}")

    def trace_flights(self):
        """Every flight waiting for a real track, in one read of adsb.lol's archive (a few minutes)."""
        if flight_tracks.enabled() or not adsb_archive.enabled():
            return  # Flightradar24 looks flights up one at a time in trace_routes
        waiting = Leg.objects.filter(mode=Leg.Mode.FLIGHT, path_status=Leg.PathStatus.PENDING).exclude(number="")
        legs = list(waiting.select_related("origin", "destination"))
        if legs:
            routing.trace_flights(legs)
            for leg in legs:
                self.stdout.write(f"Route for {leg}: {leg.path_message}")

    def trace_routes(self, deadline):
        pending = Leg.objects.filter(path_status=Leg.PathStatus.PENDING).select_related("origin", "destination")
        if adsb_archive.enabled() and not flight_tracks.enabled():
            pending = pending.exclude(mode=Leg.Mode.FLIGHT)  # left for the next trace_flights, not a read each
        for leg in pending.order_by("depart_date")[:20]:
            if time.monotonic() > deadline - 400:  # a long route can take several minutes
                break
            routing.refresh_path(leg)
            self.stdout.write(f"Route for {leg}: {leg.get_path_status_display()} {leg.path_message}".rstrip())

    def refresh_walking(self, deadline):
        """One city's walking network a run, when a month old (walking.py); then its timetable is
        rebuilt so the walks between stops follow the new streets."""
        slug = walking.due()
        if not slug or time.monotonic() > deadline - 900:  # the download and two reads take a few minutes
            return
        try:
            result = walking.build(slug)
        except Exception as exc:
            log.warning("Walking network for %s failed: %s", slug, exc)
            walking.mark_failed(slug)
            return
        self.stdout.write(f"Walking network for {slug}: {result['nodes']} points, {result['edges']} pieces")
        for network in MetroNetwork.objects.filter(slug=slug, source=MetroNetwork.Source.TIMETABLE):
            network.status = dict(network.status or {}, built="2000-01-01T00:00:00+00:00")
            network.save(update_fields=["status"])

    def refresh_china_stations(self):
        """12306's list of China's stations, for the journey planner's China trains, when a week old."""
        if not china_rail.stations_due():
            return
        try:
            self.stdout.write(f"China Railway stations: {china_rail.refresh_stations()}")
        except Exception as exc:
            log.warning("China Railway station list failed: %s", exc)

    def refresh_driving(self, deadline):
        """Roads for driving (driving.py), one step a run: build a city's network from its newly put
        together roads; else put a city's fetched tiles together; else fetch more tiles (monthly)."""
        slug = driving.build_due()
        if slug:
            if time.monotonic() > deadline - 400:
                return
            driving.mark_failed(slug)  # cleared when it finishes: if memory runs out mid-build, it waits before trying again
            try:
                roads = driving.build(slug)
            except Exception as exc:
                log.warning("Road network for %s failed: %s", slug, exc)
                driving.mark_failed(slug)
                return
            self.stdout.write(f"Road network for {slug}: {roads['nodes']} points, {roads['edges']} pieces")
            return
        slug = driving.compose_due()
        if slug:
            self.stdout.write(f"Roads put together for {slug}: {driving.compose(slug)}")
            return
        slug = driving.fetch_due()
        fetched = 0
        while slug and time.monotonic() < deadline - 600:
            due = driving.tiles_due(slug)
            if not due:
                break
            name, box = due[0]
            try:
                driving.fetch_tile(slug, name, box)
            except Exception as exc:
                log.warning("Road tile %s %s failed: %s", slug, name, exc)
                driving.mark_failed("overpass")
                break
            fetched += 1
            time.sleep(2)  # gently: Overpass is a shared public service
        if fetched:
            self.stdout.write(f"Road tiles fetched for {slug}: {fetched} ({len(driving.tiles_due(slug))} to go)")

    def refresh_transit(self, deadline):
        """One city's train, tram and ferry timetables a run, when a week old (transit.py)."""
        slug = transit.due()
        if not slug or time.monotonic() > deadline - 300:  # a download and read take up to a few minutes
            return
        try:
            network = transit.refresh(slug)
        except Exception as exc:
            log.warning("Timetable refresh for %s failed: %s", slug, exc)
            transit.mark_failed(slug, exc)
            return
        self.stdout.write(f"Timetables for {network.name}: {network.status['stations']} stations, {network.status['lines']} lines")
        # Stays there look again with the new timetable (their activities follow), and legs between its stations are redrawn.
        today = timezone.localdate()
        for stay in Stay.objects.filter(trip__end_date__gte=today, city__isnull=False).select_related("city"):
            if transit.city_for(stay.city.coords) == slug:
                metro.reset(stay)
                stay.save(update_fields=["metro", "location"])
        legs = Leg.objects.filter(trip__end_date__gte=today, origin__source="timetable", destination__source="timetable")
        for leg in legs.select_related("origin", "destination"):
            if transit.city_for(leg.origin.coords) == slug and leg.mode in routing.TIMETABLE_MODES:
                routing.refresh_path(leg, allow_network=False)

    def find_metro(self, deadline):
        now = timezone.now()
        waiting = Stay.objects.filter(trip__end_date__gte=timezone.localdate()).filter(
            Q(metro__isnull=True) | Q(metro__pending=True) | Q(metro__retry__lt=now.isoformat())
        )
        for stay in waiting.select_related("city", "location")[:10]:
            if time.monotonic() > deadline - 120:  # a new city's stations take up to a minute
                break
            try:
                info = metro.refresh_stay(stay)
            except Exception as exc:  # MetroMan or OpenStreetMap trouble: try again in a few hours
                log.warning("Metro lookup for stay %s failed: %s", stay.pk, exc)
                info = {"message": "Metro lookup failed; it will be tried again later.", "retry": (now + timedelta(hours=6)).isoformat()}
                Stay.objects.filter(pk=stay.pk).update(metro=info)
            self.stdout.write(f"Metro for {stay}: {info.get('station') or info.get('message', '')}")
        self.find_directions(deadline)

    def find_directions(self, deadline):
        """How to get to each activity from that night's hotel (after the hotels' own lookups)."""
        done = 0
        for trip in Trip.objects.filter(end_date__gte=timezone.localdate()):
            for item, stay, basis in metro.activities_waiting(trip):
                if done >= 20 or time.monotonic() > deadline - 60:
                    return
                if stay and (stay.metro is None or stay.metro.get("pending")):
                    continue  # wait for the hotel's station
                try:
                    info = metro.refresh_activity(item, stay, basis)
                except Exception as exc:
                    log.warning("Directions for activity %s failed: %s", item.pk, exc)
                    continue
                done += 1
                found = "metro" if info.get("route") else "taxi" if info.get("taxi") else "walk" if "walk_m" in info else info.get("message", "")
                self.stdout.write(f"Directions to {item}: {found}")

    def refresh_rates(self):
        if ExchangeRate.objects.filter(date=timezone.localdate()).exists():
            return
        try:
            self.stdout.write(f"Exchange rates: {money.refresh_rates()} updated")
        except Exception as exc:
            log.warning("Exchange rate refresh failed: %s", exc)

    def refresh_weather(self, deadline):
        today = timezone.localdate()
        for trip in Trip.objects.filter(Q(end_date__gte=today)):
            if time.monotonic() > deadline - 30:
                break
            updated = weather.refresh_trip(trip)
            if updated:
                self.stdout.write(f"Weather for {trip}: {updated} stops updated")
