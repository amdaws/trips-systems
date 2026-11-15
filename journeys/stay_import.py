"""Read a stay's details from a hotel page: a link (Trip.com, Booking.com, hotel websites…) or pasted page text.

Most hotel pages carry schema.org "Hotel" data (name, address, rating, sometimes
check-in times and phone), either as JSON-LD or tucked inside the page's scripts
(Trip.com nests it in escaped strings). Trip.com also includes coordinates, phone,
email and its check-in policy. Anything still missing is looked for in the text.
"""

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import page_details

MAX_BYTES = 4_000_000
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
LODGING_TYPES = ("Hotel", "Hostel", "LodgingBusiness", "Motel", "Resort", "BedAndBreakfast", "Apartment",
                 "VacationRental", "Campground", "GuestHouse", "LocalBusiness", "Accommodation")


class PageError(Exception):
    """A friendly reason the page couldn't be read."""


class BlockedError(PageError):
    """The site turned the server away (an error such as 403 or 432, or a robot check) rather than the
    page having nothing on it. Readers can fall back on what the link's own address says."""


# ---------- fetching (only public web addresses) ----------


GOOGLE = re.compile(r"(^|\.)(google\.[a-z.]+|goo\.gl|g\.page|g\.co|googleusercontent\.com|gstatic\.com|googleapis\.com)$")
NO_GOOGLE = "Google links aren't used here. Use an OpenStreetMap or Apple Maps link, a Wikipedia article, or the place's own website."


def is_google(url):
    """Whether a link goes to Google (Google Maps included): the planner uses no Google services."""
    return bool(GOOGLE.search((urllib.parse.urlsplit(url).hostname or "").lower()))


