from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from journeys import inbound

admin.site.site_header = "Aaron Daws Trips"
admin.site.site_title = "Trips admin"
admin.site.index_title = "Back office"

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("manage/", include("journeys.manage_urls")),
    path("inbound/email/<str:token>/", inbound.inbound_email, name="inbound_email"),
    path("", include("journeys.urls")),
    path("", include("core.urls")),
]
