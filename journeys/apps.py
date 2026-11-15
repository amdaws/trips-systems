from django.apps import AppConfig


class JourneysConfig(AppConfig):
    name = "journeys"

    def ready(self):
        from . import signals  # noqa: F401  (registers the handlers)
