import json
import re

from django.conf import settings
from django.test import TestCase
from django.urls import reverse


class PageTests(TestCase):
    def test_home_page_says_under_development(self):
        response = self.client.get(reverse("core:home"))
        self.assertContains(response, "Aaron Daws Trips")
        self.assertContains(response, "This page is under development.")

    def test_legals_page(self):
        response = self.client.get(reverse("core:legals"))
        self.assertContains(response, "<h1>Legals</h1>", html=True)
        self.assertContains(response, f'href="{reverse("core:legals")}" aria-current="page"')
        for section in ("privacy", "terms", "credits", "contact"):
            self.assertContains(response, f'id="{section}"')
            self.assertContains(response, f'href="#{section}"')  # in the contents list
        self.assertContains(response, "deleted automatically after 30 days")
        self.assertContains(response, "laws of Queensland")
        self.assertContains(response, "no affiliate links")
        self.assertContains(response, 'href="mailto:privacy@aarondaws.com.au"')
        self.assertNotContains(response, "This page is under development.")

    def test_places_and_about_are_gone(self):
        for path in ("/places/", "/about/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertRedirects(response, reverse("core:home"), status_code=301)

    def test_navigation_links_every_page(self):
        response = self.client.get(reverse("core:home"))
        for name in ("core:home", "core:journey", "core:legals", "core:changelog"):
            self.assertContains(response, f'href="{reverse(name)}"')
        for gone in ('href="/places/"', 'href="/about/"', ">Places<", ">About<"):
            self.assertNotContains(response, gone)

    def test_content_security_policy(self):
        response = self.client.get(reverse("core:home"))
        policy = response["Content-Security-Policy-Report-Only"]  # report-only until DJANGO_CSP_ENFORCE is set
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-ancestors 'self'", policy)
        self.assertIn("report-uri /csp-report/", policy)
        nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
        self.assertContains(response, f'<script nonce="{nonce}">')  # the theme script in the page head

    def test_csp_reports_are_logged(self):
        report = {"csp-report": {"violated-directive": "script-src", "blocked-uri": "https://evil.example/x.js",
                                 "document-uri": "https://trips.aarondaws.com.au/"}}
        with self.assertLogs("core.views", "WARNING") as logs:
            response = self.client.post("/csp-report/", json.dumps(report), content_type="application/csp-report")
        self.assertEqual(response.status_code, 204)
        self.assertIn("https://evil.example/x.js", logs.output[0])
        self.assertEqual(self.client.post("/csp-report/", "not json", content_type="application/csp-report").status_code, 204)
        self.assertEqual(self.client.get("/csp-report/").status_code, 405)

    def test_contact_page_is_gone(self):
        self.assertEqual(self.client.get("/contact/").status_code, 404)

    def test_robots_txt(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_unknown_page_uses_friendly_404(self):
        response = self.client.get("/no-such-page/")
        self.assertContains(response, "Page not found", status_code=404)


class AdminTests(TestCase):
    def test_admin_login_at_configured_path(self):
        response = self.client.get("/" + settings.ADMIN_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])


class CloudflareAccessTests(TestCase):
    """The planner and admin need a valid, signed Cloudflare Access token once Access is configured."""

    def setUp(self):
        from unittest import mock

        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.jwt = jwt
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signing_key = mock.Mock(key=self.key.public_key())
        patcher = mock.patch("jwt.PyJWKClient")
        client = patcher.start()
        client.return_value.get_signing_key_from_jwt.return_value = signing_key
        self.addCleanup(patcher.stop)

    def token(self, **overrides):
        claims = {"aud": "test-aud", "iss": "https://team.cloudflareaccess.com", "email": "aaron@example.com"}
        claims.update(overrides)
        return self.jwt.encode(claims, self.key, algorithm="RS256")

    def get(self, path, token=None):
        from django.test import override_settings

        with override_settings(CF_ACCESS_TEAM_DOMAIN="team.cloudflareaccess.com", CF_ACCESS_AUD="test-aud"):
            headers = {"HTTP_CF_ACCESS_JWT_ASSERTION": token} if token else {}
            return self.client.get(path, **headers)

    def test_planner_needs_token(self):
        self.assertEqual(self.get("/manage/login/").status_code, 403)
        self.assertEqual(self.get("/" + settings.ADMIN_URL).status_code, 403)

    def test_valid_token_passes(self):
        self.assertEqual(self.get("/manage/login/", self.token()).status_code, 200)

    def test_wrong_audience_or_issuer_is_rejected(self):
        self.assertEqual(self.get("/manage/login/", self.token(aud="other")).status_code, 403)
        self.assertEqual(self.get("/manage/login/", self.token(iss="https://evil.example")).status_code, 403)

    def test_public_pages_are_unaffected(self):
        self.assertEqual(self.get("/").status_code, 200)


class StaticFilesTests(TestCase):
    """Production builds static files with hashed names; every file a stylesheet or script references must exist."""

    def test_collectstatic_with_hashed_names(self):
        import tempfile

        from django.core.management import call_command
        from django.test import override_settings

        storages = {**settings.STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"}}
        with tempfile.TemporaryDirectory() as root, override_settings(STATIC_ROOT=root, STORAGES=storages):
            call_command("collectstatic", interactive=False, verbosity=0)


class ChangelogTests(TestCase):
    def test_page_lists_releases_newest_first_in_aest(self):
        from core import changelog

        response = self.client.get(reverse("core:changelog"))
        self.assertContains(response, "<h1>What's new</h1>", html=True)
        page = response.content.decode()
        newest, oldest = changelog.releases()[0], changelog.releases()[-1]
        self.assertLess(page.index(newest["title"]), page.index(oldest["title"]))
        self.assertContains(response, '<time class="release__time" datetime="2026-10-26T00:52:00+10:00">12:52 am AEST</time>', html=True)
        self.assertContains(response, "Sunday 25 October 2026")

    def test_times_are_aest_all_year(self):
        from datetime import UTC, datetime

        from core.changelog import aest_time

        self.assertEqual(aest_time(datetime(2026, 10, 25, 14, 21, tzinfo=UTC)), "Mon 26 Oct 2026, 12:21 am AEST")  # summer: still AEST
        self.assertEqual(aest_time(datetime(2026, 7, 1, 2, 0, tzinfo=UTC), date=False), "12:00 pm AEST")

    def test_last_update_comes_from_the_deploy_stamp(self):
        import tempfile
        from pathlib import Path

        from django.test import override_settings

        with tempfile.TemporaryDirectory() as folder:
            stamp = Path(folder, "DEPLOYED_AT")
            stamp.write_text("2026-10-26T03:30:00Z\n")
            with override_settings(DEPLOYED_AT_FILE=stamp):
                self.assertContains(self.client.get("/changelog/"), "System last updated <strong>Mon 26 Oct 2026, 1:30 pm AEST</strong>", html=True)
            with override_settings(DEPLOYED_AT_FILE=Path(folder, "missing")):
                self.assertNotContains(self.client.get("/changelog/"), "System last updated")

    def test_every_entry_is_valid(self):
        from core import changelog

        changelog.releases.cache_clear()
        releases = changelog.releases()  # raises on a bad category, area or time
        self.assertEqual(len({release["at"] for release in releases}), len(releases))
        for release in releases:
            self.assertTrue(release["items"], release["title"])

    def test_linked_from_the_footer_and_the_planner(self):
        from django.contrib.auth import get_user_model

        link = f'href="{reverse("core:changelog")}"'
        self.assertContains(self.client.get(reverse("core:home")), link)
        self.client.force_login(get_user_model().objects.create_superuser("aaron", "aaron@example.com", None))
        self.assertContains(self.client.get(reverse("manage:dashboard")), link, count=2)  # footer and planner home

    def test_recent_updates_show_the_exact_time_on_hover(self):
        from journeys.tests.factories import sample_trip

        trip, _ = sample_trip()
        response = self.client.get(reverse("journeys:trip", args=[trip.slug]))
        self.assertRegex(response.content.decode(), r'<time datetime="[^"]+" title="\w{3} \d{1,2} \w{3} \d{4}, \d{1,2}:\d{2} [ap]m AE[DS]T">')


class HeaderTests(TestCase):
    def test_no_trips_tab(self):
        page = self.client.get("/journey/").content.decode()
        nav = page[page.index('<nav aria-label="Main">'):page.index("</nav>", page.index('<nav aria-label="Main">'))]
        self.assertNotIn(">Trips<", nav)
        self.assertIn(">Journey<", nav)
