"""Polite HTTP helpers for the open data services the planner uses."""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

USER_AGENT = "AaronDawsTrips/1.0 (+https://trips.aarondaws.com.au)"

# Public Overpass API servers (OpenStreetMap data), tried in order.
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def _request(url, data=None, timeout=30):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def get_json(url, params=None, timeout=30):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return _request(url, timeout=timeout)


def overpass(query, timeout=200):
    """Run an Overpass QL query, falling back to the next server if one is busy or down."""
    last_error = None
    for server in OVERPASS_SERVERS:
        for attempt in range(2):
            try:
                return _request(server, data={"data": query}, timeout=timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (429, 502, 503, 504):  # busy or briefly down: wait, retry, then try the next server
                    log.info("Overpass %s busy (%s); retrying", server, exc.code)
                    time.sleep(10 * (attempt + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                break
    raise RuntimeError(f"Overpass query failed: {last_error}")
