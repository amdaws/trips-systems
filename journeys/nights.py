"""Where the trip sleeps each night, worked out from its stays: the trip pages' point of reference
for "Overnight in…", the overnight stops (with their weather), the map's numbered stops and the
activity days. (Days used to carry an "overnight city" of their own; it's kept but not used.)

Every viewer sees each night's city, whether or not the stay itself is public; the hotel's name,
address and exact position stay with the stay and its own visibility.
"""

from datetime import timedelta


def city_of(stay):
    """(name, local name, Place or None) for the city a stay is in: its city, else the city its hotel
    is in (named only: the hotel's own position isn't a city's), else its label."""
    if stay.city:
        return stay.city.name, stay.city.name_local, stay.city
    if stay.location and stay.location.city:
        return stay.location.city, stay.location.city_local, None
    return stay.label, "", None


def by_night(trip, stays=None):
    """{date: stay} for every night a stay covers; where two overlap, the one checked into first."""
    nights = {}
    stays = stays if stays is not None else trip.stays.select_related("city", "location").order_by("check_in_date", "id")
    for stay in stays:
        for offset in range(max(0, stay.nights)):
            nights.setdefault(stay.check_in_date + timedelta(days=offset), stay)
    return nights


def night_names(trip):
    """{date: {"name", "local"}}: the city slept in each night, for labels."""
    return {day: dict(zip(("name", "local"), city_of(stay)[:2])) for day, stay in by_night(trip).items()}


def stops(trip):
    """Consecutive nights in one city, in order: [{"name", "local", "place", "first", "last", "stays"}].
    place is the city's Place (for the map and the weather), or None if the stay has no city."""
    result = []
    for night, stay in sorted(by_night(trip).items()):
        name, local, place = city_of(stay)
        key = place.pk if place else name.lower()
        last = result[-1] if result else None
        if last and last["key"] == key and last["last"] == night - timedelta(days=1):
            last["last"] = night
            if stay not in last["stays"]:
                last["stays"].append(stay)
        else:
            result.append({"key": key, "name": name, "local": local, "place": place, "first": night, "last": night, "stays": [stay]})
    return result