def _check_public(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PageError("Use a full web address starting with https://")
    if is_google(url):
        raise PageError(NO_GOOGLE)
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except socket.gaierror:
        raise PageError("That website couldn't be found.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise PageError("That address isn't a public website.")


def _peer_is_public(response):
    """Whether the connection really went to a public address. The name is checked before connecting,
    but a hostile domain could answer differently the second time (DNS rebinding); this closes that."""
    try:
        address = response.fp.raw._sock.getpeername()[0]
    except (AttributeError, OSError, IndexError):
        return False
    return ipaddress.ip_address(address.split("%")[0]).is_global


def _inflate(data, encoding):
    """Decompress a gzip or deflate body, refusing one that grows past MAX_BYTES (a compression bomb)."""
    for wbits in ((31,) if encoding == "gzip" else (15, -15)):
        inflater = zlib.decompressobj(wbits)
        try:
            body = inflater.decompress(data, MAX_BYTES)
        except zlib.error:
            continue
        if inflater.unconsumed_tail:
            raise PageError("That page is too large to read.")
        return body
    raise PageError("That page couldn't be read.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_page(url):
    """GET a public web page (following up to 4 redirects, each checked), returning its text."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _hop in range(5):
        _check_public(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-AU,en;q=0.9", "Accept-Encoding": "gzip, deflate",
        })
        try:
            response = opener.open(request, timeout=15)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                continue
            if exc.code in (403, 429, 432, 503):
                raise BlockedError("That site blocks automatic reading. Paste the page text below instead.")
            raise PageError(f"The site answered with an error ({exc.code}).")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PageError(f"The site didn't respond ({getattr(exc, 'reason', exc)}).")
        with response:
            if not _peer_is_public(response):
                raise PageError("That address isn't a public website.")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise PageError("That page is too large to read.")
            encoding = response.headers.get("Content-Encoding", "")
            if encoding in ("gzip", "deflate"):
                data = _inflate(data, encoding)
            charset = response.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")
    raise PageError("That link redirects too many times.")


# ---------- extraction ----------


def _unescape_layers(text, layers=4):
    """Undo the escaping when JSON sits inside strings inside scripts (\\\" -> \")."""
    for _ in range(layers):
        text = text.replace("\\\\", "\\").replace('\\"', '"').replace("\\/", "/").replace("\\u0026", "&")
    return text


def _enclosing_object(text, index):
    """The JSON object (as text) that contains position `index`, found by matching braces."""
    depth, start = 0, None
    for i in range(index, max(index - 20000, -1), -1):
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    depth, in_string, escaped = 0, False, False
    for j in range(start, min(start + 60000, len(text))):
        ch = text[j]
        if in_string:
            escaped = (ch == "\\") and not escaped
            if ch == '"' and not escaped:
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return None


def _lodging_objects(text):
    pattern = re.compile(r'"@type"\s*:\s*\[?\s*"(%s)"' % "|".join(LODGING_TYPES))
    for match in pattern.finditer(text):
        raw = _enclosing_object(text, match.start())
        if raw:
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def _time(value):
    match = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    return f"{int(match[1]):02d}:{match[2]}" if match and int(match[1]) < 24 else ""


def _address(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        street = (value.get("streetAddress") or "").strip()
        parts = [value.get(k, "") for k in ("addressLocality", "addressRegion", "postalCode", "addressCountry")]
        if isinstance(parts[3], dict):
            parts[3] = parts[3].get("name", "")
        extra = [str(p).strip() for p in parts if p and str(p).strip() and str(p).strip() not in street]
        return ", ".join([street] + extra) if street else ", ".join(extra)
    return ""


def _first(pattern, text, flags=re.I):
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""


def datum(text, url=""):
    """The map system a page's coordinates are on: Trip.com and Ctrip give mainland China positions
    on China's offset system (GCJ-02, or Baidu's BD-09); see china_coords."""
    map_type = _first(r'"mapType"\s*:\s*"(gg|gd|bd)"', text)
    if map_type == "bd":
        return "bd09"
    host = (urllib.parse.urlsplit(url).hostname or "") if url else ""
    if map_type or re.search(r"(^|\.)(trip|ctrip)\.com$", host):
        return "gcj02"
    return "wgs84"


def extract(page, url=""):
    """Stay details from a page's HTML or pasted text. Keys are only present when found."""
    text = _unescape_layers(page)
    found = {}

    def add(key, value):
        if value and not found.get(key):
            found[key] = value

    objects = list(_lodging_objects(text))
    for obj in objects:
        add("name", (obj.get("name") or "").strip())
        add("address", _address(obj.get("address")))
        if isinstance(obj.get("address"), dict):
            add("city", (obj["address"].get("addressRegion") or obj["address"].get("addressLocality") or "").strip())
        add("phone", (obj.get("telephone") or "").strip())
        add("email", (obj.get("email") or "").replace("mailto:", "").strip())
        add("check_in_time", _time(obj.get("checkinTime")))
        add("check_out_time", _time(obj.get("checkoutTime")))
        geo = obj.get("geo") or {}
        if geo.get("latitude") and geo.get("longitude") and not found.get("lat"):
            found["lat"], found["lon"] = geo["latitude"], geo["longitude"]
        rating = obj.get("aggregateRating") or {}
        if rating.get("ratingValue"):
            add("rating", f"{rating['ratingValue']}/{rating.get('bestRating', 10)} ({rating.get('reviewCount', '?')} reviews)")

    # Trip.com's own page data.
    lat = _first(r'"lat"\s*:\s*"?(-?\d+\.\d+)', text)
    lng = _first(r'"lng"\s*:\s*"?(-?\d+\.\d+)', text) or _first(r'"lon"\s*:\s*"?(-?\d+\.\d+)', text)  # Trip.com's, else Wikipedia's
    if lat and lng and not found.get("lat"):
        found["lat"], found["lon"] = lat, lng
    add("phone", _first(r'"tels"\s*:\s*\[\s*\{\s*"show"\s*:\s*"([^"]+)"', text))
    add("email", _first(r'"email"\s*:\s*"([^"@\s]+@[^"\s]+\.[a-z]{2,})"', text))

    # Plain text: "Check-in: After 15:00", "check-in starts from 15:00", "Check-out until 11:00".
    plain = re.sub(r"<[^>]+>", " ", text)
    add("check_in_time", _time(_first(r"check[- ]?in[^0-9\n]{0,40}?(\d{1,2}:\d{2})", plain)))
    add("check_out_time", _time(_first(r"check[- ]?out[^0-9\n]{0,40}?(\d{1,2}:\d{2})", plain)))
    add("email", _first(r"\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b", plain))
    add("phone", _first(r"(?:phone|tel(?:ephone)?|contact)\s*[:：]?\s*(\+?\d[\d\s()\-]{6,}\d)", plain))
    title = _first(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page) or _first(r"<title>([^<]+)</title>", page)
    add("name", re.split(r"\s+[|\-–]\s+", title)[0].strip() if title else "")
    add("address", _first(r"address\s*[:：]\s*([^\n]{8,160})", plain))
    rough = page_details.price(objects, page, plain)  # a listing price: an estimate until it's booked
    if rough:
        add("amount", rough[0])
        add("currency", rough[1])

    if url:
        found["url"] = url
        found["site"] = urllib.parse.urlsplit(url).hostname.removeprefix("www.")
    found["datum"] = datum(text, url)
    found = {k: (str(v)[:300] if not isinstance(v, (int, float)) else v) for k, v in found.items() if v}
    if not any(k in found for k in ("address", "phone", "check_in_time", "lat")):
        if re.search(r"challenge|captcha|verify you are human", page[:200000], re.I):
            raise BlockedError("The site showed a robot check instead of the page. Paste the page text below instead.")
        raise PageError("Couldn't find hotel details on that page. Paste the page text below instead.")
    return found
