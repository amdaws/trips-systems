"""Require a valid Cloudflare Access token for the planner and admin.

Cloudflare Access asks for an email code before /manage/ and the admin load,
then adds a signed token (Cf-Access-Jwt-Assertion) to every request it lets
through. Checking that signature here closes the gap of someone reaching the
server from another Cloudflare account or directly by IP. It's off until
CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are set.
"""

import logging

import jwt
from django.conf import settings
from django.http import HttpResponseForbidden

log = logging.getLogger(__name__)


class CloudflareAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.team = settings.CF_ACCESS_TEAM_DOMAIN
        self.audience = settings.CF_ACCESS_AUD
        self.prefixes = ("/manage/", "/" + settings.ADMIN_URL)
        self.keys = (
            jwt.PyJWKClient(f"https://{self.team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
            if self.team and self.audience
            else None
        )

    def __call__(self, request):
        if self.keys and request.path.startswith(self.prefixes) and not self.valid(request):
            return HttpResponseForbidden("Please sign in through Cloudflare Access.", content_type="text/plain")
        return self.get_response(request)

    def valid(self, request):
        token = request.headers.get("Cf-Access-Jwt-Assertion") or request.COOKIES.get("CF_Authorization")
        if not token:
            return False
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=f"https://{self.team}")
            return True
        except Exception as exc:
            log.warning("Rejected Cloudflare Access token: %s", exc)
            return False
