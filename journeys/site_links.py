"""What Booking.com, Klook, TripAdvisor and Viator links say about themselves.

These sites don't let servers read their pages, so the planner doesn't try: it reads the link's
own address instead (the place's name, its city, and for Booking.com the dates searched), and
the views then find the place with OpenStreetMap search for its map pin and address. Pasting
the page's text fills in the rest, and the sites' confirmation emails are read by booking_import.
Nothing is fetched from the sites themselves.
"""

import re
import urllib.parse
from datetime import date

COUNTRIES = {
    "au": "Australia", "nz": "New Zealand", "cn": "China", "hk": "Hong Kong", "mo": "Macau", "tw": "Taiwan", "jp": "Japan",
    "kr": "South Korea", "sg": "Singapore", "th": "Thailand", "vn": "Vietnam", "my": "Malaysia", "id": "Indonesia",
    "ph": "Philippines", "gb": "United Kingdom", "us": "United States", "fr": "France", "it": "Italy", "es": "Spain",
    "de": "Germany", "ca": "Canada",
}
# The state or country TripAdvisor adds after a city ("Sydney_New_South_Wales"), longest first.
REGIONS = sorted([
    "New South Wales", "Victoria", "Queensland", "Western Australia", "South Australia", "Tasmania", "Northern Territory",
    "Australian Capital Territory", "New Zealand", "North Island", "South Island", "China", "Hong Kong", "Macau", "Taiwan",
    "Japan", "Guangdong", "Guangxi", "Sichuan", "Yunnan", "Shaanxi", "Hunan", "Hubei", "Fujian", "Zhejiang", "Jiangsu",
    "Shandong", "Hebei", "Henan", "Anhui", "Jiangxi", "Guizhou", "Hainan", "Liaoning", "Jilin", "Heilongjiang", "Shanxi",
    "Inner Mongolia", "Gansu", "Qinghai", "Ningxia", "Xinjiang", "Tibet", "Chongqing", "Shanghai", "Beijing", "Tianjin",
    "England", "Scotland", "Wales", "United Kingdom", "United States", "France", "Italy", "Spain", "Germany", "Singapore",
    "Thailand", "Vietnam", "Malaysia", "Indonesia", "Philippines", "South Korea", "Canada",
], key=len, reverse=True)
TRANSPORT_WORDS = re.compile(r"(?i)\b(airport[- ]transfers?|transfers?|private[- ]car|car[- ]charter|chauffeur|shuttle|"
                             r"train|rail|jr[- ]pass|high[- ]speed|bullet|bus|coach|ferry|cruise[- ]ticket|taxi)\b")


