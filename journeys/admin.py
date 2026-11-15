"""Raw-data back-up editor. Day-to-day planning happens in the planner dashboard at /manage/."""

from django.contrib import admin

from .models import (
    BudgetItem,
    ChecklistItem,
    Day,
    Document,
    ExchangeRate,
    Item,
    Leg,
    Place,
    Stay,
    Traveller,
    Trip,
)


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("name", "name_local", "kind", "code", "country", "tz", "source")
    list_filter = ("kind", "country", "source")
    search_fields = ("search",)
    readonly_fields = ("source", "source_id")


class TravellerInline(admin.TabularInline):
    model = Traveller
    extra = 0


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = ("title", "start_date", "end_date", "visibility")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [TravellerInline]


class ItemInline(admin.StackedInline):
    model = Item
    extra = 0
    autocomplete_fields = ["place"]


@admin.register(Day)
class DayAdmin(admin.ModelAdmin):
    list_display = ("date", "trip", "title")
    list_filter = ("trip",)
    exclude = ["overnight"]  # kept in the database, unused: where each night is spent comes from the stays
    inlines = [ItemInline]


@admin.register(Leg)
class LegAdmin(admin.ModelAdmin):
    list_display = ("depart_date", "depart_time", "mode", "number", "origin", "destination", "status", "path_status")
    list_filter = ("trip", "mode", "status", "path_status")
    autocomplete_fields = ["origin", "destination"]
    readonly_fields = ("path_source", "path_status", "path_message")


@admin.register(Stay)
class StayAdmin(admin.ModelAdmin):
    list_display = ("label", "trip", "check_in_date", "check_out_date", "name", "status")
    list_filter = ("trip", "status")
    autocomplete_fields = ["city", "location"]


@admin.register(BudgetItem)
class BudgetItemAdmin(admin.ModelAdmin):
    list_display = ("description", "trip", "category", "actual_aud", "paid_by")
    list_filter = ("trip", "category")


admin.site.register([ChecklistItem, Document, ExchangeRate])
