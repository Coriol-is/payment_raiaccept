import logging
from datetime import timedelta
from urllib.parse import urljoin

import requests

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..raiaccept import (
    API_BASE,
    AUTH_BASE,
    COUNTRY_ISO3,
    LOGIN_ENDPOINT,
    REFRESH_ENDPOINT,
    TOKEN_EXPIRY_MARGIN,
    gateway_amount,
    format_phone,
    integration_context,
    normalize_transaction_list,
    transliterate,
)

_logger = logging.getLogger(__name__)


# Cached tokens are cleared whenever the identity behind them
# changes; see `write` below.
_TOKEN_FIELDS = frozenset({
    "raiffeisen_access_token",
    "raiffeisen_access_token_expiry",
    "raiffeisen_refresh_token",
    "raiffeisen_refresh_token_expiry",
})


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[("raiffeisen", "Raiffeisen RaiAccept")],
        ondelete={"raiffeisen": "set default"},
    )

    # ── Credential fields (no required_if_provider — validated below) ─
    raiffeisen_api_username = fields.Char(
        string="API Username",
        copy=False,
        groups="base.group_system",
    )
    raiffeisen_api_password = fields.Char(
        string="API Password",
        copy=False,
        groups="base.group_system",
    )
    raiffeisen_sandbox_username = fields.Char(
        string="Sandbox Username",
        copy=False,
        groups="base.group_system",
    )
    raiffeisen_sandbox_password = fields.Char(
        string="Sandbox Password",
        copy=False,
        groups="base.group_system",
    )
    raiffeisen_gateway_currency = fields.Selection(
        [("RSD", "RSD"), ("EUR", "EUR")],
        string="Gateway Currency",
        default="RSD",
        help="Currency used by the RaiAccept payment gateway for "
             "settlement. Must match your merchant account currency.",
    )
    raiffeisen_currency_rate = fields.Float(
        string="Currency Rate",
        default=1.0,
        digits=(12, 6),
        help="Exchange rate: store_amount × rate = gateway_amount. "
             "Set to 1.0 if store and gateway currencies are the same.",
    )

    # ── Token cache ──────────────────────────────────────────────────
    # RaiAccept issues a short-lived access token (~299 s) plus a
    # long-lived refresh token (~86399 s). Re-sending the credentials
    # on every API call is not just wasteful: the auth service answers
    # `AUTH_PASSWORD_ATTEMPTS_EXCEEDED`, so a busy shop can lock its
    # own merchant credentials out. Cache both tokens on the provider
    # and refresh with the refresh token.

    raiffeisen_access_token = fields.Char(
        string="Cached Access Token",
        copy=False,
        groups="base.group_system",
    )
    raiffeisen_access_token_expiry = fields.Datetime(
        string="Access Token Expiry",
        copy=False,
        groups="base.group_system",
    )
    raiffeisen_refresh_token = fields.Char(
        string="Cached Refresh Token",
        copy=False,
        groups="base.group_system",
    )
    raiffeisen_refresh_token_expiry = fields.Datetime(
        string="Refresh Token Expiry",
        copy=False,
        groups="base.group_system",
    )

    # ── Webhook security ─────────────────────────────────────────────
    # The documented webhook carries no signature and no shared secret
    # (https://docs.raiaccept.com/code-integration.html, "Webhook
    # notification"), so there is nothing to verify cryptographically.
    # Authenticity comes from `_apply_updates` re-fetching the order
    # from the API before believing anything; the IP allowlist below is
    # an optional extra hop for merchants whose bank contact confirmed
    # the egress addresses.

    raiffeisen_webhook_allowed_ips = fields.Char(
        string="Webhook Allowed Source IPs",
        copy=False,
        groups="base.group_system",
        help="Comma-separated list of trusted source IPs from which "
             "the gateway may POST to the webhook URL. When set, "
             "requests from any other IP are rejected with HTTP 403. "
             "Leave empty to disable the IP allowlist (LOG ONLY — the "
             "remote_addr is logged but not enforced).\n\n"
             "remote_addr is taken from werkzeug — behind a reverse "
             "proxy you MUST enable Odoo's proxy_mode (odoo.conf "
             "`proxy_mode = True`) so XFF is honored from the trusted "
             "proxy hop only. Without proxy_mode the allowlist will "
             "see the proxy IP, so allowlist that proxy IP instead. "
             "X-Forwarded-For is intentionally NOT trusted directly "
             "by this controller — that header is client-controlled.\n\n"
             "Confirm the actual RaiAccept egress IPs with your bank "
             "contact before enforcing this in production.",
    )

    # ── State-aware credential validation ────────────────────────────

    @api.constrains("state", "code",
                    "raiffeisen_api_username", "raiffeisen_api_password",
                    "raiffeisen_sandbox_username", "raiffeisen_sandbox_password")
    def _check_raiffeisen_credentials(self):
        for provider in self.filtered(lambda p: p.code == "raiffeisen"):
            if provider.state == "enabled":
                if not provider.raiffeisen_api_username \
                        or not provider.raiffeisen_api_password:
                    raise ValidationError(
                        _("Production API credentials are required when "
                          "the Raiffeisen provider is enabled.")
                    )
            elif provider.state == "test":
                if not provider.raiffeisen_sandbox_username \
                        or not provider.raiffeisen_sandbox_password:
                    raise ValidationError(
                        _("Sandbox credentials are required when "
                          "the Raiffeisen provider is in test mode.")
                    )

    @api.constrains("raiffeisen_currency_rate")
    def _check_raiffeisen_currency_rate(self):
        for provider in self.filtered(lambda p: p.code == "raiffeisen"):
            if provider.raiffeisen_currency_rate <= 0:
                raise ValidationError(
                    _("Currency rate must be a positive number.")
                )

    def write(self, vals):
        """Drop cached tokens whenever the identity behind them changes.

        Switching between sandbox and production, or rotating the API
        password in the Merchant portal, makes every cached token
        worthless. Keeping one would send a token minted for the other
        environment and produce a 401 on the customer's checkout.
        """
        invalidating = {
            "state",
            "raiffeisen_api_username", "raiffeisen_api_password",
            "raiffeisen_sandbox_username", "raiffeisen_sandbox_password",
        }
        if invalidating & set(vals) and not _TOKEN_FIELDS & set(vals):
            vals = dict(vals, **{f: False for f in _TOKEN_FIELDS})
        res = super().write(vals)
        if vals.get("state") in ("enabled", "test"):
            self.filtered(
                lambda p: p.code == "raiffeisen"
            )._raiffeisen_activate_brand_methods()
        return res

    def _raiffeisen_activate_brand_methods(self):
        """Unarchive the card brands so their icons render at checkout.

        Core ships the visa/mastercard payment.method records archived,
        and archived records silently drop out of the provider's m2m —
        the checkout then shows only the DinaCard icon. They cannot be
        activated at install time either: payment.method.write refuses
        to activate a brand while every provider supporting it is
        disabled. So they are activated here, the moment the provider
        itself is enabled or put in test mode.
        """
        brands = self.env["payment.method"].with_context(
            active_test=False
        ).search([("code", "in", ("visa", "mastercard"))])
        brands.filtered(lambda m: not m.active).write({"active": True})
        for provider in self:
            provider.payment_method_ids = [(4, m.id) for m in brands]

    # ── Feature support ──────────────────────────────────────────────

    def _compute_feature_support_fields(self):
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == "raiffeisen").update({
            "support_refund": "partial",
        })

    # ── Webhook security helpers ─────────────────────────────────────

    def _raiffeisen_webhook_allowed_ip_list(self):
        """Parse the comma-separated allowed-IP field into a set.

        Whitespace and empty entries are dropped. Returns an empty
        set when the field is unset (caller treats empty set as
        "log only, no enforcement").
        """
        self.ensure_one()
        raw = self.raiffeisen_webhook_allowed_ips or ""
        return {
            ip.strip() for ip in raw.split(",")
            if ip.strip()
        }

    def _raiffeisen_webhook_ip_allowed(self, remote_ip):
        """Return (allowed: bool, reason: str) for a webhook source IP.

        Returns (True, "no allowlist configured") when the merchant
        hasn't set any IPs (log-only mode). Returns (True, "matched")
        when remote_ip is in the configured set, else (False, "...").
        """
        self.ensure_one()
        allowed = self._raiffeisen_webhook_allowed_ip_list()
        if not allowed:
            return (True, "no allowlist configured (log-only)")
        if remote_ip in allowed:
            return (True, "ip in allowlist")
        return (False, f"ip {remote_ip} not in allowlist")

    # ── Credential helpers ───────────────────────────────────────────

    def _raiffeisen_get_credentials(self):
        """Return (username, password) based on provider state."""
        self.ensure_one()
        if self.state == "test":
            return (
                self.raiffeisen_sandbox_username or "",
                self.raiffeisen_sandbox_password or "",
            )
        return (
            self.raiffeisen_api_username or "",
            self.raiffeisen_api_password or "",
        )

    def _raiffeisen_integration_context(self):
        """Return the `integrationContext` object required by auth.

        `name` identifies the merchant's store; `vendor` and `version`
        identify who wrote and maintains the integration.
        """
        self.ensure_one()
        return integration_context(self.company_id.name)

    # ── Authentication ───────────────────────────────────────────────

    def _raiffeisen_auth_request(self, endpoint, payload):
        """POST to the RaiAccept Auth Service and return the JSON body."""
        self.ensure_one()
        url = urljoin(AUTH_BASE, endpoint)
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            body = ""
            if exc.response is not None:
                body = exc.response.text or ""
            _logger.error(
                "Raiffeisen auth %s failed: %s. Body: %s",
                endpoint, exc, body,
            )
            raise ValidationError(
                _("Failed to authenticate with Raiffeisen: %s")
                % (body[:300] or str(exc))
            ) from exc

    def _raiffeisen_store_tokens(self, data):
        """Persist the tokens returned by login or refresh.

        The refresh response carries only an access token, so the
        refresh token and its expiry are left untouched in that case.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        access_token = data.get("accessToken")
        if not access_token:
            raise ValidationError(
                _("Raiffeisen authentication failed: "
                  "no access token in response.")
            )
        vals = {
            "raiffeisen_access_token": access_token,
            "raiffeisen_access_token_expiry": now + timedelta(
                seconds=int(data.get("accessTokenExpiresIn") or 0)
            ),
        }
        if data.get("refreshToken"):
            vals["raiffeisen_refresh_token"] = data["refreshToken"]
            vals["raiffeisen_refresh_token_expiry"] = now + timedelta(
                seconds=int(data.get("refreshTokenExpiresIn") or 0)
            )
        self.sudo().write(vals)
        return access_token

    def _raiffeisen_login(self):
        """Authenticate with the API credentials and cache the tokens."""
        self.ensure_one()
        username, password = self._raiffeisen_get_credentials()
        if not username or not password:
            raise ValidationError(
                _("Raiffeisen API credentials are not configured.")
            )
        data = self._raiffeisen_auth_request(
            LOGIN_ENDPOINT,
            {
                "username": username,
                "password": password,
                "integrationContext": self._raiffeisen_integration_context(),
            },
        )
        return self._raiffeisen_store_tokens(data)

    def _raiffeisen_refresh_access_token(self):
        """Mint a new access token from the cached refresh token.

        Returns None when there is no usable refresh token or the
        refresh call fails, so the caller falls back to a full login.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        expiry = self.raiffeisen_refresh_token_expiry
        if not self.raiffeisen_refresh_token or not expiry \
                or expiry <= now + timedelta(seconds=TOKEN_EXPIRY_MARGIN):
            return None
        try:
            data = self._raiffeisen_auth_request(
                REFRESH_ENDPOINT,
                {
                    "refreshToken": self.raiffeisen_refresh_token,
                    "integrationContext":
                        self._raiffeisen_integration_context(),
                },
            )
        except ValidationError:
            _logger.info(
                "Raiffeisen: refresh token rejected, falling back to login."
            )
            return None
        return self._raiffeisen_store_tokens(data)

    def _raiffeisen_get_access_token(self, force_new=False):
        """Return a usable access token, minting one only when needed.

        Order of preference: the cached access token, then a refresh,
        then a full login with the merchant credentials.
        """
        self.ensure_one()
        now = fields.Datetime.now()
        expiry = self.raiffeisen_access_token_expiry
        if not force_new and self.raiffeisen_access_token and expiry \
                and expiry > now + timedelta(seconds=TOKEN_EXPIRY_MARGIN):
            return self.raiffeisen_access_token
        return self._raiffeisen_refresh_access_token() \
            or self._raiffeisen_login()

    # ── API request ──────────────────────────────────────────────────

    def _raiffeisen_api_request(self, method, endpoint, payload=None,
                                _retried=False):
        """Make an authenticated request to the RaiAccept API."""
        self.ensure_one()
        token = self._raiffeisen_get_access_token(force_new=_retried)
        url = urljoin(API_BASE, endpoint)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.request(
                method,
                url,
                json=payload if method == "POST" else None,
                headers=headers,
                timeout=30,
            )
            # A cached token can be revoked server-side before it
            # expires. Retry exactly once with a freshly minted one.
            if resp.status_code == 401 and not _retried:
                _logger.info(
                    "Raiffeisen API %s %s -> 401, re-authenticating once.",
                    method, endpoint,
                )
                return self._raiffeisen_api_request(
                    method, endpoint, payload, _retried=True,
                )
            if resp.status_code >= 400:
                # Log the full response body to aid debugging RaiAccept
                # validation errors. RaiAccept returns JSON with
                # field-level error details on 400 responses.
                _logger.error(
                    "Raiffeisen API %s %s -> %s\nRequest payload: %s\n"
                    "Response body: %s",
                    method, endpoint, resp.status_code,
                    payload, resp.text,
                )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.RequestException as exc:
            body = ""
            if exc.response is not None:
                body = exc.response.text or ""
            _logger.error(
                "Raiffeisen API %s %s failed: %s. Body: %s",
                method, endpoint, exc, body,
            )
            # Show a truncated body in the user-facing error so the
            # admin sees the validation reason without digging into logs.
            short = body[:300] if body else str(exc)
            raise ValidationError(
                _("Raiffeisen API request failed: %s") % short
            ) from exc

    # ── Order / Checkout ─────────────────────────────────────────────

    def _raiffeisen_create_order_and_checkout(self, tx):
        """Create an order on RaiAccept and return the checkout redirect URL."""
        self.ensure_one()
        order_payload = self._raiffeisen_build_order_payload(tx)

        # Step 1: Create order entry
        order_resp = self._raiffeisen_api_request(
            "POST", "/orders", order_payload
        )
        order_id = order_resp.get("orderIdentification")
        if not order_id:
            raise ValidationError(
                _("Raiffeisen: order creation returned no "
                  "orderIdentification.")
            )
        tx.raiffeisen_order_id = order_id

        # Step 2: Create payment session (checkout). The docs require
        # the same parameters and values as step 1.
        checkout_resp = self._raiffeisen_api_request(
            "POST", f"/orders/{order_id}/checkout", order_payload
        )
        redirect_url = checkout_resp.get("paymentRedirectURL")
        if not redirect_url:
            raise ValidationError(
                _("Raiffeisen: checkout returned no paymentRedirectURL.")
            )
        return redirect_url

    @staticmethod
    def _raiffeisen_gateway_amount(amount, rate=1.0):
        """Convert a store amount into the amount RaiAccept expects."""
        return gateway_amount(amount, rate)

    def _raiffeisen_build_order_payload(self, tx):
        """Build the CreateOrderEntryRequest payload from a transaction."""
        self.ensure_one()
        partner = tx.partner_id

        gateway_currency = self.raiffeisen_gateway_currency or "RSD"
        amount = self._raiffeisen_gateway_amount(
            tx.amount, self.raiffeisen_currency_rate
        )

        country_a2 = partner.country_id.code or ""
        country_a3 = COUNTRY_ISO3.get(country_a2)
        if not country_a3:
            raise ValidationError(
                _("Cannot process payment: country '%s' (%s) is not "
                  "supported by the Raiffeisen payment gateway. "
                  "Please contact support to add this country.",
                  partner.country_id.name or "N/A", country_a2)
            )

        # Strip trailing slash to avoid double-slash in callback URLs like
        # "https://example.rs//payment/raiffeisen/return".
        base_url = self.get_base_url().rstrip("/")

        name_parts = (partner.name or "Customer").split()
        first_name = name_parts[0]
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 \
            else first_name

        billing_address = {
            "firstName": transliterate(first_name, 32),
            "lastName": transliterate(last_name, 32),
            "addressStreet1": transliterate(partner.street or "N/A", 50),
            "city": transliterate(partner.city or "N/A", 50),
            "postalCode": transliterate(partner.zip or "00000", 16),
            "country": country_a3,
        }
        if partner.street2:
            billing_address["addressStreet2"] = transliterate(
                partner.street2, 50
            )
        # RaiAccept caps `state` at 3 characters. Odoo's
        # res.country.state.code is typically "RS-00" or "00" — strip
        # the country prefix, and omit the field when no short code can
        # be produced (the field is optional).
        if partner.state_id and partner.state_id.code:
            raw_code = partner.state_id.code
            if "-" in raw_code:
                raw_code = raw_code.split("-", 1)[1]
            if raw_code and len(raw_code) <= 3:
                billing_address["state"] = raw_code

        consumer = {
            "firstName": billing_address["firstName"],
            "lastName": billing_address["lastName"],
            "email": partner.email or "",
        }
        # Odoo 19 merged partner.mobile into partner.phone. RaiAccept
        # accepts "+381..." or "00381...", max 15 chars, no spaces.
        phone = format_phone(partner.phone)
        if phone:
            consumer["phone"] = phone
            consumer["mobilePhone"] = phone
        consumer_ip = tx._raiffeisen_consumer_ip()
        if consumer_ip:
            consumer["ipAddress"] = consumer_ip

        merchant_reference = tx.raiffeisen_merchant_reference \
            or tx._raiffeisen_merchant_reference()
        description = transliterate(tx.reference or "Order", 200)

        return {
            "billingAddress": billing_address,
            "shippingAddress": billing_address,
            "consumer": consumer,
            "invoice": {
                "amount": amount,
                "currency": gateway_currency,
                "description": description,
                "merchantOrderReference": merchant_reference,
                "items": [{
                    "description": transliterate(
                        tx.reference or "Order", 100
                    ),
                    "numberOfItems": 1,
                    "price": amount,
                }],
            },
            "urls": {
                "successUrl": tx._raiffeisen_return_url(base_url),
                "failUrl": tx._raiffeisen_return_url(base_url),
                "cancelUrl": tx._raiffeisen_return_url(base_url),
                "notificationUrl": (
                    f"{base_url}/payment/raiffeisen/webhook"
                ),
            },
            "paymentMethodPreference": "CARD",
        }

    # ── Order / transaction queries ──────────────────────────────────

    def _raiffeisen_get_order_status(self, order_id):
        """Query the order from the gateway.

        Returns the order object, whose `status` is one of DRAFT,
        CHECKOUT, PAID, PARTIALLY_REFUNDED, FULLY_REFUNDED, FAILED,
        CANCELED, ABANDONED. Note that this payload contains the order
        and its invoice only — it carries no list of transactions.
        """
        self.ensure_one()
        return self._raiffeisen_api_request("GET", f"/orders/{order_id}")

    def _raiffeisen_get_transaction(self, order_id, transaction_id):
        """Query a single transaction of an order."""
        self.ensure_one()
        return self._raiffeisen_api_request(
            "GET", f"/orders/{order_id}/transactions/{transaction_id}"
        )

    def _raiffeisen_list_transactions(self, order_id):
        """Return the order's transactions as a list of dicts.

        The docs label this endpoint "Retrieve all transactions" but
        document it as a POST whose example response is a single
        transaction object rather than an array. Until that is settled
        against the live API, try GET first, fall back to POST when the
        method is refused, and accept every plausible response shape.
        """
        self.ensure_one()
        endpoint = f"/orders/{order_id}/transactions"
        try:
            data = self._raiffeisen_api_request("GET", endpoint)
        except ValidationError:
            data = self._raiffeisen_api_request("POST", endpoint, {})
        return self._raiffeisen_normalize_transaction_list(data)

    @staticmethod
    def _raiffeisen_normalize_transaction_list(data):
        """Coerce a transactions response into a list of dicts."""
        return normalize_transaction_list(data)

    def _raiffeisen_refund(self, order_id, transaction_id, amount,
                           currency):
        """Issue a refund via the API.

        The response carries only `transactionId`; the refund's own
        status has to be read back from the transaction endpoint.
        """
        self.ensure_one()
        return self._raiffeisen_api_request(
            "POST",
            f"/orders/{order_id}/transactions/{transaction_id}/refund",
            {"amount": amount, "currency": currency},
        )

    # ── Odoo payment provider interface ──────────────────────────────

    def _get_supported_currencies(self):
        if self.code != "raiffeisen":
            return super()._get_supported_currencies()
        return self.env["res.currency"].search([
            ("name", "in", ["RSD", "EUR"]),
        ])

    def _get_default_payment_method_codes(self):
        if self.code != "raiffeisen":
            return super()._get_default_payment_method_codes()
        return ["card"]

