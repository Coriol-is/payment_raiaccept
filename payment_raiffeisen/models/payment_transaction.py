import logging
from urllib.parse import quote, urlparse, parse_qsl, urlunparse

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.http import request

from ..raiaccept import (
    ORDER_STATUS_MAP,
    TRANSACTION_STATUS_MAP,
    gateway_amount,
    normalize_webhook,
    sanitize_merchant_reference,
    select_purchase_transaction,
    unwrap_transaction,
)

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    raiffeisen_order_id = fields.Char(
        string="RaiAccept Order ID",
        readonly=True,
    )
    raiffeisen_tx_id = fields.Char(
        string="RaiAccept Transaction ID",
        readonly=True,
    )
    raiffeisen_merchant_reference = fields.Char(
        string="RaiAccept Merchant Order Reference",
        readonly=True,
        help="The sanitized reference actually sent to RaiAccept. It "
             "can differ from the Odoo reference, which may contain "
             "characters the gateway rejects.",
    )
    raiffeisen_gateway_currency = fields.Char(
        string="Gateway Currency (snapshot)",
        readonly=True,
        help="Currency used at checkout time, snapshotted from provider.",
    )
    raiffeisen_currency_rate = fields.Float(
        string="Currency Rate (snapshot)",
        readonly=True,
        digits=(12, 6),
        help="Exchange rate at checkout time, snapshotted from provider.",
    )

    # ── Helpers ──────────────────────────────────────────────────────

    def _raiffeisen_merchant_reference(self):
        """Return an API-safe merchant order reference for this tx."""
        self.ensure_one()
        return sanitize_merchant_reference(
            self.reference, fallback=f"ODOO-{self.id}"
        )

    def _raiffeisen_return_url(self, base_url):
        """Return the customer-facing callback URL for this tx."""
        self.ensure_one()
        return (
            f"{base_url}/payment/raiffeisen/return"
            f"?ref={quote(self.reference or '', safe='')}"
        )

    def _raiffeisen_consumer_ip(self):
        """Return the customer's IP address when one is available.

        RaiAccept marks `ipAddress` as recommended: sending it raises
        the chance of a frictionless 3-D Secure flow. It is only known
        when the checkout runs inside an HTTP request.
        """
        self.ensure_one()
        if request and request.httprequest:
            return request.httprequest.remote_addr or ""
        return ""

    # ── Redirect rendering ───────────────────────────────────────────

    def _get_specific_rendering_values(self, processing_values):
        """Override of payment to return Raiffeisen-specific rendering values.

        Creates the RaiAccept order and returns the redirect URL.

        Note: self.ensure_one() from `_get_processing_values`
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "raiffeisen":
            return res

        try:
            # Snapshot provider config at checkout time
            provider = self.provider_id
            self.raiffeisen_gateway_currency = (
                provider.raiffeisen_gateway_currency or "RSD"
            )
            self.raiffeisen_currency_rate = (
                provider.raiffeisen_currency_rate or 1.0
            )
            self.raiffeisen_merchant_reference = \
                self._raiffeisen_merchant_reference()

            redirect_url = provider._raiffeisen_create_order_and_checkout(self)
        except ValidationError as error:
            # Returning an empty dict would leave `api_url` undefined
            # and make QWeb raise while rendering the redirect form, so
            # the customer would get a traceback instead of the reason.
            # Point the form at the payment status page, where Odoo
            # shows the error message set here.
            self._set_error(str(error))
            return {"api_url": "/payment/status", "url_params": {}}

        # The redirect form submits with method="get", which makes the
        # browser drop any query string on the action URL and rebuild
        # it from the form's hidden inputs. Split the RaiAccept URL so
        # the query params survive as hidden fields.
        parsed = urlparse(redirect_url)
        api_url = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
        )
        url_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        return {"api_url": api_url, "url_params": url_params}

    # ── Webhook payload normalization ────────────────────────────────

    @api.model
    def _raiffeisen_normalize_webhook(self, payload):
        """Flatten a RaiAccept webhook body into flat payment data."""
        return normalize_webhook(payload)

    # ── Reference extraction ─────────────────────────────────────────

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        """Extract the transaction reference from payment data."""
        if provider_code != "raiffeisen":
            return super()._extract_reference(provider_code, payment_data)

        # Return redirect: ref in query params
        ref = payment_data.get("ref")
        if ref:
            return ref

        # Webhook: resolve by transactionId FIRST (critical for refunds,
        # since a refund child shares the parent's orderIdentification
        # and merchantOrderReference)
        tx_id = payment_data.get("transactionId")
        if tx_id:
            tx = self.search(
                [("raiffeisen_tx_id", "=", tx_id)], limit=1
            )
            if tx:
                return tx.reference

        # Then the merchant reference we actually sent, which may have
        # been sanitized and so differ from the Odoo reference.
        merchant_ref = payment_data.get("merchantOrderReference")
        if merchant_ref:
            tx = self.search(
                [("raiffeisen_merchant_reference", "=", merchant_ref)],
                limit=1,
            )
            if tx:
                return tx.reference
            return merchant_ref

        # Fallback: look up by gateway orderIdentification
        order_id = payment_data.get("orderIdentification")
        if order_id:
            tx = self.search(
                [("raiffeisen_order_id", "=", order_id)], limit=1
            )
            if tx:
                return tx.reference

        return super()._extract_reference(provider_code, payment_data)

    # ── Amount validation ────────────────────────────────────────────

    def _extract_amount_data(self, payment_data):
        """Override of payment to skip base amount validation.

        Return None → Odoo skips amount validation. The webhook body is
        not authoritative, so the amount is verified in `_apply_updates`
        against a fresh read of the order from the API.
        """
        if self.provider_code != "raiffeisen":
            return super()._extract_amount_data(payment_data)
        return None

    # ── Apply gateway status updates ─────────────────────────────────

    def _apply_updates(self, payment_data):
        """Process the RaiAccept payment response and update state."""
        super()._apply_updates(payment_data)
        if self.provider_code != "raiffeisen":
            return

        # A webhook is the only place the gateway volunteers the
        # transaction id, and the order endpoint never returns one, so
        # record it whenever it shows up.
        notified_tx_id = payment_data.get("transactionId")
        if notified_tx_id and not self.raiffeisen_tx_id:
            self.raiffeisen_tx_id = notified_tx_id

        if not self.raiffeisen_order_id:
            self._set_pending()
            return

        if self.source_transaction_id:
            self._raiffeisen_apply_refund_updates()
        else:
            self._raiffeisen_apply_payment_updates()

    def _raiffeisen_apply_payment_updates(self):
        """Verify and apply the gateway's view of a payment."""
        self.ensure_one()
        try:
            order_data = self.provider_id._raiffeisen_get_order_status(
                self.raiffeisen_order_id
            )
        except Exception:
            _logger.warning(
                "Raiffeisen: could not query order %s",
                self.raiffeisen_order_id, exc_info=True,
            )
            # Do NOT fall back to unverified notification data.
            self._set_pending()
            return

        if not self._raiffeisen_amount_matches(order_data):
            return

        # The order payload carries no transactions, so when the id is
        # still unknown, ask the transactions endpoint for it. A failure
        # here is not fatal for the payment — it only costs the ability
        # to refund from Odoo later.
        if not self.raiffeisen_tx_id:
            self._raiffeisen_fetch_purchase_tx_id()

        self.provider_reference = (
            self.raiffeisen_order_id or self.raiffeisen_tx_id or ""
        )
        status = order_data.get("status") or ""
        self._raiffeisen_set_state(
            ORDER_STATUS_MAP.get(status), status,
        )

    def _raiffeisen_apply_refund_updates(self):
        """Apply the gateway's view of a refund child transaction."""
        self.ensure_one()
        if not self.raiffeisen_tx_id:
            self._set_pending()
            return
        try:
            data = self.provider_id._raiffeisen_get_transaction(
                self.raiffeisen_order_id, self.raiffeisen_tx_id,
            )
        except Exception:
            _logger.warning(
                "Raiffeisen: could not query refund transaction %s",
                self.raiffeisen_tx_id, exc_info=True,
            )
            self._set_pending()
            return

        tx_data = unwrap_transaction(data)
        if not self._raiffeisen_refund_amount_matches(tx_data):
            return

        self.provider_reference = self.raiffeisen_tx_id
        status = tx_data.get("status") or ""
        self._raiffeisen_set_state(
            TRANSACTION_STATUS_MAP.get(status), status,
        )

    def _raiffeisen_fetch_purchase_tx_id(self):
        """Store the id of this order's PURCHASE transaction."""
        self.ensure_one()
        try:
            txs = self.provider_id._raiffeisen_list_transactions(
                self.raiffeisen_order_id
            )
        except Exception:
            _logger.warning(
                "Raiffeisen: could not list transactions of order %s; "
                "refunds from Odoo will not be possible until it is known.",
                self.raiffeisen_order_id, exc_info=True,
            )
            return
        purchase = select_purchase_transaction(txs)
        if purchase and purchase.get("transactionId"):
            self.raiffeisen_tx_id = purchase["transactionId"]

    # ── Verification helpers ─────────────────────────────────────────

    def _raiffeisen_amount_matches(self, order_data):
        """Return True when the gateway's invoice matches our snapshot.

        Sets the transaction to error and returns False otherwise.
        """
        self.ensure_one()
        invoice = order_data.get("invoice") or {}
        gw_amount = invoice.get("amount")
        gw_currency = invoice.get("currency")
        if gw_amount is None or not gw_currency:
            return True  # Nothing to compare against.

        expected_currency = self.raiffeisen_gateway_currency or "RSD"
        if gw_currency != expected_currency:
            self._set_error(state_message=_(
                "Currency mismatch: gateway returned %s, expected %s.",
                gw_currency, expected_currency,
            ))
            return False

        expected = gateway_amount(
            self.amount, self.raiffeisen_currency_rate
        )
        if abs(float(gw_amount) - expected) > 0.01:
            self._set_error(state_message=_(
                "Amount mismatch: gateway returned %s, expected %s.",
                gw_amount, expected,
            ))
            return False
        return True

    def _raiffeisen_refund_amount_matches(self, tx_data):
        """Return True when a refund's gateway amount matches ours."""
        self.ensure_one()
        gw_amount = tx_data.get("transactionAmount")
        if gw_amount is None:
            return True
        expected = gateway_amount(
            abs(self.amount), self.raiffeisen_currency_rate
        )
        if abs(float(gw_amount) - expected) > 0.01:
            self._set_error(state_message=_(
                "Refund amount mismatch: gateway returned %s, expected %s.",
                gw_amount, expected,
            ))
            return False
        return True

    def _raiffeisen_set_state(self, odoo_state, gateway_status):
        """Move the transaction to the state the gateway reports.

        An unmapped status is left pending and logged rather than
        guessed at: the documented sets are closed, so an unknown value
        means the API changed and a human needs to look.
        """
        self.ensure_one()
        if odoo_state is None:
            _logger.warning(
                "Raiffeisen: unmapped gateway status %r on %s; "
                "leaving the transaction pending.",
                gateway_status, self.reference,
            )
            self._set_pending()
        elif odoo_state == "done":
            self._set_done()
        elif odoo_state == "cancel":
            self._set_canceled(state_message=_(
                "Payment was canceled on the Raiffeisen gateway (%s).",
                gateway_status,
            ))
        elif odoo_state == "error":
            self._set_error(state_message=_(
                "Payment failed on the Raiffeisen gateway (%s).",
                gateway_status,
            ))
        else:
            self._set_pending()

    # ── Refund ───────────────────────────────────────────────────────

    def _send_refund_request(self):
        """Override of payment to send a refund request to RaiAccept.

        Called on the CHILD refund transaction; the gateway ids come
        from the source transaction. The refund response contains only
        `transactionId`, so the resulting state is read back from the
        transaction endpoint rather than guessed from the response.
        """
        if self.provider_code != "raiffeisen":
            return super()._send_refund_request()

        source_tx = self.source_transaction_id
        if not source_tx:
            raise ValidationError(
                _("Cannot refund: no source transaction found.")
            )

        order_id = source_tx.raiffeisen_order_id
        tx_id = source_tx.raiffeisen_tx_id
        if not order_id:
            raise ValidationError(
                _("Cannot refund: the original payment has no "
                  "Raiffeisen order id.")
            )
        if not tx_id:
            # The order endpoint never returns transaction ids, so an
            # older payment may not have one recorded yet. Fetch it now
            # instead of refusing the refund outright.
            source_tx._raiffeisen_fetch_purchase_tx_id()
            tx_id = source_tx.raiffeisen_tx_id
        if not tx_id:
            raise ValidationError(
                _("Cannot refund: the Raiffeisen transaction id of the "
                  "original payment could not be determined.")
            )

        rate = source_tx.raiffeisen_currency_rate or 1.0
        currency = source_tx.raiffeisen_gateway_currency or "RSD"
        provider = self.provider_id
        # self.amount is negative for refunds; the gateway wants a
        # positive number.
        amount = gateway_amount(abs(self.amount), rate)

        resp = provider._raiffeisen_refund(
            order_id, tx_id, amount, currency
        )

        refund_tx_id = resp.get("transactionId", "")
        self.raiffeisen_order_id = order_id
        self.raiffeisen_tx_id = refund_tx_id
        self.raiffeisen_gateway_currency = currency
        self.raiffeisen_currency_rate = rate
        self.raiffeisen_merchant_reference = \
            source_tx.raiffeisen_merchant_reference
        self.provider_reference = refund_tx_id

        if not refund_tx_id:
            self._set_error(state_message=_(
                "Raiffeisen accepted the refund but returned no "
                "transaction id, so its outcome cannot be confirmed."
            ))
            return

        # Read the refund's real status back from the gateway.
        self._raiffeisen_apply_refund_updates()
