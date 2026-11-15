import json

from django.core.management.base import BaseCommand

from journeys import datasets
from journeys.models import Place


class Command(BaseCommand):
    help = "Load places exported as JSON (a list of Place fields with source_id) for one source, e.g. a station export."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--source", default="osm")

    def handle(self, *args, **options):
        fields = {f.name for f in Place._meta.fields}
        with open(options["path"], encoding="utf-8") as handle:
            places = [Place(**{k: v for k, v in row.items() if k in fields}) for row in json.load(handle)]
        created, updated = datasets._upsert(options["source"], places)
        self.stdout.write(f"{created} added, {updated} updated")
