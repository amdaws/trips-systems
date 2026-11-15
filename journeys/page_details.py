"""Details a booking or product page can give a planned stay or activity before it's booked: a rough
price, opening hours, and phone and email. From schema.org data first (JSON-LD), then meta tags,
then the page's words. Anything not found is simply left out (the planner flags what's missing).
"""

import re

from .booking_costs import parse_money

DAYS = {"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu", "friday": "Fri", "saturday": "Sat", "sunday": "Sun"}
MONEY = r"((?:A\$|AU\$|US\$|HK\$|NT\$|MOP\$|CN¥|RMB|AUD|CNY|HKD|USD|TWD|MOP|EUR|GBP|¥|￥|€|£)\s?\d[\d,]*(?:\.\d{1,2})?)"


def _first(pattern, text, flags=re.I):
    match = re.search(pattern, text, flags)
    return match[1].strip() if match else ""


def price(objects, page, plain):
    """(amount as text, currency) for a rough price, or None."""
    for obj in objects:
        offers = obj.get("offers") or obj.get("makesOffer") or []
        for offer in offers if isinstance(offers, list) else [offers]:
            if isinstance(offer, dict):
                value = offer.get("price") or offer.get("lowPrice")
                money = parse_money(f"{offer.get('priceCurrency', '')} {value}") if value else None
                if money:
                    return str(money[0]), money[1]
        money = parse_money(obj.get("priceRange")) if isinstance(obj.get("priceRange"), str) else None
        if money:
            return str(money[0]), money[1]
    for amount_tag, currency_tag in (("product:price:amount", "product:price:currency"), ("og:price:amount", "og:price:currency")):
        amount = _first(rf'<meta[^>]+(?:property|name)="{amount_tag}"[^>]+content="([^"]+)"', page)
        currency = _first(rf'<meta[^>]+(?:property|name)="{currency_tag}"[^>]+content="([^"]+)"', page)
        money = parse_money(f"{currency} {amount}") if amount else None
        if money:
            return str(money[0]), money[1]
    money = parse_money(_first(rf"(?:prices?\s*from|from|price|per night|starting at|adult)\s*[:：]?\s*{MONEY}", plain))
    return (str(money[0]), money[1]) if money else None


def _days(value):
    names = value if isinstance(value, list) else [value]
    return [DAYS.get(str(name).rsplit("/", 1)[-1].lower(), str(name)) for name in names if name]


def opening_hours(objects, plain):
    """"Mon, Tue 09:00–17:00 · Sat 10:00–16:00" style text, or ""."""
    for obj in objects:
        hours = obj.get("openingHours")
        if isinstance(hours, list) and hours:
            return "; ".join(str(h) for h in hours)[:300]
        if isinstance(hours, str) and hours.strip():
            return hours.strip()[:300]
        spec = obj.get("openingHoursSpecification")
        if isinstance(spec, dict):
            spec = [spec]
        if isinstance(spec, list) and spec:
            parts = []
            for entry in spec:
                if isinstance(entry, dict) and entry.get("opens") and entry.get("closes"):
                    parts.append(f"{', '.join(_days(entry.get('dayOfWeek'))) or 'Daily'} {str(entry['opens'])[:5]}–{str(entry['closes'])[:5]}")
            if parts:
                return " · ".join(dict.fromkeys(parts))[:300]
    return _first(r"(?:opening hours|opening times|open(?:ing)? time|hours of operation|business hours)\s*[:：]\s*([^\n]{4,120})", plain)[:300]


def contact(objects, plain):
    """(phone, email), each "" when not found."""
    phone = email = ""
    for obj in objects:
        phone = phone or str(obj.get("telephone") or "").strip()
        email = email or str(obj.get("email") or "").replace("mailto:", "").strip()
    phone = phone or _first(r"(?:phone|tel(?:ephone)?|contact|hotline)\s*[:：]?\s*(\+?\d[\d\s()\-]{6,}\d)", plain)
    email = email or _first(r"\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b", plain)
    return phone[:60], email[:254]
