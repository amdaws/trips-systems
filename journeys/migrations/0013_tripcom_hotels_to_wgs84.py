"""Hotels placed from Trip.com links before 25 Oct 2026 kept Trip.com's offset (GCJ-02) position
for mainland China, a few hundred metres out. Move them onto the world system and look up their
metro again. See journeys/china_coords.py."""

from django.db import migrations


def forwards(apps, schema_editor):
    from journeys.china_coords import to_wgs84

    Stay = apps.get_model("journeys", "Stay")
    moved = set()
    for stay in Stay.objects.filter(location__source="link").select_related("location"):
        if not any(site in stay.url for site in ("trip.com", "ctrip.com")):
            continue
        place = stay.location
        if place.pk not in moved:
            new = to_wgs84(place.lat, place.lon, "gcj02")
            if new != (round(place.lat, 6), round(place.lon, 6)):
                place.lat, place.lon = new
                place.save(update_fields=["lat", "lon"])
            moved.add(place.pk)
        stay.metro = {"pending": True, "geocoded": False}
        stay.save(update_fields=["metro"])


class Migration(migrations.Migration):
    dependencies = [
        ("journeys", "0012_item_address_directions"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
