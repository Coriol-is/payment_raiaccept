import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class RaiffeisenController(http.Controller):
    _return_url = "/payment/raiffeisen/return"
    _webhook_url = "/payment/raiffeisen/webhook"

    @http.route(
        _return_url,
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        save_session=False,
    )
    def raiffeisen_return(self, **data):
        """Handle customer return from the RaiAccept payment page.

        After payment, the gateway redirects the customer back here
        via successUrl / failUrl / cancelUrl. The redirect itself says
        nothing trustworthy about the outcome — `_apply_updates` reads
        the order back from the API.
        """
        _logger.info(
            "Raiffeisen return with ref=%s", data.get("ref", "N/A")
        )
        request.env["payment.transaction"].sudo()._process(
            "raiffeisen", data
        )
        return request.redirect("/payment/status")

    @http.route(
        _webhook_url,
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def raiffeisen_webhook(self, **data):
        """Handle asynchronous webhook notifications from RaiAccept.

        RaiAccept sends an unsigned JSON body — the documented
        notification carries no signature, MAC or shared secret. So the
        webhook is treated as a hint that something changed, never as
        evidence of what changed: `_apply_updates` re-fetches the order
        from the API and believes only that.

        On top of that, a merchant who has confirmed the gateway's
        egress addresses with their bank can set an IP allowlist, which
        is enforced here. It defaults to empty, in which case the source
        IP is only logged — a misconfigured allowlist during onboarding
        would otherwise silently drop every notification.

        remote_addr comes straight from werkzeug. X-Forwarded-For is
        deliberately not read here: it is client-controlled, and a
        forged header would defeat the allowlist. Behind a reverse
        proxy, enable Odoo's `proxy_mode` so werkzeug rewrites
        remote_addr from the trusted hop.
        """
        remote_ip = request.httprequest.remote_addr or "?"

        # The provider record is needed for the IP check before any
        # transaction is resolved. Prefer an enabled provider over a
        # test one; a disabled provider is used only as a last resort so
        # that the request is still logged against something.
        providers = request.env["payment.provider"].sudo().search(
            [("code", "=", "raiffeisen")], order="id asc",
        )
        provider = (
            providers.filtered(lambda p: p.state == "enabled")[:1]
            or providers.filtered(lambda p: p.state == "test")[:1]
            or providers[:1]
        )
        if not provider:
            _logger.warning(
                "Raiffeisen webhook: no raiffeisen provider configured "
                "(remote_ip=%s)", remote_ip,
            )
            return request.make_json_response(
                {"error": "provider not configured"}, status=503,
            )

        ip_ok, ip_reason = provider._raiffeisen_webhook_ip_allowed(remote_ip)
        if not ip_ok:
            _logger.warning(
                "Raiffeisen webhook REJECTED: %s (remote_ip=%s)",
                ip_reason, remote_ip,
            )
            return request.make_json_response(
                {"error": "source ip not allowed"}, status=403,
            )

        try:
            payload = json.loads(request.httprequest.data or b"{}")
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _logger.warning(
                "Raiffeisen webhook: invalid JSON payload (remote_ip=%s)",
                remote_ip,
            )
            return request.make_json_response(
                {"error": "invalid payload"}, status=400
            )

        if not isinstance(payload, dict):
            _logger.warning(
                "Raiffeisen webhook: payload is not a JSON object "
                "(remote_ip=%s)", remote_ip,
            )
            return request.make_json_response(
                {"error": "expected JSON object"}, status=400
            )

        payment_data = request.env["payment.transaction"].sudo() \
            ._raiffeisen_normalize_webhook(payload)

        if not payment_data.get("orderIdentification") \
                and not payment_data.get("transactionId") \
                and not payment_data.get("merchantOrderReference"):
            _logger.warning(
                "Raiffeisen webhook: payload identifies no order, "
                "transaction or merchant reference (remote_ip=%s)",
                remote_ip,
            )
            return request.make_json_response(
                {"error": "malformed payload structure"}, status=400
            )

        # Log only non-PII identifiers — no customer data
        _logger.info(
            "Raiffeisen webhook ACCEPTED: order=%s tx=%s type=%s status=%s "
            "code=%s remote_ip=%s ip_check=%s",
            payment_data.get("orderIdentification", "?"),
            payment_data.get("transactionId", "?"),
            payment_data.get("transactionType", "?"),
            payment_data.get("transactionStatus", "?"),
            payment_data.get("statusCode", "?"),
            remote_ip,
            ip_reason,
        )

        try:
            request.env["payment.transaction"].sudo()._process(
                "raiffeisen", payment_data
            )
        except Exception:
            _logger.exception("Raiffeisen webhook processing failed")
            return request.make_json_response(
                {"error": "processing failed"}, status=500
            )

        # Acknowledge receipt — RaiAccept expects HTTP 200
        return ""
