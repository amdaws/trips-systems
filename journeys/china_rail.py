"""China Railway trains between two stations on a day, from 12306, China Railway's own booking site.

12306 publishes no API, but its timetable-and-tickets search (leftTicket/query…) answers plain
requests once a visit's cookies are set, for the days tickets are on sale: about 15 days ahead.
For a later day the trains on the same day of the week within that window are shown instead
(usually the same timetable), with when tickets open. Searches are cached for a few minutes
and paced, so the site isn't troubled. Trip.com blocks servers with a captcha, so it's offered
only as a link for booking.

Station names and codes come from 12306's station list (station_name.js), kept for a week. The
English names are those of the OpenStreetMap stations already loaded as Places where there's
one, else spelled from the pinyin ("Guangzhou South", "Xi'an North").

Only the two stations and the date are sent to 12306, never anything about the visitor.
"""

import http.cookiejar
import json
import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache

from .http import USER_AGENT

log = logging.getLogger(__name__)

BASE = "https://kyfw.12306.cn/otn"
STATIONS_URL = f"{BASE}/resources/js/framework/station_name.js"
CHINA = ZoneInfo("Asia/Shanghai")
SALE_DAYS = 15  # 12306 sells tickets this many days ahead, today included
STATIONS_DAYS = 7  # the station list is fetched again when this old
SESSION_SECS = 20 * 60  # a visit's cookies are reused this long
CACHE_SECS = 10 * 60
PACE_SECS = 1.0  # at least this long between requests to 12306
DIRECTIONS = {"东": "East", "南": "South", "西": "West", "北": "North"}
# Where each class's seats-left is in a result row, in the order the classes are shown.
SEATS = [(32, "Business"), (25, "Premium"), (31, "First class"), (30, "Second class"), (21, "Deluxe soft sleeper"),
         (23, "Soft sleeper"), (33, "High-speed sleeper"), (28, "Hard sleeper"), (24, "Soft seat"), (29, "Hard seat"), (26, "Standing")]
KINDS = {"G": "High-speed", "D": "Bullet train", "C": "Intercity", "Z": "Direct express", "T": "Express", "K": "Fast", "S": "Suburban", "Y": "Tourist"}
NUMBER = re.compile(r"^[GDCZTKSLY]?\d{1,5}$")
# Names the pinyin and OpenStreetMap don't give in English: (name, lat, lon).
KNOWN = {"香港西九龙": ("Hong Kong West Kowloon", 22.30413, 114.16564), "香港": ("Hong Kong", 22.30413, 114.16564)}


class RailError(Exception):
    """Why trains can't be shown, in words for the visitor."""


# ---------- stations ----------

_stations = {"loaded": None, "list": [], "by_code": {}}


def _stations_path():
    return Path(settings.TRANSIT_DIR) / "china-stations.json"


def _spell(chars):
    """'西安' -> "Xi'an": pinyin as one word, with an apostrophe where a syllable starts with a vowel."""
    from pypinyin import lazy_pinyin

    word = ""
    for syllable in lazy_pinyin(chars):
        if word and syllable[:1] in "aeo":
            word += "'"
        word += syllable
    return word[:1].upper() + word[1:]


def english_name(local, city_local=""):
    """'广州南' -> 'Guangzhou South', '上海虹桥' (city 上海) -> 'Shanghai Hongqiao', '江北' -> 'Jiangbei'."""
    if len(local) > 2 and local[-1] in DIRECTIONS:
        return f"{english_name(local[:-1], city_local)} {DIRECTIONS[local[-1]]}"
    if city_local and local.startswith(city_local) and local != city_local:
        return f"{_spell(city_local)} {_spell(local[len(city_local):])}"
    return _spell(local)


def _known_names():
    """{Chinese name: (English name, lat, lon)} from the stations already loaded as Places."""
    from .models import Place

    names = dict(KNOWN)
    stations = Place.objects.filter(kind=Place.Kind.STATION, country__in=["CN", "HK"]).exclude(name_local="").order_by("id")
    for local, name, lat, lon in stations.values_list("name_local", "name", "lat", "lon"):
        name = re.sub(r"'([A-Z])", lambda m: "'" + m[1].lower(), name)  # "Xi'An" -> "Xi'an"
        name = re.sub(r"^(\w+)-(\w+)\b", r"\1 \2", name)  # "Shanghai-Hongqiao" -> "Shanghai Hongqiao"
        names.setdefault(local.removesuffix("站"), (name, round(lat, 5), round(lon, 5)))
    return names


