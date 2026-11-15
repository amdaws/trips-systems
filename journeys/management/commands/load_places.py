import logging

from django.core.management.base import BaseCommand

from journeys import datasets


class Command(BaseCommand):
    help = "Load airports (OurAirports), airlines (OpenFlights) and railway/metro stations (OpenStreetMap)."

    def add_arguments(self, parser):
        parser.add_argument("--only", choices=["airports", "airlines", "stations"], help="Load just one dataset.")
        parser.add_argument("--countries", default=",".join(datasets.STATION_COUNTRIES), help="Station countries.")

    def handle(self, *args, **options):
        logging.getLogger("journeys").setLevel(logging.INFO)
        if options["only"] in (None, "airports"):
            created, updated = datasets.load_airports()
            self.stdout.write(f"Airports: {created} added, {updated} updated")
        if options["only"] in (None, "airlines"):
            self.stdout.write(f"Airlines: {datasets.load_airlines()} loaded")
        if options["only"] in (None, "stations"):
            countries = [c.strip().upper() for c in options["countries"].split(",") if c.strip()]
            created, updated = datasets.load_stations(countries)
            self.stdout.write(f"Stations: {created} added, {updated} updated")
