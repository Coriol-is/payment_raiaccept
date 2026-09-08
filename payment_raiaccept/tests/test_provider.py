"""Provider-side tests: authentication, token cache, request plumbing.

The bug that motivated this suite is not visible from inside the
module: authenticating on every single API call is correct-looking
Python that quietly walks the merchant's credentials into
`AUTH_PASSWORD_ATTEMPTS_EXCEEDED`. So these tests count HTTP calls, not
just outcomes.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import RaiffeisenCommon

AUTH_PATH = "odoo.addons.payment_raiaccept.models.payment_provider.requests.post"
REQ_PATH = "odoo.addons.payment_raiaccept.models.payment_provider.requests.request"


class _Response:
    """Minimal stand-in for a `requests.Response`."""

    def __init__(self, json_data=None, status_code=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


LOGIN_OK = {
    "accessToken": "access-1",
    "accessTokenExpiresIn": 299,
    "refreshToken": "refresh-1",
    "refreshTokenExpiresIn": 86399,
}
REFRESH_OK = {"accessToken": "access-2", "accessTokenExpiresIn": 299}


@tagged("post_install", "-at_install")
class TestRaiffeisenAuth(RaiffeisenCommon):

    def test_login_sends_the_documented_body(self):
        with patch(AUTH_PATH, return_value=_Response(LOGIN_OK)) as post:
            token = self.provider._raiffeisen_login()
        self.assertEqual(token, "access-1")
        url = post.call_args.args[0]
        body = post.call_args.kwargs["json"]
        self.assertEqual(url, "https://auth.raiaccept.com/auth/api/login")
        self.assertEqual(body["username"], "sandbox-user")
        self.assertEqual(body["password"], "sandbox-pass")
        self.assertEqual(body["integrationContext"]["type"], "CODE")
        self.assertTrue(body["integrationContext"]["data"]["vendor"])

    def test_login_caches_both_tokens_with_expiries(self):
        with patch(AUTH_PATH, return_value=_Response(LOGIN_OK)):
            self.provider._raiffeisen_login()
        self.assertEqual(self.provider.raiffeisen_access_token, "access-1")
        self.assertEqual(self.provider.raiffeisen_refresh_token, "refresh-1")
        self.assertTrue(self.provider.raiffeisen_access_token_expiry)
        self.assertGreater(
            self.provider.raiffeisen_refresh_token_expiry,
            self.provider.raiffeisen_access_token_expiry,
        )

    def test_cached_token_is_reused_without_any_http_call(self):
        self.provider.write({
            "raiffeisen_access_token": "cached",
            "raiffeisen_access_token_expiry":
                fields.Datetime.now() + timedelta(minutes=4),
        })
        with patch(AUTH_PATH) as post:
            token = self.provider._raiffeisen_get_access_token()
        self.assertEqual(token, "cached")
        post.assert_not_called()

    def test_expiring_token_is_refreshed_not_re_logged_in(self):
        """A refresh must not resend the credentials.

        Every credential submission counts towards the auth service's
        attempt limit, which is what locks a busy shop out.
        """
        now = fields.Datetime.now()
        self.provider.write({
            "raiffeisen_access_token": "cached",
            "raiffeisen_access_token_expiry": now + timedelta(seconds=5),
            "raiffeisen_refresh_token": "refresh-1",
            "raiffeisen_refresh_token_expiry": now + timedelta(hours=20),
        })
        with patch(AUTH_PATH, return_value=_Response(REFRESH_OK)) as post:
            token = self.provider._raiffeisen_get_access_token()
        self.assertEqual(token, "access-2")
        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs["json"]
        self.assertEqual(
            post.call_args.args[0],
            "https://auth.raiaccept.com/auth/api/refresh",
        )
        self.assertEqual(body["refreshToken"], "refresh-1")
        self.assertNotIn("password", body)

    def test_expired_refresh_token_falls_back_to_login(self):
        now = fields.Datetime.now()
        self.provider.write({
            "raiffeisen_refresh_token": "refresh-old",
            "raiffeisen_refresh_token_expiry": now - timedelta(seconds=1),
        })
        with patch(AUTH_PATH, return_value=_Response(LOGIN_OK)) as post:
            token = self.provider._raiffeisen_get_access_token()
        self.assertEqual(token, "access-1")
        self.assertIn("password", post.call_args.kwargs["json"])

    def test_rejected_refresh_falls_back_to_login(self):
        now = fields.Datetime.now()
        self.provider.write({
            "raiffeisen_refresh_token": "refresh-dead",
            "raiffeisen_refresh_token_expiry": now + timedelta(hours=20),
        })
        responses = [_Response(status_code=400), _Response(LOGIN_OK)]
        with patch(AUTH_PATH, side_effect=responses):
            token = self.provider._raiffeisen_get_access_token()
        self.assertEqual(token, "access-1")

    def test_missing_access_token_in_response_is_an_error(self):
        with patch(AUTH_PATH, return_value=_Response({"nothing": True})):
            with self.assertRaises(ValidationError):
                self.provider._raiffeisen_login()

    def test_missing_credentials_are_reported_before_any_call(self):
        self.provider.write({
            "state": "disabled",
            "raiffeisen_sandbox_username": False,
            "raiffeisen_sandbox_password": False,
            "raiffeisen_api_username": False,
            "raiffeisen_api_password": False,
        })
        with patch(AUTH_PATH) as post:
            with self.assertRaises(ValidationError):
                self.provider._raiffeisen_login()
        post.assert_not_called()

    def test_changing_credentials_drops_the_cached_tokens(self):
        """A token minted for sandbox is worthless against production."""
        self.provider.write({
            "raiffeisen_access_token": "cached",
            "raiffeisen_access_token_expiry":
                fields.Datetime.now() + timedelta(minutes=4),
            "raiffeisen_refresh_token": "refresh-1",
        })
        self.provider.raiffeisen_sandbox_password = "rotated"
        self.assertFalse(self.provider.raiffeisen_access_token)
        self.assertFalse(self.provider.raiffeisen_refresh_token)

    def test_switching_state_drops_the_cached_tokens(self):
        self.provider.write({
            "raiffeisen_api_username": "prod-user",
            "raiffeisen_api_password": "prod-pass",
            "raiffeisen_access_token": "cached",
            "raiffeisen_access_token_expiry":
                fields.Datetime.now() + timedelta(minutes=4),
        })
        self.provider.state = "enabled"
        self.assertFalse(self.provider.raiffeisen_access_token)


@tagged("post_install", "-at_install")
class TestRaiffeisenApiRequest(RaiffeisenCommon):

    def setUp(self):
        super().setUp()
        self.provider.write({
            "raiffeisen_access_token": "cached",
            "raiffeisen_access_token_expiry":
                fields.Datetime.now() + timedelta(minutes=4),
        })

    def test_request_targets_the_documented_api_host(self):
        with patch(REQ_PATH, return_value=_Response({"ok": 1})) as req:
            self.provider._raiffeisen_api_request("GET", "/orders/X")
        self.assertEqual(
            req.call_args.args[1], "https://trapi.raiaccept.com/orders/X"
        )
        self.assertEqual(
            req.call_args.kwargs["headers"]["Authorization"], "Bearer cached"
        )

    def test_401_retries_once_with_a_fresh_token(self):
        responses = [_Response(status_code=401), _Response({"ok": 1})]
        with patch(REQ_PATH, side_effect=responses) as req, \
                patch(AUTH_PATH, return_value=_Response(LOGIN_OK)) as post:
            result = self.provider._raiffeisen_api_request("GET", "/orders/X")
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(req.call_count, 2)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            req.call_args.kwargs["headers"]["Authorization"], "Bearer access-1"
        )

    def test_persistent_401_gives_up_rather_than_looping(self):
        with patch(REQ_PATH, return_value=_Response(status_code=401)) as req, \
                patch(AUTH_PATH, return_value=_Response(LOGIN_OK)):
            with self.assertRaises(ValidationError):
                self.provider._raiffeisen_api_request("GET", "/orders/X")
        self.assertEqual(req.call_count, 2)

    def test_transactions_endpoint_falls_back_to_post(self):
        """The docs describe this endpoint as a POST; live behaviour is
        unconfirmed, so a refused GET must not lose the transaction id."""
        calls = []

        def _request(method, url, **kwargs):
            calls.append(method)
            if method == "GET":
                return _Response(status_code=405)
            return _Response({"transactionId": "T-1",
                              "transactionType": "PURCHASE",
                              "status": "SUCCESS"})

        with patch(REQ_PATH, side_effect=_request):
            txs = self.provider._raiffeisen_list_transactions("ORD-1")
        self.assertEqual(calls, ["GET", "POST"])
        self.assertEqual(txs[0]["transactionId"], "T-1")


@tagged("post_install", "-at_install")
class TestWebhookIpAllowlist(RaiffeisenCommon):

    def test_empty_allowlist_is_log_only(self):
        self.provider.raiffeisen_webhook_allowed_ips = False
        self.assertEqual(
            self.provider._raiffeisen_webhook_allowed_ip_list(), set()
        )
        allowed, _reason = self.provider._raiffeisen_webhook_ip_allowed(
            "203.0.113.9"
        )
        self.assertTrue(allowed)

    def test_allowlist_parsing_strips_whitespace_and_blanks(self):
        self.provider.raiffeisen_webhook_allowed_ips = \
            " 1.2.3.4 , ,5.6.7.8 "
        self.assertEqual(
            self.provider._raiffeisen_webhook_allowed_ip_list(),
            {"1.2.3.4", "5.6.7.8"},
        )

    def test_allowlist_accepts_and_rejects(self):
        self.provider.raiffeisen_webhook_allowed_ips = "1.2.3.4"
        self.assertTrue(
            self.provider._raiffeisen_webhook_ip_allowed("1.2.3.4")[0]
        )
        self.assertFalse(
            self.provider._raiffeisen_webhook_ip_allowed("9.9.9.9")[0]
        )


@tagged("post_install", "-at_install")
class TestProviderConfiguration(RaiffeisenCommon):

    def test_enabled_requires_production_credentials(self):
        with self.assertRaises(ValidationError):
            self.provider.write({
                "state": "enabled",
                "raiffeisen_api_username": False,
                "raiffeisen_api_password": False,
            })

    def test_currency_rate_must_be_positive(self):
        with self.assertRaises(ValidationError):
            self.provider.raiffeisen_currency_rate = 0

    def test_refunds_are_advertised_as_partial(self):
        self.assertEqual(self.provider.support_refund, "partial")

    def test_only_rsd_and_eur_are_offered(self):
        (self.env.ref("base.RSD") | self.env.ref("base.EUR")).active = True
        names = set(self.provider._get_supported_currencies().mapped("name"))
        self.assertEqual(names, {"RSD", "EUR"})

    def test_a_currency_the_merchant_has_not_activated_is_not_offered(self):
        """Both currencies ship inactive in a fresh database, so the
        provider offers nothing until the merchant activates one."""
        self.env.ref("base.RSD").active = False
        self.env.ref("base.EUR").active = False
        self.assertFalse(self.provider._get_supported_currencies())
