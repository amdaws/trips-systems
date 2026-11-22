from django.urls import path

from . import views

app_name = "journeys"

urlpatterns = [
    path("trips/<slug:slug>/", views.trip_detail, name="trip"),
    path("trips/<slug:slug>/stays/", views.trip_stays, name="stays"),
    path("trips/<slug:slug>/activities/", views.trip_activities, name="activities"),
    path("trips/<slug:slug>/stops/<int:place_pk>/", views.trip_stop, name="stop"),
    path("trips/<slug:slug>/map.json", views.trip_map, name="map"),
    path("trips/<slug:slug>/live.json", views.trip_live, name="live"),
    path("trips/<slug:slug>/calendar.ics", views.trip_calendar, name="calendar"),
    path("s/<str:token>/", views.trip_detail, name="shared_trip"),
    path("s/<str:token>/stays/", views.trip_stays, name="shared_stays"),
    path("s/<str:token>/activities/", views.trip_activities, name="shared_activities"),
    path("s/<str:token>/stops/<int:place_pk>/", views.trip_stop, name="shared_stop"),
    path("s/<str:token>/map.json", views.trip_map, name="shared_map"),
    path("s/<str:token>/live.json", views.trip_live, name="shared_live"),
    path("s/<str:token>/calendar.ics", views.trip_calendar, name="shared_calendar"),
    path("cal/<str:token>.ics", views.private_calendar, name="private_calendar"),
]
