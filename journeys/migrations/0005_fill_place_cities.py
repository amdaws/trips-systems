from django.db import migrations


def fill_cities(apps, schema_editor):
    from journeys.cities import derive_city, fill_related_cities

    Place = apps.get_model("journeys", "Place")
    batch = []
    for place in Place.objects.iterator(chunk_size=2000):
        place.city, place.city_local = derive_city(place)
        batch.append(place)
        if len(batch) >= 2000:
            Place.objects.bulk_update(batch, ["city", "city_local"])
            batch = []
    Place.objects.bulk_update(batch, ["city", "city_local"])
    fill_related_cities(Place)


class Migration(migrations.Migration):
    dependencies = [("journeys", "0004_place_city_flightlookup")]
    operations = [migrations.RunPython(fill_cities, migrations.RunPython.noop)]