def parse_stations(text):
    """12306's station list ('@bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||@…') as dicts."""
    known = _known_names()
    found = []
    for entry in text.split("@")[1:]:
        fields = entry.split("|")
        if len(fields) < 8 or not re.fullmatch(r"[A-Z]{3}", fields[2]):
            continue
        local, city_local = fields[1].strip(), fields[7].strip()
        place = known.get(local)
        found.append({
            "code": fields[2], "local": local, "pinyin": fields[3].lower(), "abbr": fields[4].lower(),
            "city_local": city_local, "name": place[0] if place else english_name(local, city_local),
            "city": (known[city_local][0] if city_local in known else english_name(city_local)) if city_local else "",
            "lat": place[1] if place else None, "lon": place[2] if place else None,
        })
    return found


def refresh_stations():
    """Fetch 12306's station list and keep it (beside the city timetables). Returns how many."""
    request = urllib.request.Request(STATIONS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read(2_000_000).decode("utf-8")
    found = parse_stations(text)
    if len(found) < 1000:
        raise RailError(f"12306's station list looks wrong ({len(found)} stations).")
    path = _stations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(found, ensure_ascii=False))
    temporary.replace(path)
    return len(found)


def stations_due():
    path = _stations_path()
    return not path.exists() or time.time() - path.stat().st_mtime > STATIONS_DAYS * 86400


def stations():
    """Every station 12306 sells tickets for, fetched the first time it's needed."""
    path = _stations_path()
    if not path.exists():
        refresh_stations()
    modified = path.stat().st_mtime
    if _stations["loaded"] != modified:
        found = json.loads(path.read_text())
        _stations.update(loaded=modified, list=found, by_code={s["code"]: s for s in found})
    return _stations["list"]


def station(code):
    stations()
    return _stations["by_code"].get(str(code).upper())


def _plain(text):
    text = re.sub(r"(?i)\b(railway|train)?\s*station\b|站$", "", text.strip())
    return re.sub(r"[\s'’\-]", "", text).lower()


def find_stations(query, limit=8):
    """Stations matching what was typed: English, pinyin, initials or Chinese; a city's main station first."""
    wanted = _plain(query)
    if not wanted:
        return []
    found = []
    for index, s in enumerate(stations()):
        english = _plain(s["name"])
        names = (s["local"], english, s["pinyin"], s["abbr"], s["code"].lower())
        if wanted in names:
            score = 0
        elif any(name.startswith(wanted) for name in names[:3]):
            score = 1
        elif any(wanted in name for name in names[:3]):
            score = 2
        elif _plain(s["city"]).startswith(wanted):
            score = 3  # "Hong Kong" finds West Kowloon
        else:
            continue
        found.append((score, s["local"] != s["city_local"], len(s["local"]), index, s))
    found.sort(key=lambda row: row[:4])
    return [_public(row[4]) for row in found[:limit]]


def _public(s):
    return {"code": s["code"], "name": s["name"], "local": s["local"], "city": s["city"], "lat": s.get("lat"), "lon": s.get("lon")}


# ---------- trains ----------

_session = {"opener": None, "made": 0.0, "endpoint": "leftTicket/queryG"}
_lock = threading.Lock()
_last_call = [0.0]


def _opener(fresh=False):
    if fresh or not _session["opener"] or time.monotonic() - _session["made"] > SESSION_SECS:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [("User-Agent", USER_AGENT)]
        opener.open(f"{BASE}/leftTicket/init", timeout=15).read()  # sets the visit's cookies
        _session.update(opener=opener, made=time.monotonic())
    return _session["opener"]


