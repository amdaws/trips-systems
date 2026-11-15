#!/usr/bin/env bash
# Deploy this working copy to production (/srv/trips) and restart the site.
# Safe to run repeatedly: ./deploy/deploy.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/srv/trips/app
VENV=/srv/trips/venv
ENV_FILE=/etc/trips/trips.env
STATIC=/var/www/trips.aarondaws.com.au/static

echo "==> Running tests"
if ! out=$(DJANGO_DEBUG=1 "$SRC/.venv/bin/python" "$SRC/manage.py" test --verbosity 0 2>&1); then
  echo "$out" >&2
  echo "Tests failed; not deploying." >&2
  exit 1
fi
echo "$out" | grep -E "^(Ran|OK)"

echo "==> Preparing folders"
sudo install -d -o ubuntu -g ubuntu -m 0755 /srv/trips "$APP"
sudo install -d -o trips -g trips -m 0750 /var/lib/trips
sudo install -d -o trips -g trips -m 0755 "$STATIC"
sudo install -d -o root -g trips -m 0750 /etc/trips
if ! sudo test -f "$ENV_FILE"; then
  echo "==> Creating $ENV_FILE with a new secret key"
  secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
  sed "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$secret|" "$SRC/deploy/trips.env.example" \
    | sudo install -o root -g trips -m 0640 /dev/stdin "$ENV_FILE"
fi
# Add any settings introduced since the env file was created (existing values are kept).
grep -E '^[A-Z0-9_]+=' "$SRC/deploy/trips.env.example" | while IFS= read -r line; do
  key="${line%%=*}"
  if ! sudo grep -q "^${key}=" "$ENV_FILE"; then
    echo "    adding $key to $ENV_FILE"
    echo "$line" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
done

echo "==> Pausing background jobs"
# A job starting mid-deploy would run new code against an unmigrated database.
sudo systemctl stop trips-jobs.timer
# Whatever happens below, don't leave the jobs switched off.
trap 'sudo systemctl start trips-jobs.timer' EXIT
for _ in $(seq 120); do
  systemctl is-active --quiet trips-jobs.service || break
  sleep 1
done
sudo systemctl stop trips-jobs.service

echo "==> Copying code"
# --delete-excluded also clears old __pycache__ folders, so removed apps leave nothing behind.
rsync -a --delete --delete-excluded --exclude-from="$SRC/deploy/rsync-exclude" "$SRC/" "$APP/"
# When this code went live, for "System last updated" on the change log (shown in AEDT).
date -u +%Y-%m-%dT%H:%M:%SZ > "$APP/DEPLOYED_AT"

echo "==> Installing dependencies"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$APP/requirements.txt"
"$VENV/bin/python" -m compileall -q "$APP"

echo "==> Installing service and helper"
sudo install -m 0755 "$SRC/deploy/manage.sh" /usr/local/bin/trips-manage
sudo install -m 0644 "$SRC/deploy/trips.service" /etc/systemd/system/trips.service
sudo install -m 0644 "$SRC/deploy/trips-jobs.service" /etc/systemd/system/trips-jobs.service
sudo install -m 0644 "$SRC/deploy/trips-jobs.timer" /etc/systemd/system/trips-jobs.timer
sudo systemctl daemon-reload

echo "==> Migrating database and collecting static files"
trips-manage migrate --noinput
trips-manage collectstatic --noinput --clear --verbosity 0
trips-manage check --deploy --fail-level WARNING

echo "==> Restarting"
sudo systemctl enable --quiet trips
sudo systemctl restart trips
sudo systemctl enable --quiet --now trips-jobs.timer
for _ in 1 2 3 4 5 6 7 8 9 10; do
  curl -fsS -o /dev/null -H "Host: trips.aarondaws.com.au" http://127.0.0.1:8001/ 2>/dev/null && break
  sleep 1
done
curl -fsS -o /dev/null -H "Host: trips.aarondaws.com.au" http://127.0.0.1:8001/ \
  && echo "Deployed: site is responding." \
  || { echo "Site is not responding. Check: journalctl -u trips -n 50" >&2; exit 1; }
