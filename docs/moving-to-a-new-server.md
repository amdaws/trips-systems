# Aaron Daws Trips: app review and move plan

**From:** Aaron Daws
**Date:** 22 November 2026
**About:** moving the trips web app (code, data and website) from its current server to a new server, and optionally to a new web address.

---

## Before you start: the one rule

**The current website and server must stay running the whole time.** Nothing in this plan stops, changes or removes anything on the current server. Every step there only *reads*: copying the code, taking a database snapshot, copying settings. Visitors keep using the current site until the new one has been checked, and switching over is a single DNS change that can be undone in a minute.

Once the new server has run well for a while, I'll decide separately whether the old one is switched off. That decision is not part of this move.

---

## 1. What the app is

`trips.aarondaws.com.au` is my travel site. It has three parts:

- a public site for trip pages and maps;
- a public journey planner for Brisbane, Melbourne and Sydney, with international routes;
- a private planner where I organise trips: transport, stays, activities, budget and bookings.

It's one Django application, built and maintained by me with Claude Code.

| | |
|---|---|
| Framework | Django 6.1.3 on Python 3.14 |
| Database | SQLite, a single file of 11.5 MB |
| Web server | gunicorn (2 workers × 2 threads) behind Caddy 2.11 |
| In front | Cloudflare: proxy, TLS, and Cloudflare Access on the planner |
| Background work | a systemd timer runs `manage.py run_jobs` every 2 minutes |
| Server today | Ubuntu 26.04 LTS, 2 CPUs, 1 GB RAM (AWS Lightsail) |
| Code | GitHub `amdaws/trips-systems` (private) |

```
Visitor → Cloudflare (proxy, TLS, Access for /manage) → Caddy (TLS, static files) → gunicorn :8001 → Django → SQLite
                                                                         trips-jobs.timer (every 2 min) → run_jobs
```

### Where things live on the current server

| What | Where | Size |
|---|---|---|
| Source (edited here, deployed from here) | `/home/ubuntu/sites/trips.aarondaws.com.au` | 5 MB |
| Deployed code and virtualenv | `/srv/trips/app`, `/srv/trips/venv` | 5 MB, 267 MB |
| Database | `/var/lib/trips/db.sqlite3` (WAL mode) | 11.5 MB |
| Uploaded documents | `/var/lib/trips/media/` (none uploaded yet) | 0 |
| Timetables, walking and driving networks, live-feed caches | `/var/lib/trips/transit/` | 2.6 GB (all rebuildable) |
| Settings and secrets | `/etc/trips/trips.env` (root:trips, 0640) | |
| Static files | `/var/www/trips.aarondaws.com.au/static` (served by Caddy) | 5 MB |
| Services | `trips.service` (web), `trips-jobs.service` + `trips-jobs.timer` (jobs) | |
| Web server | `/etc/caddy/Caddyfile`, plus `/etc/caddy/cloudflare.env` for certificates | |
| Firewall | nftables table `cloudflare_only`: ports 80/443 accept only Cloudflare | |
| Helper | `/usr/local/bin/trips-manage` runs management commands as the `trips` user | |

The live code is exactly commit **`40b4a15`** on branch **`per-person-costs-and-privacy-fixes`**. I checked this on 22 Nov 2026 by comparing `/srv/trips/app` with the commit. That branch has not been merged into `main` yet (`main` is at `627ba6e`), so deploy from the branch, or merge it first.

---

## 2. Review: what the app does

### Public site (anyone)

