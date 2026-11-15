"""Currency conversion to AUD using daily rates from open.er-api.com (ExchangeRate-API)."""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from .http import get_json

log = logging.getLogger(__name__)

RATES_URL = "https://open.er-api.com/v6/latest/AUD"
CENT = Decimal("0.01")


def rate_for(currency, on_date=None):
    """Units of `currency` per 1 AUD on (or before) `on_date`, falling back to the latest known rate."""
    from .models import ExchangeRate

    if currency == "AUD":
        return Decimal(1)
    rates = ExchangeRate.objects.filter(currency=currency)
    if on_date:
        rate = rates.filter(date__lte=on_date).order_by("-date").first()
        if rate:
            return rate.per_aud
    rate = rates.order_by("-date").first()
    return rate.per_aud if rate else None


def to_aud(amount, currency, on_date=None):
    if amount is None:
        return None
    rate = rate_for(currency, on_date)
    if not rate:
        return None
    return (Decimal(amount) / rate).quantize(CENT, rounding=ROUND_HALF_UP)


def refresh_rates():
    """Store today's rates for every currency the planner offers; returns how many were saved."""
    from .models import CURRENCIES, BudgetItem, ExchangeRate

    data = get_json(RATES_URL)
    if data.get("result") != "success":
        raise RuntimeError(f"Exchange rate service error: {data.get('error-type', 'unknown')}")
    today = timezone.localdate()
    saved = 0
    for code, _label in CURRENCIES:
        if code != "AUD" and code in data["rates"]:
            ExchangeRate.objects.update_or_create(
                date=today, currency=code, defaults={"per_aud": Decimal(str(data["rates"][code]))}
            )
            saved += 1
    # Fill in conversions that were waiting for a rate.
    for item in BudgetItem.objects.exclude(estimate_currency="AUD", actual_currency="AUD"):
        if (item.estimate is not None and item.estimate_aud is None) or (item.actual is not None and item.actual_aud is None):
            item.save()
    return saved