def _fetch(params, fresh=False):
    with _lock:
        wait = PACE_SECS - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            opener = _opener(fresh)
            request = urllib.request.Request(f"{BASE}/{_session['endpoint']}?{params}", headers={
                "Referer": f"{BASE}/leftTicket/init", "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
            with opener.open(request, timeout=15) as response:
                body = response.read(5_000_000)
        finally:
            _last_call[0] = time.monotonic()
    try:
        return json.loads(body)
    except ValueError:
        return None  # an error page: usually the visit has expired


def _query(origin, destination, day):
    params = urllib.parse.urlencode([("leftTicketDTO.train_date", day.isoformat()), ("leftTicketDTO.from_station", origin),
                                     ("leftTicketDTO.to_station", destination), ("purpose_codes", "ADULT")])
    for attempt in range(3):
        try:
            data = _fetch(params, fresh=attempt > 0)
        except Exception as exc:
            log.warning("12306 search failed: %s", type(exc).__name__)
            data = None
        if isinstance(data, dict) and data.get("c_url") and data["c_url"] != _session["endpoint"]:
            _session["endpoint"] = data["c_url"]  # 12306 renames its search now and then, and says so
            continue
        if isinstance(data, dict) and data.get("status") and isinstance(data.get("data"), dict):
            return data["data"]
    raise RailError("China Railway's site (12306) isn't answering right now. Please try again in a minute.")


def _seats(value):
    value = value.strip()
    if value in ("", "--"):
        return None
    if value == "有":
        return "Available"
    if value == "无":
        return "Sold out"
    if value == "*":
        return "Not on sale yet"
    if value == "候补":
        return "Waitlist"
    return f"{value} left" if value.isdigit() else value


def _minutes(text):
    hours, _, minutes = text.partition(":")
    return int(hours) * 60 + int(minutes)


def _train(fields, names):
    if len(fields) < 34 or not NUMBER.match(fields[3]) or "停运" in fields[1]:
        return None  # not running that day
    try:
        depart, duration = _minutes(fields[8]), _minutes(fields[10])
    except ValueError:
        return None
    if duration >= 99 * 60:
        return None
    seats = [{"class": label, "left": left} for index, label in SEATS if (left := _seats(fields[index]))]

    def name(code):
        found = station(code)
        return found["name"] if found else names.get(code, code)

    opens = re.search(r"(\d{1,2})点(?:(\d{1,2})分)?起售", fields[1])
    return {
        "number": fields[3], "kind": KINDS.get(fields[3][:1], "Train"), "high_speed": fields[3][:1] in "GDC",
        "from_code": fields[6], "to_code": fields[7], "from": name(fields[6]), "to": name(fields[7]),
        "from_local": names.get(fields[6], ""), "to_local": names.get(fields[7], ""),
        "start": name(fields[4]), "end": name(fields[5]),
        "depart": fields[8], "arrive": fields[9], "days": (depart + duration) // 1440,
        "minutes": duration, "duration": f"{duration // 60} h {duration % 60:02d} min" if duration >= 60 else f"{duration} min",
        "bookable": fields[11] == "Y", "opens": f"{int(opens[1]):02d}:{int(opens[2] or 0):02d}" if opens else "",
        "seats": seats,
    }


def china_today():
    return datetime.now(CHINA).date()


def sale_day(day):
    """The first day tickets for a train on this day are sold (in China's time)."""
    return day - timedelta(days=SALE_DAYS - 1)


def trains(origin, destination, day, today=None):
    """The trains from one station to another on a day (date), soonest first, with seats left.
    12306 also lists trains from and to the cities' other stations; each train says which."""
    a, b = station(origin), station(destination)
    if not a or not b:
        raise RailError("Choose both stations from the list.")
    if a["code"] == b["code"]:
        raise RailError("Choose two different stations.")
    today = today or china_today()
    if day < today:
        raise RailError("Choose a date from today on.")
    if day > today + timedelta(days=400):
        raise RailError("Choose a date within the next year.")
    last = today + timedelta(days=SALE_DAYS - 1)
    shown = day if day <= last else day - timedelta(weeks=math.ceil((day - last).days / 7))
    key = f"china-rail:{a['code']}:{b['code']}:{shown.isoformat()}"
    data = cache.get(key)
    if data is None:
        data = _query(a["code"], b["code"], shown)
        cache.set(key, data, CACHE_SECS)
    names = data.get("map") or {}
    found = [t for row in data.get("result") or [] if isinstance(row, str) and (t := _train(row.split("|"), names))]
    found.sort(key=lambda t: (t["depart"], t["minutes"]))
    note = ""
    if shown != day:
        note = (f"Tickets for {day:%A %-d %B} go on sale on {sale_day(day):%A %-d %B} (12306 sells {SALE_DAYS} days ahead; "
                f"Trip.com takes bookings earlier). These are the trains on {shown:%A %-d %B}, the same day of the week, "
                "so the timetable could still change.")
    return {"from": _public(a), "to": _public(b), "day": day.isoformat(), "day_label": f"{day:%A %-d %B %Y}",
            "shown": shown.isoformat(), "shown_label": f"{shown:%A %-d %B}", "projected": shown != day, "note": note,
            "on_sale": sale_day(day).isoformat(), "trains": found,
            "book": f"https://au.trip.com/trains/china/route/{_slug(a['city'] or a['name'])}-to-{_slug(b['city'] or b['name'])}/"}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower().replace("'", "")).strip("-")


def parse_day(text):
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None
