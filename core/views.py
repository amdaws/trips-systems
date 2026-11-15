import json
import logging
import time

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from journeys.models import Trip

from . import changelog

log = logging.getLogger(__name__)
_csp_reports = {"hour": 0, "count": 0}


def home(request):
    trips = Trip.objects.filter(visibility=Trip.Visibility.PUBLIC).order_by("-start_date")
    return render(request, "core/home.html", {"trips": trips})


def legals(request):
    return render(request, "core/legals.html")


def whats_new(request):
    """The change log: what changed on the site and when, in AEST."""
    deployed = changelog.deployed_at()
    return render(request, "core/changelog.html", {
        "days": changelog.by_day(), "counts": changelog.counts(),
        "deployed": changelog.aest_time(deployed) if deployed else None,
    })


def robots_txt(request):
    return HttpResponse("User-agent: *\nDisallow: /manage/\nDisallow: /s/\nDisallow: /cal/\nDisallow: /inbound/\n", content_type="text/plain")


@csrf_exempt
@require_POST
def csp_report(request):
    """Browsers report what the Content-Security-Policy blocked (or would block). Logged, at most 60 an hour."""
    hour = int(time.time() // 3600)
    if _csp_reports["hour"] != hour:
        _csp_reports.update(hour=hour, count=0)
    _csp_reports["count"] += 1
    try:
        size = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = 1 << 30
    if _csp_reports["count"] <= 60 and 0 < size <= 16_384:  # checked before the body is read into memory
        try:
            report = json.loads(request.body).get("csp-report", {})
        except (ValueError, AttributeError):
            report = {}
        if isinstance(report, dict):
            log.warning("CSP %s blocked %s on %s", str(report.get("violated-directive", "?"))[:80],
                        str(report.get("blocked-uri", "?"))[:200], str(report.get("document-uri", "?"))[:200])
    return HttpResponse(status=204)
