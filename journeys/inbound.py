"""Receive forwarded booking confirmations by email.

Works with a Mailgun route ("forward" to this URL, form data) or anything that can
POST the raw email (Content-Type: message/rfc822), such as a Cloudflare Email
Worker. The secret token in the URL is required; a Mailgun signature is checked
when MAILGUN_WEBHOOK_SIGNING_KEY is set; INBOUND_ALLOWED_SENDERS limits who may
forward. Emails land in the planner's Bookings inbox for review.
"""

import hashlib
import hmac
import logging
import time
from email.utils import parseaddr

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import booking_import
from .models import BookingImport, Trip

log = logging.getLogger(__name__)


def _mailgun_signature_ok(post):
    timestamp, token, signature = post.get("timestamp", ""), post.get("token", ""), post.get("signature", "")
    if not (timestamp.isdigit() and token and signature) or abs(time.time() - int(timestamp)) > 15 * 60:
        return False
    expected = hmac.new(settings.MAILGUN_WEBHOOK_SIGNING_KEY.encode(), (timestamp + token).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@csrf_exempt
@require_POST
def inbound_email(request, token):
    if not settings.INBOUND_EMAIL_TOKEN or not constant_time_compare(token, settings.INBOUND_EMAIL_TOKEN):
        raise Http404

    if request.content_type == "message/rfc822":
        subject, sender, text = booking_import.text_from_email(request.body)
    else:
        if settings.MAILGUN_WEBHOOK_SIGNING_KEY and not _mailgun_signature_ok(request.POST):
            return HttpResponseForbidden("Bad signature")
        subject = request.POST.get("subject", "")
        sender = request.POST.get("from") or request.POST.get("sender", "")
        text = request.POST.get("body-plain") or booking_import.html_to_text(request.POST.get("body-html", ""))
        for upload in request.FILES.values():
            if upload.name.lower().endswith((".pdf", ".eml", ".html", ".htm", ".txt")):
                text += "\n" + booking_import.text_from_upload(upload)[2]

    address = parseaddr(sender)[1].lower()
    if settings.INBOUND_ALLOWED_SENDERS and address not in settings.INBOUND_ALLOWED_SENDERS:
        log.warning("Ignored a forwarded email from %s (not an allowed sender)", address or "unknown")
        return HttpResponse("ignored")  # look accepted, so senders learn nothing
    if not text.strip():
        return HttpResponse("empty")

    trip = booking_import.guess_trip(booking_import.parse(text, subject=subject), Trip.objects.all())
    BookingImport.objects.create(
        trip=trip, source=BookingImport.Source.EMAIL, subject=subject[:300], sender=sender[:200],
        text=booking_import.redact(booking_import.clean_text(text))[:200_000],
    )
    log.info("Received a booking email: %s", subject[:80])
    return HttpResponse("ok")
