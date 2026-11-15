#!/usr/bin/env bash
# Run a Django management command against the live site, as the trips service user.
# Installed as /usr/local/bin/trips-manage. Examples:
#   trips-manage createsuperuser
#   trips-manage changepassword aaron
#   trips-manage axes_reset          (clear login lockouts)
set -euo pipefail
exec sudo -u trips -- bash -c '
  set -a; . /etc/trips/trips.env; set +a
  cd /srv/trips/app && exec /srv/trips/venv/bin/python manage.py "$@"
' trips-manage "$@"
