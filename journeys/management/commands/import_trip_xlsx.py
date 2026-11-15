from django.core.management.base import BaseCommand, CommandError

from journeys.xlsx_import import ImportProblem, import_workbook


class Command(BaseCommand):
    help = "Import a trip from a multi-tab spreadsheet in the 'China 2027 Trip' layout."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--title", default="China 2027")
        parser.add_argument("--slug", default="china-2027")
        parser.add_argument("--replace", action="store_true", help="Replace an existing trip with the same slug.")

    def handle(self, *args, **options):
        try:
            trip = import_workbook(options["path"], options["title"], options["slug"], options["replace"], log=self.stdout.write)
        except ImportProblem as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"Imported '{trip}' (private until you publish it)."))
