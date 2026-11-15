"""Weather for each overnight stop, from Open-Meteo (free, CC BY 4.0, https://open-meteo.com).

Within FORECAST_DAYS of travel: the daily forecast. Further out: typical
weather, averaged over the same calendar dates in the previous ten years.
"""

import logging
from datetime import date, timedelta
from statistics import mean

from django.utils import timezone

from . import nights
from .http import get_json
from .models import WeatherCache

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_DAYS = 14
TYPICAL_YEARS = 10
FORECAST_MAX_AGE = timedelta(hours=6)
DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum"


def stops(trip):
    """The trip's overnight stops (consecutive nights in one city, from its stays) that have a city
    to look the weather up for, as (place, first night, last night)."""
    return [(stop["place"], stop["first"], stop["last"]) for stop in nights.stops(trip) if stop["place"]]


def kind_for(start, end, today=None):
    today = today or timezone.localdate()
    if end < today:
        return None
    if start <= today + timedelta(days=FORECAST_DAYS - 1):
        return WeatherCache.Kind.FORECAST
    return WeatherCache.Kind.TYPICAL


def for_stop(place, start, end):
    kind = kind_for(start, end)
    if not kind:
        return None
    return WeatherCache.objects.filter(place=place, kind=kind, start_date=start, end_date=end).first()


def refresh_trip(trip, force=False):
    """Fetch any missing or stale weather for a trip's stops. Returns how many entries were updated."""
    updated = 0
    for place, start, end in stops(trip):
        kind = kind_for(start, end)
        if not kind:
            continue
        entry = WeatherCache.objects.filter(place=place, kind=kind, start_date=start, end_date=end).first()
        if entry and not force:
            fresh = kind == WeatherCache.Kind.TYPICAL or timezone.now() - entry.fetched_at < FORECAST_MAX_AGE
            if fresh:
                continue
        try:
            data = _forecast(place, start, end) if kind == WeatherCache.Kind.FORECAST else _typical(place, start, end)
        except Exception as exc:
            log.warning("Weather lookup failed for %s: %s", place, exc)
            continue
        WeatherCache.objects.update_or_create(
            place=place, kind=kind, start_date=start, end_date=end, defaults={"data": data}
        )
        updated += 1
    return updated


def _forecast(place, start, end):
    today = timezone.localdate()
    params = {
        "latitude": place.lat, "longitude": place.lon, "daily": DAILY, "timezone": place.tz or "auto",
        "start_date": max(start, today).isoformat(),
        "end_date": min(end, today + timedelta(days=FORECAST_DAYS - 1)).isoformat(),
    }
    daily = get_json(FORECAST_URL, params)["daily"]
    days = [
        {"date": d, "max": hi, "min": lo, "rain": rain}
        for d, hi, lo, rain in zip(daily["time"], daily["temperature_2m_max"], daily["temperature_2m_min"], daily["precipitation_sum"])
    ]
    return {"days": days, "summary": _summarise(days)}


def _typical(place, start, end):
    """Average of the same month-days over the previous TYPICAL_YEARS years."""
    wanted = {(start + timedelta(days=i)).strftime("%m-%d") for i in range((end - start).days + 1)}
    last_year = timezone.localdate().year - 1
    params = {
        "latitude": place.lat, "longitude": place.lon, "daily": DAILY, "timezone": place.tz or "auto",
        "start_date": date(last_year - TYPICAL_YEARS + 1, 1, 1).isoformat(),
        "end_date": date(last_year, 12, 31).isoformat(),
    }
    daily = get_json(ARCHIVE_URL, params, timeout=60)["daily"]
    days = [
        {"max": hi, "min": lo, "rain": rain}
        for d, hi, lo, rain in zip(daily["time"], daily["temperature_2m_max"], daily["temperature_2m_min"], daily["precipitation_sum"])
        if d[5:] in wanted and hi is not None and lo is not None
    ]
    return {"years": TYPICAL_YEARS, "summary": _summarise(days)}


def _summarise(days):
    highs = [d["max"] for d in days if d["max"] is not None]
    lows = [d["min"] for d in days if d["min"] is not None]
    rains = [d["rain"] for d in days if d["rain"] is not None]
    if not highs or not lows:
        return None
    return {
        "max": round(mean(highs)),
        "min": round(mean(lows)),
        "rain_share": round(100 * sum(1 for r in rains if r >= 1) / len(rains)) if rains else None,
    }
