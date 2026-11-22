"""Stop guides: a short public page for each overnight stop, after the China by Rail city guides.

draft() asks Claude for a guide to a place from what's generally known about it: where it is, a
sentence about it, a few facts, a little history, what's happened in the last ten years and the
natural wonders nearby. Only the place's name, local name, country and position are sent. The draft
is saved unpublished (PlaceGuide); the owner checks and edits it in the planner, then publishes it.

The stop page (views.trip_stop) shows a published guide to every viewer, and a draft only to the
owner signed in. It names the city, never the hotel.
"""

from django.conf import settings
from django.utils import timezone

from . import claude_read
from .models import PlaceGuide

SYSTEM = (
    "You write short, accurate guides to towns and cities for the stop pages of a traveller's trip website, in plain "
    "Australian English. Use only facts you're confident of. Give numbers as approximate ('≈ 3,300 m', '≈ 180,000'), and "
    "leave out anything you're unsure of rather than guess: a short guide that's right is better than a long one that "
    "isn't. Don't mention hotels, restaurants, prices, opening hours or the weather (the page shows the forecast). Don't "
    "use marketing words like 'breathtaking', 'nestled' or 'must-see'."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string", "description": "Where it is, broad to narrow, e.g. 'Yunnan · Diqing Tibetan Prefecture'"},
        "lede": {"type": "string", "description": "One sentence on what the place is, under 140 characters"},
        "facts": {
            "type": "array", "description": "3 to 5 short facts: elevation, population, what it's known for, the local language or dish",
            "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                      "required": ["label", "value"], "additionalProperties": False},
        },
        "history": {
            "type": "array", "description": "1 or 2 paragraphs of history, each under 90 words",
            "items": {"type": "string"},
        },
        "decade": {
            "type": "array", "description": "2 to 4 things that happened there in the last ten years, oldest first",
            "items": {"type": "object", "properties": {"year": {"type": "string"}, "text": {"type": "string"}},
                      "required": ["year", "text"], "additionalProperties": False},
        },
        "wonders": {
            "type": "array", "description": "2 to 4 natural or scenic places in or near it, each in one sentence",
            "items": {"type": "object", "properties": {"name": {"type": "string"}, "text": {"type": "string"}},
                      "required": ["name", "text"], "additionalProperties": False},
        },
    },
    "required": ["region", "lede", "facts", "history", "decade", "wonders"],
    "additionalProperties": False,
}


def _line(text, size=400):
    return " ".join(str(text or "").split())[:size]


def prompt(place, when=None):
    local = f" ({place.name_local})" if place.name_local and place.name_local != place.name else ""
    country = f", country code {place.country}" if place.country else ""
    visit = f" The traveller is there in {when:%B %Y}." if when else ""
    return (f"Write the guide for {place.name}{local}, a {place.get_kind_display().lower()}{country}, at "
            f"{place.lat:.3f}, {place.lon:.3f}.{visit}")


def draft(place, when=None, again=False):
    """Draft (or with again=True, redraft) the guide to `place` with Claude and save it unpublished.
    Returns the PlaceGuide, or None when Claude didn't answer."""
    found = claude_read.answer("guide", SCHEMA, prompt(place, when), SYSTEM, reuse=not again)
    if not found.get("lede"):
        return None
    pairs = lambda rows, a, b: "\n".join(f"{_line(row.get(a), 60)}: {_line(row.get(b))}" for row in rows[:6]
                                         if isinstance(row, dict) and row.get(a) and row.get(b))
    guide, _ = PlaceGuide.objects.get_or_create(place=place)
    guide.region = _line(found.get("region"), 120)
    guide.lede = _line(found.get("lede"), 300)
    guide.facts = pairs(found.get("facts") or [], "label", "value")
    guide.history = "\n\n".join(_line(part, 1200) for part in (found.get("history") or [])[:3] if _line(part))
    guide.decade = pairs(found.get("decade") or [], "year", "text")
    guide.wonders = pairs(found.get("wonders") or [], "name", "text")
    guide.is_published = False
    guide.drafted_at, guide.drafted_by = timezone.now(), settings.CLAUDE_MODEL
    guide.save()
    return guide


def pinyin(place):
    """'Xiānggélǐlā' for a Chinese name, or ''."""
    import re

    if not re.search(r"[一-鿿]", place.name_local or ""):
        return ""
    from pypinyin import Style, lazy_pinyin

    spelled = "".join(lazy_pinyin(place.name_local, style=Style.TONE))
    return spelled[:1].upper() + spelled[1:]


def viewable(guide, staff):
    """Whether this viewer may see the guide: published, or a draft for the owner."""
    return bool(guide) and not guide.is_empty and (guide.is_published or staff)