- **Home page:** lists published trips, or shows "in development" when there are none.
- **Trip pages:**
  - A route map showing flights (along recorded flight paths where available), trains on real track, boats, road and metro legs, with a railway overlay.
  - Overnight stops with weather, a day-by-day plan and a countdown.
  - Stays and activities pages.
  - A page per stop, with a short guide (drafted by Claude, published only once I've checked it).
- **Live flights:** follow a flight in the air on the trip map.
- **Calendar feeds:** `.ics` feeds for a trip.
- **Privacy:** public pages show the route and story only. They never show booking references, hotel names or addresses, costs, private notes, or flight and train numbers. Visitors get no cookies.
- **Secret link:** `/s/<token>/` shows the full trip, including what's booked, to the people I share it with. They don't need to sign in. There's also a private calendar feed at `/cal/<token>.ics`.
- **Journey planner (`/journey/`):**
  - Brisbane, Melbourne and Sydney: trains, metro, trams, ferries and buses from the official timetables, with live times, walking along real streets, driving, and step-free options.
  - International routes: China's high-speed trains and flights between airports.
  - Any journey can be added to a trip.
- **Other pages:** "What's new" (the change log), privacy policy and terms, `robots.txt`, and a CSP report endpoint.

### Private planner (`/manage/`, my login plus Cloudflare Access)

- **Trips:** settings, public or private, item by item, and secret links that can be replaced.
- **Days and activities:**
  - Cost per person × people.
  - Trip.com, Tripadvisor and Klook links are read for the name and the exact map pin.
- **Transport:**
  - Quick entry (for example `CX156 BNE-HKG`), automatic route tracing and live flight look-ups.
  - Planned or Booked, with a "booked vs still to book" count and when each train's tickets go on sale.
  - 24-hour times throughout.
- **Stays:** Booking.com and other links read for details. Nights and weather follow the stays.
- **Budget:** any currency, converted to AUD with daily exchange rates. Shows who paid, an estimate against what's spent, and costs per person.
- **Bookings in:**
  - Add a confirmation (pasted text, link or PDF).
  - Forward booking emails to the site (Mailgun).
  - **Send to Trips**, a bookmarklet that sends the page I'm looking at.
- **Claude features** (Anthropic API, model `claude-haiku-5-5`):
  - Reads pages, pasted text and PDFs into bookings.
  - Copies local (Chinese) names and addresses for **Taxi cards**.
  - Reviews the plan on the **Check** tab.
  - Drafts **stop guides**.
- **Checklist** with automatic train-ticket reminders, and **private documents** (uploads, kept off the public site).
- **Admin** at a hidden address, which is set in the settings file.

### Background jobs (`run_jobs`, every 2 minutes)

- Route tracing for new or changed legs.
- Exchange rates and weather.
- City timetables, rebuilt weekly with one city per run.
- Walking and driving networks, refreshed monthly.
- China's station list, refreshed weekly.
- Live flight positions, and recorded flight tracks (FlightRadar24 and adsb.lol).

---

## 3. What's in the bundle

The bundle folder is `/home/ubuntu/trips-bundle-2026-11-22/` on the current server. Only its owner can read it (0700). It was made on 22 Nov 2026 at 17:02 AEST without stopping anything.

| File | What it is | Size |
|---|---|---|
| `code/trips-systems.bundle` | The whole git repository: every branch, full history, and the set-aside "rail red colours" stash (`refs/stash`). Restore it with `git clone`. | 656 KB |
| `code/trips-source-40b4a15.tar.gz` | Just the live code (commit `40b4a15`), no history | 561 KB |
| `data/trips-db-2026-11-22.sqlite3.gz` | Snapshot of the live database (integrity check ok; 1 trip, 23 legs, 15 stays, 25,848 places; last migration `journeys 0030`) | 3.2 MB (11.5 MB unpacked) |
| `snapshot-db.sh` | Takes a fresh database copy at switch-over time, without stopping the site | |
| `MOVE-BRIEF.md` | This document | |
| `SHA256SUMS` | Checksums: `sha256sum -c SHA256SUMS` | |

### Left out on purpose

| What | Why | How it gets to the new server |
|---|---|---|
| `/etc/trips/trips.env` (API keys, secret key) | Secrets don't go in bundles | Copied directly between the servers (step C) |
| `/etc/caddy/cloudflare.env` (Cloudflare token for certificates) | Secret | Copied directly (step F), or make a new token |
| `/var/lib/trips/transit/` (2.6 GB) | Rebuilt automatically by the jobs | Optional direct copy to skip the wait (step D) |
| Old database backups in `/var/lib/trips/` | Superseded by the snapshot | Not needed |
| Caddy access logs | The privacy policy says they're kept for 30 days | Not moved |
| Other sites on this server (`jackyoung.au`, `timesheets.127.technology`) | Not part of this move | They stay where they are |

**The database snapshot holds private trip details** (bookings, references, costs). Treat it like the settings file: copy it server-to-server with `scp`, never by email or chat.

---

## 4. What the new server needs

- **OS:** Ubuntu 26.04 LTS with its Python 3.14. The pinned packages in `requirements.txt` are known to install there.
- **Size:**
  - Memory: 1 GB is the minimum (it runs on that today); 2 GB gives the timetable builds more room.
  - CPU: 2 cores.
  - Disk: at least 10 GB free.
- **A login user called `ubuntu` with sudo.** `deploy/deploy.sh` gives `/srv/trips` to `ubuntu`. With another username, change `-o ubuntu -g ubuntu` there.
- **Caddy 2.11 with the Cloudflare DNS module** (`dns.providers.cloudflare`). Certificates come from Let's Encrypt by DNS-01 through the Cloudflare API, so the new server can get its certificate before any traffic points at it.
  - On the current server, a custom Caddy build replaces the apt package's binary with `dpkg-divert`, as described in Caddy's docs for custom builds.
  - A drop-in at `/etc/systemd/system/caddy.service.d/cloudflare.conf` loads `/etc/caddy/cloudflare.env`.
- **Ports:**
  - 22 for SSH.
  - 80 and 443 open to Cloudflare. Open them in the provider's firewall, then lock them to Cloudflare with `deploy/firewall/`.
- **Outbound HTTPS** to the services the app uses:
  - Transport: Translink, Transport Victoria open data, Transport for NSW.
  - Maps: OpenStreetMap (Overpass and its mirrors, Nominatim, OSRM), Photon, BBBike extracts, OurAirports.
  - Weather and money: Open-Meteo, open.er-api.com.
  - China rail: 12306, MetroMan.
  - Flights: AeroDataBox (RapidAPI), FlightRadar24, adsb.lol.
  - Other: GitHub, Anthropic, Cloudflare.

---

## 5. Settings to carry over

All of these are in `/etc/trips/trips.env`. The names are also in `deploy/trips.env.example`, which matches the live file name for name. **Copy the file as it is**, then change only what the table says.

| Setting | What it's for | Secret | Same address | New address |
|---|---|---|---|---|
| `DJANGO_SECRET_KEY` | Signs sign-in sessions | Yes | Keep | Keep |
| `DJANGO_ALLOWED_HOSTS` | The site's address | | Keep | **Change** |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://` + the address | | Keep | **Change** |
| `DJANGO_DB_PATH` | `/var/lib/trips/db.sqlite3` | | Keep | Keep |
| `DJANGO_STATIC_ROOT` | `/var/www/trips.aarondaws.com.au/static` | | Keep | Optional (must match Caddyfile and `deploy.sh`) |
| `DJANGO_MEDIA_ROOT` | `/var/lib/trips/media` | | Keep | Keep |
| `DJANGO_ADMIN_PATH` | The admin's hidden address | Keep private | Keep | Keep |
| `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | Checks the Cloudflare Access token on `/manage/` and the admin | | Keep | Keep if the same Access application covers the new address; else the new application's AUD |
| `AERODATABOX_RAPIDAPI_KEY` | Live flight look-ups when adding flights | Yes | Keep | Keep |
| `INBOUND_EMAIL_TOKEN`, `MAILGUN_WEBHOOK_SIGNING_KEY`, `INBOUND_ALLOWED_SENDERS` | Forwarded booking emails | Yes | Keep | Keep (update Mailgun's route URL) |
| `FLIGHTRADAR24_API_TOKEN` | Recorded flight paths (**costs credits**) | Yes | See step E | See step E |
| `ADSBLOL_TRACKS` | Free flight tracks from adsb.lol (`1`) | | Keep | Keep |
| `TFNSW_API_KEY` | Sydney timetables and live times | Yes | Keep | Keep |
| `TRANSPORT_VIC_API_KEY` | Melbourne live times (see known issues) | Yes | Keep | Keep |
| `DJANGO_CSP_ENFORCE` | Browser security rules enforced (`1`) | | Keep | Keep |
| `ANTHROPIC_API_KEY` | Claude features | Yes | Keep | Keep |
| `TRIPS_CLAUDE_MODEL` | `claude-haiku-5-5` | | Keep | Keep |

Caddy separately needs `CLOUDFLARE_API_TOKEN` in `/etc/caddy/cloudflare.env`: a Cloudflare API token with DNS edit rights on the zone.

---

## 6. Moving it, step by step

In the commands, `OLD` is the current server and `NEW` is the new one, as SSH hosts. Steps on `OLD` only read.

### A. Prepare the new server (on NEW)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git rsync sqlite3 curl
sudo useradd --system --user-group --home-dir /var/lib/trips --no-create-home --shell /usr/sbin/nologin trips
sudo install -d -o trips -g trips -m 0750 /var/lib/trips
```

Install Caddy from its apt repository, then swap in a build with the Cloudflare DNS module (Caddy's "custom builds with the apt package" instructions). Check with `caddy list-modules | grep cloudflare`.

### B. Code (on NEW)

Copy the bundle across (run on OLD):

```bash
scp -r ~/trips-bundle-2026-11-22 NEW:~/
```

Then on NEW:

```bash
cd ~/trips-bundle-2026-11-22 && sha256sum -c SHA256SUMS
mkdir -p ~/sites && cd ~/sites
git clone --branch per-person-costs-and-privacy-fixes ~/trips-bundle-2026-11-22/code/trips-systems.bundle trips.aarondaws.com.au
cd trips.aarondaws.com.au
git remote set-url origin git@github.com:amdaws/trips-systems.git
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DJANGO_DEBUG=1 .venv/bin/python manage.py test --verbosity 0
```

- **Pushing to GitHub** from NEW needs its own deploy key on the repository. Today `OLD` uses an SSH alias, `github-trips-systems`, with a key in `~/.ssh/trips-systems-deploy`.
- **The rail red colours stash** comes back with `git fetch ~/trips-bundle-2026-11-22/code/trips-systems.bundle refs/stash` and then `git stash apply FETCH_HEAD`, if it's ever wanted. Expect one conflict, in `core/changelog.toml`, because the change log has grown since.

### C. Settings (from OLD to NEW, directly)

On OLD:

```bash
sudo install -m 600 -o ubuntu /etc/trips/trips.env ~/trips.env.copy && scp ~/trips.env.copy NEW:~/ && rm ~/trips.env.copy
```

On NEW:

```bash
sudo install -d -o root -g trips -m 0750 /etc/trips
sudo install -o root -g trips -m 0640 ~/trips.env.copy /etc/trips/trips.env && rm ~/trips.env.copy
```

**Before the first deploy, clear `FLIGHTRADAR24_API_TOKEN=` on NEW.** Otherwise both servers' jobs fetch the same flight tracks and spend the credits twice. Put it back at switch-over (step H).

### D. Data (on NEW)

```bash
gunzip -c ~/trips-bundle-2026-11-22/data/trips-db-2026-11-22.sqlite3.gz | sudo install -o trips -g trips -m 0640 /dev/stdin /var/lib/trips/db.sqlite3
```

Optional: copy the 2.6 GB of timetables and street networks. Otherwise the jobs rebuild them over the first hours, one city per run. The copy reads OLD at low priority and doesn't stop anything. Run it on OLD:

```bash
sudo nice -n 19 tar -C /var/lib/trips -czf - transit | ssh NEW 'sudo tar -C /var/lib/trips -xzf - && sudo chown -R trips:trips /var/lib/trips/transit'
```

### E. Deploy (on NEW)

```bash
cd ~/sites/trips.aarondaws.com.au && ./deploy/deploy.sh
```

This script:

1. Runs the tests.
2. Copies the code to `/srv/trips/app` and builds `/srv/trips/venv`.
3. Installs the systemd units and `trips-manage`.
4. Migrates the database. With the snapshot in place, nothing should need migrating.
5. Collects static files and starts the site and the jobs.

It warns if `/etc/caddy/Caddyfile` doesn't have the site's block yet; that's step F.

Then check:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: trips.aarondaws.com.au" http://127.0.0.1:8001/
trips-manage check --deploy
```

### F. Web server and firewall (on NEW)

1. Copy `/etc/caddy/cloudflare.env` from OLD the same way as step C (root, 0600), or create a new Cloudflare token.
2. Add the drop-in `/etc/systemd/system/caddy.service.d/cloudflare.conf` with `[Service]` and `EnvironmentFile=/etc/caddy/cloudflare.env`.
3. Build `/etc/caddy/Caddyfile` from the repository's `deploy/Caddyfile`. Keep the global options, the `(common)` snippet and the `trips.aarondaws.com.au { … }` block. **Leave out `jackyoung.au` and `www.jackyoung.au`**: they belong to another site on OLD.
4. Validate and reload:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl daemon-reload && sudo systemctl restart caddy
```

Lock ports 80 and 443 to Cloudflare, as on OLD:

```bash
cd ~/sites/trips.aarondaws.com.au/deploy/firewall
sudo install -m 0755 cloudflare-only-refresh /usr/local/sbin/cloudflare-only-refresh
sudo install -m 0644 cloudflare-only.service cloudflare-only-refresh.service cloudflare-only-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo /usr/local/sbin/cloudflare-only-refresh
sudo systemctl enable --now cloudflare-only.service cloudflare-only-refresh.timer
```

### G. Test before switching (OLD is still live)

1. In Cloudflare DNS, add a temporary proxied record, for example `trips-new.aarondaws.com.au` → NEW's IP.
2. On NEW, add that name:
   - to `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated);
   - to the Caddy site address (`trips.aarondaws.com.au, trips-new.aarondaws.com.au {`).
   - Then restart `trips` and reload `caddy`.
3. Add the temporary name to the **Cloudflare Access application** that guards `/manage` and the admin. Until it's added, the planner on that name answers 403 (it fails closed), so the public pages are safe to test meanwhile.
4. Check:
   - [ ] The home page and the China 2027 trip page, its map and stop pages.
   - [ ] The secret link (`/s/<token>/`) shows booked status; the public page doesn't.
   - [ ] The journey planner in Brisbane, Sydney and Melbourne, and a China train search.
   - [ ] Sign in to `/manage/`. Open Transport (its booked count matches OLD's), Stays, Budget, Check and Taxi cards.
   - [ ] "Read details from a link" on a stay (this checks Claude).
   - [ ] `sudo journalctl -u trips -u trips-jobs -n 100` shows no errors.

### H. Switch over (a few minutes)

1. Ask anyone using the planner not to edit for 15 minutes.
2. On OLD, take a fresh snapshot (the site keeps running):

   ```bash
   ~/trips-bundle-2026-11-22/snapshot-db.sh ~/trips-db-final.sqlite3
   ```

   Then copy it across with `scp ~/trips-db-final.sqlite3.gz NEW:~/`.
3. On NEW, swap the database in:

   ```bash
   sudo systemctl stop trips-jobs.timer trips-jobs.service trips
   sudo rm -f /var/lib/trips/db.sqlite3-wal /var/lib/trips/db.sqlite3-shm   # the test copy's journal, never the new file's
   gunzip -c ~/trips-db-final.sqlite3.gz | sudo install -o trips -g trips -m 0640 /dev/stdin /var/lib/trips/db.sqlite3
   trips-manage migrate --noinput
   sudo systemctl start trips trips-jobs.timer
   ```

4. Put `FLIGHTRADAR24_API_TOKEN` back on NEW and run `sudo systemctl restart trips`.
5. In Cloudflare DNS, point `trips.aarondaws.com.au` at NEW's IP. Leave it proxied. Behind Cloudflare's proxy this takes effect almost at once.
6. Run the checks from step G against the real address.
7. Remove the temporary `trips-new` record, and the name from the settings, Caddy and Access.

### I. After the switch

- **OLD stays exactly as it is**, running, as the fallback. It just stops getting visitors.
- If OLD carries on running its jobs, it keeps spending FlightRadar24 credits. Clearing `FLIGHTRADAR24_API_TOKEN` on OLD stops that without taking anything down. That's my call, after the switch.
- From now on, edit and deploy from `~/sites/trips.aarondaws.com.au` on NEW. Nothing should be deployed to OLD again, or the two copies will drift apart.

### Rolling back

Point the DNS record back at OLD's IP. Anything changed on NEW in between can be carried back with `snapshot-db.sh` run on NEW, followed by step H's database swap run on OLD. **That's the only step in this plan that changes OLD, and only if rolling back.**

---

## 7. If the website moves to a new address too

Do the server move first, at the same address, and change the address afterwards as a separate step. This is how the site moved to `trips.aarondaws.com.au` before. To change it, update these places, then deploy:

| Where | What |
|---|---|
| `/etc/trips/trips.env` | `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` (and optionally `DJANGO_STATIC_ROOT`) |
| `deploy/trips.env.example` lines 1, 6, 7, 9 | The same, for new installs |
| `deploy/Caddyfile` lines 33, 58, 72, and `/etc/caddy/Caddyfile` | The site name, the static folder and the log file name |
| `deploy/deploy.sh` lines 10, 80, 83, 90, 92, 97 | The static folder, the `Host:` header used to check the site, and the Caddy block it compares |
| `journeys/http.py` line 12 | The User-Agent's contact URL. OpenStreetMap's usage policy asks for a real one. |
| `static/js/journey.js` line 620 | "Planned with the journey planner at …" text in saved journeys |
| `journeys/ics.py` line 16, `static/js/journey.js` line 623 | Calendar event IDs. **Leave these as they are**: changing them makes calendar apps show every event twice. |
| `templates/core/legals.html` line 204 | The privacy email, only if the email domain changes too |
| `deploy/trips.service`, `deploy/trips-jobs.service` line 2 | Descriptions only |
| Cloudflare | DNS record for the new name. Add the new name to the Access application (else update `CF_ACCESS_AUD`). |
| Mailgun | The inbound route's URL: `https://<new address>/inbound/email/<token>/` |
| Send to Trips | Reinstall the bookmarklet from the planner's Send to Trips page |
| People I've shared with | Secret links (`/s/…`) and calendar feeds (`/cal/…`, `/trips/…/calendar.ics`) use the old address. **Add a permanent redirect** from the old address to the new one (a Cloudflare redirect rule, keeping the path) so they keep working. |

---

## 8. Known issues to carry over

These came up in a review on 22 Nov 2026. None of them blocks the move.

1. **Melbourne live times are refused.**
   - Transport Victoria answers 401 to `TRANSPORT_VIC_API_KEY`, the same answer as sending no key.
   - The key has the shape of a self-made token from the portal's API tokens page. The fix is to copy the automatically generated API key (opendata.transport.vic.gov.au → My Account → Profile → API tokens) into the settings and restart `trips`.
   - Meanwhile Melbourne shows timetabled times. Sydney's key works on every feed.
2. **Simultaneous journey searches can clash.**
   - Each gunicorn worker runs two threads, which share one SQLite connection to a city's timetable (`journeys/timetable.py`, class `City`).
   - Two searches at the same moment can error or get each other's rows. This happened once, on 19 Nov, and I reproduced it.
   - The fix is a connection per thread. It hasn't been done yet.
3. **One test is occasionally flaky.** `test_public.PrivacyTests.test_hidden_dates` checks that "2027" isn't on a page, and a timestamp can contain those digits by chance. If `deploy.sh` stops on it, run it again.
4. **Manly Fast Ferry live times** were failing at Transport for NSW's end (502) on 22 Nov. The planner falls back to the timetable.
5. **The live code is on a branch** (`per-person-costs-and-privacy-fixes`), not `main`.

---

## 9. Checklist

- [ ] New server ready: Ubuntu 26.04, `ubuntu` user with sudo, `trips` user, Caddy with the Cloudflare module
- [ ] Bundle copied and `sha256sum -c SHA256SUMS` passes
- [ ] Code cloned, tests pass on NEW
- [ ] Settings copied directly. `FLIGHTRADAR24_API_TOKEN` cleared on NEW for now.
- [ ] Database restored (optionally `transit/` copied)
- [ ] `./deploy/deploy.sh` succeeded on NEW
- [ ] Caddy block, Cloudflare token and firewall in place on NEW
- [ ] Tested through a temporary hostname, including `/manage/` behind Access
- [ ] Final snapshot from OLD restored on NEW
- [ ] `FLIGHTRADAR24_API_TOKEN` restored on NEW
- [ ] DNS switched to NEW and checked
- [ ] Temporary hostname removed
- [ ] OLD still running, untouched, as the fallback

Thanks,
Aaron Daws
