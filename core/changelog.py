"""The site's change log ("What's new"), read from changelog.toml next to this file.

Times are stored with their UTC offset (written in AEST, +10:00) and always shown in AEST,
Australian Eastern Standard Time (UTC+10, Brisbane time: no daylight saving), all year round.
"""

import tomllib
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings

SOURCE = Path(__file__).with_name("changelog.toml")
AEST = ZoneInfo("Australia/Brisbane")  # UTC+10 all year: its abbreviation is always AEST
# Each kind of change: its label and badge colour.
CATEGORIES = {"new": ("New", "badge--blue"), "improved": ("Improved", "badge--green"),
              "fixed": ("Fixed", "badge--amber"), "security": ("Security", "")}
AREAS = {"Itinerary", "Map", "Planner", "Stays", "Activities", "Site"}


def aest_time(moment, date=True):
    """'Mon 26 Oct 2026, 12:21 am AEST' (or just '12:21 am AEST')."""
    local = moment.astimezone(AEST)
    clock = f"{local:%-I:%M} {'am' if local.hour < 12 else 'pm'} {local.tzname()}"
    return f"{local:%a %-d %b %Y}, {clock}" if date else clock


@cache
def releases():
    """Every release, newest first, checked as it's read so a typo can't reach the page."""
    with SOURCE.open("rb") as file:
        data = tomllib.load(file)
    result = []
    for release in data["release"]:
        at = release["at"]
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError(f"{release['title']}: 'at' needs a date, time and UTC offset")
        for item in release["items"]:
            if item["category"] not in CATEGORIES or item["area"] not in AREAS:
                raise ValueError(f"{release['title']}: unknown category or area in {item}")
        result.append({
            "at": at, "title": release["title"],
            "items": [dict(item, label=CATEGORIES[item["category"]][0], badge=CATEGORIES[item["category"]][1])
                      for item in release["items"]],
        })
    return sorted(result, key=lambda release: release["at"], reverse=True)


def by_day():
    """Releases grouped by their day in AEST, newest day first, each with its time shown."""
    days = []
    for release in releases():
        local = release["at"].astimezone(AEST)
        if not days or days[-1]["date"] != local.date():
            days.append({"date": local.date(), "label": f"{local:%A %-d %B %Y}", "releases": []})
        days[-1]["releases"].append(dict(release, time=aest_time(release["at"], date=False), iso=local.isoformat()))
    return days


def counts():
    """How many changes of each kind, in the order the categories are listed."""
    tally = dict.fromkeys(CATEGORIES, 0)
    for release in releases():
        for item in release["items"]:
            tally[item["category"]] += 1
    return [{"label": CATEGORIES[key][0], "badge": CATEGORIES[key][1], "count": tally[key]} for key in CATEGORIES if tally[key]]


def deployed_at():
    """When the code on this server was deployed (deploy.sh writes the file), or None."""
    try:
        text = Path(settings.DEPLOYED_AT_FILE).read_text().strip()
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
