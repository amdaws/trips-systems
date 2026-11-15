# Stays and activities get the same Idea / Planned / Booked status as transport. A stay's old
# "booked" tick becomes Booked (ticked) or Planned (not), before the tick is removed. Budget
# lines can now belong to an activity, as they already could to a stay or a leg.

import django.db.models.deletion
from django.db import migrations, models

STATUS = [("idea", "Idea"), ("planned", "Planned"), ("booked", "Booked")]


def tick_to_status(apps, schema_editor):
    Stay = apps.get_model("journeys", "Stay")
    Stay.objects.filter(booked=True).update(status="booked")


def status_to_tick(apps, schema_editor):
    Stay = apps.get_model("journeys", "Stay")
    Stay.objects.filter(status="booked").update(booked=True)


class Migration(migrations.Migration):

    dependencies = [
        ("journeys", "0022_leg_live_path_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="stay",
            name="status",
            field=models.CharField(choices=STATUS, default="planned", max_length=10),
        ),
        migrations.RunPython(tick_to_status, status_to_tick),
        migrations.RemoveField(
            model_name="stay",
            name="booked",
        ),
        migrations.AddField(
            model_name="item",
            name="status",
            field=models.CharField(choices=STATUS, default="planned", max_length=10),
        ),
        migrations.AddField(
            model_name="budgetitem",
            name="item",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="budget_items", to="journeys.item", verbose_name="activity"),
        ),
    ]
