from django.urls import path
from django.views.generic import RedirectView

from journeys import journey_views

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("journey/", journey_views.journey_page, name="journey"),
    path("journey/api/plan/", journey_views.api_plan, name="journey_plan"),
    path("journey/api/nearby/", journey_views.api_nearby, name="journey_nearby"),
    path("journey/api/places/", journey_views.api_places, name="journey_places"),
    path("journey/api/china/stations/", journey_views.api_china_stations, name="journey_china_stations"),
    path("journey/api/china/trains/", journey_views.api_china_trains, name="journey_china_trains"),
    path("journey/api/airports/", journey_views.api_airports, name="journey_airports"),
    path("journey/api/flights/", journey_views.api_flights, name="journey_flights"),
    path("journey/api/add/", journey_views.api_add_to_trip, name="journey_add"),
    # Places and About were removed on 28 Oct 2026: their old addresses go to the home page.
    path("about/", RedirectView.as_view(pattern_name="core:home", permanent=True)),
    path("places/", RedirectView.as_view(pattern_name="core:home", permanent=True)),
    path("legals/", views.legals, name="legals"),
    path("changelog/", views.whats_new, name="changelog"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("csp-report/", views.csp_report, name="csp_report"),
]
