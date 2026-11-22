"""Stay labels that name part of the property ("Hilton" for "Hilton Shenzhen Futian") become the city,
as Stay.save() now does for new ones, and the trip's "Recently updated" list stops showing the old
labels too (each change keeps what visitors saw)."""

from django.db import migrations


def hide_hotels_in_labels(apps, schema_editor):
    from journeys.models import label_gives_hotel_away  # a pure function of the four texts

    Stay = apps.get_model("journeys", "Stay")
    for stay in Stay.objects.select_related("city"):
        city = stay.city
        if label_gives_hotel_away(stay.label, stay.name, city.name if city else "", city.name_local if city else ""):
            Stay.objects.filter(pk=stay.pk).update(label=(city.name if city else "Stay")[:100])

    TripChange = apps.get_model("journeys", "TripChange")
    for change in TripChange.objects.filter(kind="stay"):
        data = change.data or {}
        name = (data.get("private") or {}).get("property", "")
        dirty = False
        for part in ("public", "was"):
            shown = data.get(part) or {}
            if shown.get("label") and label_gives_hotel_away(shown["label"], name, shown.get("city", "")):
                shown["label"] = shown.get("city") or "Stay"
                dirty = True
        if dirty:
            TripChange.objects.filter(pk=change.pk).update(data=data)


class Migration(migrations.Migration):

    dependencies = [
        ("journeys", "0027_item_people"),
    ]

    operations = [
        migrations.RunPython(hide_hotels_in_labels, migrations.RunPython.noop),
    ]