def site_of(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host == "booking.com" or host.endswith(".booking.com"):
        return "booking.com"
    if host == "klook.com" or host.endswith(".klook.com"):
        return "klook.com"
    if re.search(r"(^|\.)tripadvisor\.[a-z.]+$", host):
        return "tripadvisor"
    if host == "viator.com" or host.endswith(".viator.com"):
        return "viator.com"
    return ""


def _words(slug):
    """'the-langham-sydney' -> 'The Langham Sydney' (TripAdvisor's 'Sydney_Opera_House' too)."""
    text = urllib.parse.unquote(slug).replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    small = {"and", "of", "the", "at", "in", "on", "to", "by", "with", "a", "for"}
    upper = {"jr", "mrt", "mtr", "cbd", "nsw", "qld", "ktx", "hsr", "uk", "usa", "nz", "vip"}
    words = text.split(" ")
    return " ".join(word.upper() if word.lower() in upper else word if word.isupper()
                    else word.capitalize() if index == 0 or word.lower() not in small else word.lower()
                    for index, word in enumerate(words) if word)


def _iso(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _split_place(place):
    """'Sydney New South Wales' -> ('Sydney', 'New South Wales'); 'Brisbane Brisbane Region Queensland' -> ('Brisbane', …)."""
    city, region = place, ""
    for candidate in REGIONS:
        if place.lower().endswith(" " + candidate.lower()):
            city, region = place[: -len(candidate) - 1].strip(), candidate
            break
        if place.lower() == candidate.lower():
            return place, ""
    city = re.sub(r"\s+(?:\w+\s+){0,2}Region$", "", city)  # TripAdvisor's "… Brisbane Region"
    words = city.split(" ")
    half = len(words) // 2
    if len(words) % 2 == 0 and words[:half] == words[half:]:
        city = " ".join(words[:half])
    return city, region


def read(url):
    """{'site', 'kind' ('stay', 'activity' or 'transport'), 'name', 'city', 'region', 'country',
    'check_in_date', 'check_out_date', 'adults', 'mode', 'origin', 'destination', 'url'} (what's
    known) for a Booking.com, Klook, TripAdvisor or Viator link, or None for any other link."""
    site = site_of(url)
    if not site:
        return None
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parts.path)
    query = {key: values[0] for key, values in urllib.parse.parse_qs(parts.query).items()}
    found = {"site": site, "url": url}

    if site == "booking.com":
        hotel = re.search(r"/hotel/([a-z]{2})/([^/.]+)", path)
        attraction = re.search(r"/attractions/([a-z]{2})/[a-z0-9]+-([^/.]+)", path)
        if hotel:
            found.update(kind="stay", name=_words(hotel[2]), country=hotel[1].upper())
        elif attraction:
            found.update(kind="activity", name=_words(attraction[2]), country=attraction[1].upper())
        elif "/taxi" in path or "/cars" in path:
            found.update(kind="transport", mode="car")
        else:
            return None
        check_in = _iso(query.get("checkin")) or _iso("-".join(query.get(k, "") for k in ("checkin_year", "checkin_month", "checkin_monthday")).replace("--", ""))
        check_out = _iso(query.get("checkout")) or _iso("-".join(query.get(k, "") for k in ("checkout_year", "checkout_month", "checkout_monthday")).replace("--", ""))
        if check_in and check_out and check_out > check_in:
            found.update(check_in_date=check_in, check_out_date=check_out)
        if str(query.get("group_adults", "")).isdigit():
            found["adults"] = int(query["group_adults"])

    elif site == "tripadvisor":
        page = re.search(r"/(Attraction|Hotel|Restaurant|AttractionProduct|VacationRental)_?Review-g\d+-d\d+(?:-Reviews)?-([^-]+)-([^.]+)\.html", path)
        if not page:
            return None
        city, region = _split_place(_words(page[3]))
        found.update(kind="stay" if page[1] in ("Hotel", "VacationRental") else "activity", name=_words(page[2]), city=city, region=region)

    elif site == "viator.com":
        tour = re.search(r"/tours/([^/]+)/([^/]+)/", path)
        if not tour:
            return None
        found.update(kind="activity", name=_words(tour[2]), city=_words(tour[1]))

    else:  # Klook
        page = re.search(r"/(activity|hotels/detail|event)/\d+-([^/]+)", path)
        if page:
            words = _words(page[2]).split(" ")
            if len(words) > 2 and words[-1].lower() in (w.lower() for w in words[:-1]):
                words = words[:-1]  # Klook ends many names with the city again: "Sydney Harbour Bridge Climb Sydney"
            found["name"] = " ".join(words)
            found["kind"] = "stay" if page[1] == "hotels/detail" else "activity"
        elif re.search(r"/(airport-transfers|car-rentals|rails?|transport|bus|ferry)", path):
            found.update(kind="transport", name=_words(path.rstrip("/").rsplit("/", 1)[-1]))
        else:
            return None
        if found["kind"] == "activity" and TRANSPORT_WORDS.search(found["name"]):
            found["kind"] = "transport"

    if found.get("kind") == "transport":
        from .leg_import import mode_for, route_from_title  # (leg_import uses this module)

        title = found.get("name", "")
        if re.search(r"(?i)\b(train|rail|jr[- ]pass|high[- ]speed|bullet)\b", title):
            found.setdefault("mode", "train")
        elif re.search(r"(?i)\b(ferry|cruise[- ]ticket)\b", title):
            found.setdefault("mode", "ferry")
        found["mode"] = found.get("mode") or mode_for(title) or "car"
        found["origin"], found["destination"], _ = route_from_title(re.sub(r"(?i)\bairport\b(?=\s+(?:to|-))", "Airport", title))
        found["operator"] = {"klook.com": "Klook", "booking.com": "Booking.com"}.get(site, "")
    if found.get("country") and not found.get("region"):
        found["region"] = COUNTRIES.get(found["country"].lower(), "")
    return found


TRIPCOM_PAGES = {"travel-guide", "attraction", "things-to-do", "detail", "tourist-attractions", "list", "en-au", "en-us", "en-gb"}


def is_tripcom(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return bool(re.search(r"(^|\.)(trip|ctrip)\.com$", host))


def tripcom_activity(url):
    """What a Trip.com sight or tour link says about itself, for when Trip.com turns the server away:
    "/travel-guide/attraction/beijing/the-palace-museum-75595/" -> {"name": "The Palace Museum",
    "city": "Beijing"}. A tour link with only a number ("/things-to-do/detail/36394123/") gives no
    name. {} for anything that isn't a Trip.com link."""
    if not is_tripcom(url):
        return {}
    found = {"site": "trip.com", "url": url, "kind": "activity"}
    segments = [s for s in urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower().split("/") if s]
    for index in range(len(segments) - 1, -1, -1):
        # The name, then Trip.com's number for it: "chongqing-1949-129377915" is "Chongqing 1949".
        page = re.fullmatch(r"([a-z][a-z0-9'-]*?)-(\d{3,})", segments[index])
        if page and page[1] not in TRIPCOM_PAGES:
            found["name"] = _words(page[1])
            city = segments[index - 1] if index else ""
            city = re.sub(r"-?\d+$", "", city)
            if city and city not in TRIPCOM_PAGES and re.fullmatch(r"[a-z][a-z-]*", city):
                found["city"] = _words(city)
            break
    if found.get("name") and TRANSPORT_WORDS.search(found["name"]):
        found["kind"] = "transport"
    return found


GENERIC = re.compile(r"(?i)\b(private|small[- ]group|group|guided|day|half[- ]day|full[- ]day|tours?|trips?|tickets?|"
                     r"admission|entry|entrance|passes?|experience|skip[- ]the[- ]line|e-?ticket|with|including|and)\b.*$")


def search_texts(found, near="", most=5):
    """Ways to ask the map search for the place, most exact first: as named, without tour words
    ("Great Wall at Mutianyu Private Tour" -> "Great Wall at Mutianyu"), without the city in the
    name ("Rydges Southbank Brisbane" -> "Rydges Southbank"), shorter ("Sydney Harbour Bridge Climb"
    -> "Sydney Harbour Bridge"), and finally the name on its own. At most `most`: OpenStreetMap's
    search allows one a second."""
    name = found.get("name", "")
    plain = GENERIC.sub("", name).strip(" -,") or name
    city = found.get("city") or near
    names = [name, plain]
    if city:
        names.append(re.sub(rf"(?i)\s*\b{re.escape(city)}\b\s*", " ", plain).strip())
    words = names[-1].split(" ")
    names += [" ".join(words[:count]) for count in range(len(words) - 1, 1, -1)]
    texts = [search_text(dict(found, name=candidate), near) for candidate in names if candidate]
    texts.append(plain)
    return list(dict.fromkeys(text for text in texts if text))[:most]


def search_text(found, near=""):
    """What to ask the map search for: the name with its city or country (or the day's city, `near`)."""
    place = found.get("city") or near
    name = found.get("name", "")
    bits = [name]
    if place and place.lower() not in name.lower():
        bits.append(place)
    if found.get("region") and found["region"].lower() not in " ".join(bits).lower():
        bits.append(found["region"])
    return ", ".join(bit for bit in bits if bit)
