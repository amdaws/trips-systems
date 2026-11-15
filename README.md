# trips.aarondaws.com.au

Django 6.1 journey planner for Aaron Daws Trips.

- **Public site:** the home page lists published trips (or shows "in development"
  when there are none). Each trip page has a route map (flights, trains on real
  track, boats, road and metro legs, plus a railway network overlay), overnight
  stops with weather, and a day-by-day plan. Public pages show the route and
  story only: no booking references, hotels, addresses, costs, private notes or
  flight/train numbers.
- **Planner** (`/manage/`, your login plus Cloudflare Access): trips, days and
  activities, transport with quick entry and automatic route tracing, stays,
  budget in any currency converted to AUD: spent is what's been paid, and the estimate is that plus
  each planned stay, transport and activity's cost (one Cost per plan: an estimate while Planned, what it
  cost once Booked; see `journeys/booking_costs.py`). Planned / Booked status (ideas were merged into
  Planned on 15 Nov 2026); Add confirmation on planned ones, and View confirmation on booked ones whose confirmation was imported by link or uploaded file (kept as a trip document); a planned one with a link has its details read from it when saved (`journeys/link_details.py`; Refresh details tries again); each
  records how it was added (booking link, import or by hand), and one filled in from a booking link
  flags details the page didn't give (`booking_costs.missing_details`). Also checklists with automatic train
  ticket reminders, private document uploads, calendar feeds and print views.

## How it runs

```
Visitor → Cloudflare (proxy, TLS, Access for /manage) → Caddy (TLS, static files) → gunicorn :8001 → Django → SQLite
                                                                         trips-jobs.timer (every 2 min) → run_jobs
```

| What | Where |
|---|---|
| Source (edit here) | `/home/ubuntu/sites/trips.aarondaws.com.au` |
| Deployed code + virtualenv | `/srv/trips/app`, `/srv/trips/venv` |
| Database and uploads | `/var/lib/trips/db.sqlite3`, `/var/lib/trips/media/` (only the `trips` user can write) |
| Settings and secrets | `/etc/trips/trips.env` (template: `deploy/trips.env.example`) |
| Static files | `/var/www/trips.aarondaws.com.au/static` (served by Caddy) |
| Services | `trips.service` (web), `trips-jobs.timer` (background jobs); logs: `journalctl -u trips -u trips-jobs` |
| Web server | `/etc/caddy/Caddyfile` (also limits `/manage` and `/backstage` to Cloudflare traffic) |

## Project layout

- `config/`: settings and URLs
- `core/`: home, places, about and legals pages (placeholders for now), robots.txt, client IP helper, Cloudflare Access token check
- `journeys/`: the planner
  - `models.py`: Place, Trip, Traveller, Day, Item, Leg, Stay, BudgetItem, ExchangeRate, ChecklistItem, Document, WeatherCache, Airline
  - `views.py` / `urls.py`: public pages; `manage_views.py` / `manage_urls.py`: the planner
  - `presenters.py`: timeline, stops and map data (with the public/private split)
  - `routing.py`: how each leg is drawn; `geo.py`: distances, arcs, simplification
  - `datasets.py`: airport, airline and station loaders; `search.py`: place search and quick entry
  - `weather.py`, `money.py`, `geocode.py`, `ics.py`: Open-Meteo, exchange rates, Nominatim, calendars
  - `xlsx_import.py`: importer for the "China 2027 Trip (Multi-Tab)" spreadsheet layout
- `templates/`, `static/css/site.css` (minimalist blue theme), `static/js/` (map and planner forms), `static/vendor/leaflet/`
- `deploy/`: systemd units, deploy script, `trips-manage` helper

## Everyday commands

```bash
./deploy/deploy.sh                               # test, then deploy (stops if tests fail)
DJANGO_DEBUG=1 .venv/bin/python manage.py test   # run the tests locally

trips-manage createsuperuser                   # live-site management commands
trips-manage load_places                       # refresh airports, airlines and stations (~15 min)
trips-manage import_trip_xlsx FILE --title "China 2027" --slug china-2027
trips-manage run_jobs                          # trace routes / refresh rates and weather now
trips-manage axes_reset                        # clear admin login lockouts
```

## Open data used

| Data | Source | Licence |
|---|---|---|
| Airports | OurAirports | Public domain |
| Airlines | OpenFlights | ODbL |
| Stations, railway track, rivers, place search | OpenStreetMap via Overpass and Nominatim | ODbL |
| Road routes | OSRM demo server (OpenStreetMap data) | ODbL |
| Base map | OpenStreetMap standard tiles (shown in grey) | ODbL; tile usage policy |
| Railway overlay | OpenRailwayMap tiles | CC-BY-SA |
| Weather | Open-Meteo | CC BY 4.0 |
| Exchange rates | ExchangeRate-API (open.er-api.com) | Free with attribution |

Everything is fetched by the server and cached; the free public services are
used lightly (Nominatim only on explicit "Search the map" clicks, Overpass and
OSRM once per leg when it's added or changed).

## Cloudflare Access

`/manage/` and `/backstage/` sit behind a Cloudflare Access application (email
one-time code). Caddy refuses those paths unless the request came from
Cloudflare, and Django checks the signed Access token once
`CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD` are set in `/etc/trips/trips.env`
(then `sudo systemctl restart trips`).

## Growing it

- New data: add a model, run `makemigrations`, add planner screens (or register it
  in `journeys/admin.py` for quick access), deploy.
- PostgreSQL later: change `DATABASES` in `config/settings.py` and copy the data
  across with `dumpdata` / `loaddata`.
- Django 6.2 (long-term support) is due April 2027.
