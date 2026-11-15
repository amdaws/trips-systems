from django.db import migrations


def refresh_search(apps, schema_editor):
    from journeys.models import place_search_text

    Place = apps.get_model("journeys", "Place")
    batch = []
    for place in Place.objects.only("id", "name", "name_local", "code", "aliases").iterator(chunk_size=2000):
        place.search = place_search_text(place.name, place.name_local, place.code, place.aliases)
        batch.append(place)
        if len(batch) >= 2000:
            Place.objects.bulk_update(batch, ["search"])
            batch = []
    Place.objects.bulk_update(batch, ["search"])


class Migration(migrations.Migration):
    dependencies = [("journeys", "0002_airline")]
    operations = [migrations.RunPython(refresh_search, migrations.RunPython.noop)]
