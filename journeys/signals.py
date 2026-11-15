"""Housekeeping when records are saved or removed."""

from django.db.models import QuerySet
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from . import changes
from .models import Document, Item, Leg, Stay, Trip, TripChange


@receiver(post_delete, sender=Document)
def delete_document_file(sender, instance, **kwargs):
    """Remove an uploaded file from disk with its record, so private documents don't linger
    (this also covers documents removed along with a whole trip)."""
    if instance.file:
        instance.file.delete(save=False)


# ---------- the "Recently updated" list (changes.py) ----------


def remember_state(sender, instance, raw=False, **kwargs):
    """Before a plan is saved, note how it looked, to compare afterwards."""
    if raw or instance.pk is None:
        return
    old = sender.objects.filter(pk=instance.pk).first()
    instance._state_before = changes.state(old) if old else None


def record_save(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    if created:
        changes.record(instance, TripChange.Action.ADDED)
    elif getattr(instance, "_state_before", None) is not None:
        changes.record(instance, TripChange.Action.CHANGED, before=instance._state_before)
        instance._state_before = None


def record_delete(sender, instance, origin=None, **kwargs):
    # A whole trip being deleted takes its list with it.
    if isinstance(origin, Trip) or (isinstance(origin, QuerySet) and origin.model is Trip):
        return
    changes.record(instance, TripChange.Action.REMOVED)


for model in (Leg, Stay, Item):
    pre_save.connect(remember_state, sender=model, dispatch_uid=f"remember-{model.__name__}")
    post_save.connect(record_save, sender=model, dispatch_uid=f"record-save-{model.__name__}")
    post_delete.connect(record_delete, sender=model, dispatch_uid=f"record-delete-{model.__name__}")
