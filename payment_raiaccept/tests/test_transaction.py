"""Transaction-side tests: payload, resolution, state, refunds.

These cover the three failures that made the previous version unusable
in production and that no unit test could see, because each one was
consistent with the module's own assumptions: amounts multiplied into
minor units the API does not use, order statuses that the API never
emits, and a transaction id read from a field the order endpoint does
not return, which made refunds impossible.
"""

import json
import pathlib
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import RaiffeisenCommon

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

PROVIDER = "odoo.addons.payment_raiaccept.models.payment_provider.PaymentProvider"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@tagged("post_install", "-at_install")
class TestOrderPayload(RaiffeisenCommon):

    def setUp(self):
        super().setUp()
        self.tx = self._create_tx(amount=4000.0)

    def _payload(self):
        return self.provider._raiffeisen_build_order_payload(self.tx)

    def test_amount_is_the_order_total_not_minor_units(self):
        payload = self._payload()
        self.assertEqual(payload["invoice"]["amount"], 4000.00)
        self.assertEqual(payload["invoice"]["items"][0]["price"], 4000.00)

    def test_amount_survives_decimals(self):
        self.tx.amount = 825.50
        self.assertEqual(self._payload()["invoice"]["amount"], 825.50)

    def test_amount_applies_the_configured_rate(self):
        self.provider.raiffeisen_currency_rate = 117.5
        self.tx.amount = 100.0
        self.assertEqual(self._payload()["invoice"]["amount"], 11750.00)

    def test_merchant_reference_is_sanitized_and_recorded(self):
        tx = self._create_tx(reference="INV/2026/00001")
        payload = self.provider._raiffeisen_build_order_payload(tx)
        self.assertEqual(
            payload["invoice"]["merchantOrderReference"], "INV-2026-00001"
        )

    def test_addresses_are_transliterated_to_latin(self):
        payload = self._payload()
        billing = payload["billingAddress"]
        self.assertEqual(billing["firstName"], "Milica")
        self.assertEqual(billing["addressStreet1"], "Resavska 1")
        self.assertEqual(billing["city"], "Beograd")
        self.assertEqual(billing["country"], "SRB")

    def test_phone_is_normalized_into_both_fields(self):
        consumer = self._payload()["consumer"]
        self.assertEqual(consumer["phone"], "+381113202100")
        self.assertEqual(consumer["mobilePhone"], "+381113202100")

    def test_unsupported_country_is_refused_with_a_clear_error(self):
        self.partner.country_id = self.env.ref("base.tv")
        with self.assertRaises(ValidationError):
            self._payload()

    def test_callback_urls_are_absolute_and_reference_the_tx(self):
        urls = self._payload()["urls"]
        for key in ("successUrl", "failUrl", "cancelUrl"):
            self.assertIn("/payment/raiffeisen/return?ref=", urls[key])
            self.assertNotIn("//payment", urls[key].split("://", 1)[1])
        self.assertTrue(
            urls["notificationUrl"].endswith("/payment/raiffeisen/webhook")
        )

    def test_reference_in_the_callback_url_is_percent_encoded(self):
        tx = self._create_tx(reference="S00042 1")
        urls = self.provider._raiffeisen_build_order_payload(tx)["urls"]
        self.assertIn("ref=S00042%201", urls["successUrl"])

    def test_payment_method_preference_is_card(self):
        self.assertEqual(self._payload()["paymentMethodPreference"], "CARD")

    def test_wallet_method_sets_the_matching_preference(self):
        for xmlid, expected in (
            ("payment_raiaccept.payment_method_apple_pay", "APPLE_PAY"),
            ("payment_raiaccept.payment_method_google_pay", "GOOGLE_PAY"),
        ):
            method = self.env.ref(xmlid)
            tx = self._create_tx(reference=f"S-{expected}")
            tx.payment_method_id = method
            payload = self.provider._raiffeisen_build_order_payload(tx)
            self.assertEqual(payload["paymentMethodPreference"], expected)

    def test_wallet_methods_ship_archived(self):
        for xmlid in (
            "payment_raiaccept.payment_method_apple_pay",
            "payment_raiaccept.payment_method_google_pay",
        ):
            self.assertFalse(self.env.ref(xmlid).active)


@tagged("post_install", "-at_install")
class TestReferenceResolution(RaiffeisenCommon):

    def setUp(self):
        super().setUp()
        self.tx = self._create_tx()
        self.tx.write({
            "raiffeisen_order_id": "ORD-1",
            "raiffeisen_tx_id": "TX-1",
            "raiffeisen_merchant_reference": "S00042-1",
        })
        self.model = self.env["payment.transaction"]

    def test_return_redirect_uses_the_ref_parameter(self):
        self.assertEqual(
            self.model._extract_reference("raiffeisen", {"ref": "S00042-1"}),
            "S00042-1",
        )

    def test_transaction_id_wins_over_the_order(self):
        """A refund child shares its parent's order and merchant ref."""
        refund = self._create_tx(reference="S00042-1-R", amount=-100.0)
        refund.write({
            "raiffeisen_order_id": "ORD-1",
            "raiffeisen_tx_id": "TX-REFUND",
            "raiffeisen_merchant_reference": "S00042-1",
        })
        found = self.model._extract_reference("raiffeisen", {
            "transactionId": "TX-REFUND",
            "orderIdentification": "ORD-1",
            "merchantOrderReference": "S00042-1",
        })
        self.assertEqual(found, "S00042-1-R")

    def test_sanitized_merchant_reference_resolves_the_transaction(self):
        tx = self._create_tx(reference="INV/2026/00001")
        tx.raiffeisen_merchant_reference = "INV-2026-00001"
        found = self.model._extract_reference(
            "raiffeisen", {"merchantOrderReference": "INV-2026-00001"}
        )
        self.assertEqual(found, "INV/2026/00001")

    def test_order_identification_is_the_last_resort(self):
        found = self.model._extract_reference(
            "raiffeisen", {"orderIdentification": "ORD-1"}
        )
        self.assertEqual(found, "S00042-1")

    def test_webhook_body_resolves_end_to_end(self):
        """The documented notification must find its transaction."""
        self.tx.write({
            "raiffeisen_order_id":
                "R-001-ORD-d68b0e14-66c0-4a89-940a-9438cf5e2e57",
            "raiffeisen_tx_id": False,
            "raiffeisen_merchant_reference": "12345678940",
        })
        data = self.model._raiffeisen_normalize_webhook(
            fixture("webhook_purchase_success.json")
        )
        self.assertEqual(
            self.model._extract_reference("raiffeisen", data),
            self.tx.reference,
        )

    def test_amount_validation_is_delegated_to_the_gateway(self):
        self.assertIsNone(self.tx._extract_amount_data({"amount": 1}))


@tagged("post_install", "-at_install")
class TestPaymentUpdates(RaiffeisenCommon):

    def setUp(self):
        super().setUp()
        self.tx = self._create_tx(amount=4000.0)
        self.tx.write({
            "raiffeisen_order_id": "ORD-1",
            "raiffeisen_gateway_currency": "RSD",
            "raiffeisen_currency_rate": 1.0,
        })

    def _order(self, status="PAID", amount=4000.00, currency="RSD"):
        data = fixture("order_details.json")
        data["status"] = status
        data["invoice"]["amount"] = amount
        data["invoice"]["currency"] = currency
        return data

    def _apply(self, order, transactions=None):
        with patch(f"{PROVIDER}._raiffeisen_get_order_status",
                   return_value=order), \
             patch(f"{PROVIDER}._raiffeisen_list_transactions",
                   return_value=transactions or []):
            self.tx._apply_updates({})

    def test_paid_order_completes_the_transaction(self):
        self._apply(self._order("PAID"))
        self.assertEqual(self.tx.state, "done")

    def test_checkout_stays_pending(self):
        self._apply(self._order("CHECKOUT"))
        self.assertEqual(self.tx.state, "pending")

    def test_refunded_order_still_counts_as_paid(self):
        self._apply(self._order("PARTIALLY_REFUNDED"))
        self.assertEqual(self.tx.state, "done")

    def test_failed_order_errors(self):
        self._apply(self._order("FAILED"))
        self.assertEqual(self.tx.state, "error")

    def test_canceled_and_abandoned_orders_cancel(self):
        self._apply(self._order("CANCELED"))
        self.assertEqual(self.tx.state, "cancel")
        other = self._create_tx(reference="S00043-1")
        other.raiffeisen_order_id = "ORD-2"
        with patch(f"{PROVIDER}._raiffeisen_get_order_status",
                   return_value=self._order("ABANDONED")), \
             patch(f"{PROVIDER}._raiffeisen_list_transactions",
                   return_value=[]):
            other._apply_updates({})
        self.assertEqual(other.state, "cancel")

    def test_unknown_status_stays_pending_instead_of_guessing(self):
        self._apply(self._order("SOMETHING_NEW"))
        self.assertEqual(self.tx.state, "pending")

    def test_amount_mismatch_errors_rather_than_completing(self):
        self._apply(self._order("PAID", amount=1.00))
        self.assertEqual(self.tx.state, "error")

    def test_currency_mismatch_errors(self):
        self._apply(self._order("PAID", currency="EUR"))
        self.assertEqual(self.tx.state, "error")

    def test_transaction_id_comes_from_the_transactions_endpoint(self):
        """The order payload has no transactions; without this call the
        transaction id stays empty and refunds are impossible."""
        self._apply(self._order("PAID"), [fixture("transactions_single.json")])
        self.assertEqual(
            self.tx.raiffeisen_tx_id,
            "R-001-TX-ab2301a6-104c-436e-bf60-ccc0bc9d6ae2",
        )

    def test_transaction_id_is_taken_from_the_webhook_when_present(self):
        with patch(f"{PROVIDER}._raiffeisen_get_order_status",
                   return_value=self._order("PAID")), \
             patch(f"{PROVIDER}._raiffeisen_list_transactions") as lister:
            self.tx._apply_updates({"transactionId": "TX-FROM-HOOK"})
        self.assertEqual(self.tx.raiffeisen_tx_id, "TX-FROM-HOOK")
        lister.assert_not_called()

    def test_unreachable_gateway_leaves_the_transaction_pending(self):
        with patch(f"{PROVIDER}._raiffeisen_get_order_status",
                   side_effect=ValidationError("boom")):
            self.tx._apply_updates({})
        self.assertEqual(self.tx.state, "pending")

    def test_transactions_lookup_failure_does_not_break_the_payment(self):
        with patch(f"{PROVIDER}._raiffeisen_get_order_status",
                   return_value=self._order("PAID")), \
             patch(f"{PROVIDER}._raiffeisen_list_transactions",
                   side_effect=ValidationError("boom")):
            self.tx._apply_updates({})
        self.assertEqual(self.tx.state, "done")
        self.assertFalse(self.tx.raiffeisen_tx_id)

    def test_missing_order_id_leaves_it_pending(self):
        tx = self._create_tx(reference="S00099-1")
        tx._apply_updates({})
        self.assertEqual(tx.state, "pending")


@tagged("post_install", "-at_install")
class TestRefunds(RaiffeisenCommon):

    def setUp(self):
        super().setUp()
        self.source = self._create_tx(amount=4000.0)
        self.source.write({
            "state": "done",
            "raiffeisen_order_id":
                "D-001-ORD-edf3e8b2-8b0e-404a-b55a-e6b3c2dd7780",
            "raiffeisen_tx_id": "D-001-TX-purchase",
            "raiffeisen_gateway_currency": "RSD",
            "raiffeisen_currency_rate": 1.0,
            "raiffeisen_merchant_reference": "12345678922",
        })
        self.refund = self._create_tx(
            reference="S00042-1-R", amount=-2000.0,
            source_transaction_id=self.source.id, operation="refund",
        )

    def test_refund_sends_a_decimal_amount(self):
        with patch(f"{PROVIDER}._raiffeisen_refund",
                   return_value={"transactionId": "D-001-TX-refund"}) as call, \
             patch(f"{PROVIDER}._raiffeisen_get_transaction",
                   return_value=fixture("transaction_refund.json")):
            self.refund._send_refund_request()
        _order_id, tx_id, amount, currency = call.call_args.args
        self.assertEqual(tx_id, "D-001-TX-purchase")
        self.assertEqual(amount, 2000.00)
        self.assertEqual(currency, "RSD")

    def test_successful_refund_is_confirmed_from_the_gateway(self):
        """The refund response carries only an id — the status has to be
        read back, not assumed."""
        with patch(f"{PROVIDER}._raiffeisen_refund",
                   return_value={"transactionId":
                                 "D-001-TX-ffa6c2b4-b1d7-431d-8452-ebf858b2b175"}), \
             patch(f"{PROVIDER}._raiffeisen_get_transaction",
                   return_value=fixture("transaction_refund.json")):
            self.refund._send_refund_request()
        self.assertEqual(self.refund.state, "done")
        self.assertEqual(
            self.refund.provider_reference,
            "D-001-TX-ffa6c2b4-b1d7-431d-8452-ebf858b2b175",
        )

    def test_pending_refund_stays_pending(self):
        data = fixture("transaction_refund.json")
        data["transaction"]["status"] = "PENDING"
        with patch(f"{PROVIDER}._raiffeisen_refund",
                   return_value={"transactionId": "TX-R"}), \
             patch(f"{PROVIDER}._raiffeisen_get_transaction",
                   return_value=data):
            self.refund._send_refund_request()
        self.assertEqual(self.refund.state, "pending")

    def test_refund_amount_mismatch_errors(self):
        data = fixture("transaction_refund.json")
        data["transaction"]["transactionAmount"] = 4000.00
        with patch(f"{PROVIDER}._raiffeisen_refund",
                   return_value={"transactionId": "TX-R"}), \
             patch(f"{PROVIDER}._raiffeisen_get_transaction",
                   return_value=data):
            self.refund._send_refund_request()
        self.assertEqual(self.refund.state, "error")

    def test_missing_purchase_id_is_fetched_before_refusing(self):
        """Payments taken before the id was recorded must stay refundable."""
        self.source.raiffeisen_tx_id = False
        with patch(f"{PROVIDER}._raiffeisen_list_transactions",
                   return_value=[fixture("transactions_single.json")]), \
             patch(f"{PROVIDER}._raiffeisen_refund",
                   return_value={"transactionId": "TX-R"}) as call, \
             patch(f"{PROVIDER}._raiffeisen_get_transaction",
                   return_value=fixture("transaction_refund.json")):
            self.refund._send_refund_request()
        self.assertEqual(
            call.call_args.args[1],
            "R-001-TX-ab2301a6-104c-436e-bf60-ccc0bc9d6ae2",
        )

    def test_unresolvable_purchase_id_raises(self):
        self.source.raiffeisen_tx_id = False
        with patch(f"{PROVIDER}._raiffeisen_list_transactions",
                   return_value=[]):
            with self.assertRaises(ValidationError):
                self.refund._send_refund_request()

    def test_refund_without_a_returned_id_errors(self):
        with patch(f"{PROVIDER}._raiffeisen_refund", return_value={}):
            self.refund._send_refund_request()
        self.assertEqual(self.refund.state, "error")


@tagged("post_install", "-at_install")
class TestRedirectRendering(RaiffeisenCommon):

    def setUp(self):
        super().setUp()
        self.tx = self._create_tx()

    def test_checkout_url_is_split_for_the_get_form(self):
        url = ("https://payment.raiaccept.com/checkout"
               "?paymentSession=D-001-SESS-94c89598")
        with patch(f"{PROVIDER}._raiffeisen_create_order_and_checkout",
                   return_value=url):
            values = self.tx._get_specific_rendering_values({})
        self.assertEqual(
            values["api_url"], "https://payment.raiaccept.com/checkout"
        )
        self.assertEqual(
            values["url_params"], {"paymentSession": "D-001-SESS-94c89598"}
        )

    def test_checkout_snapshots_the_provider_configuration(self):
        self.provider.raiffeisen_currency_rate = 117.5
        with patch(f"{PROVIDER}._raiffeisen_create_order_and_checkout",
                   return_value="https://payment.raiaccept.com/checkout?a=1"):
            self.tx._get_specific_rendering_values({})
        self.assertEqual(self.tx.raiffeisen_gateway_currency, "RSD")
        self.assertEqual(self.tx.raiffeisen_currency_rate, 117.5)
        self.assertEqual(self.tx.raiffeisen_merchant_reference, "S00042-1")

    def test_a_failed_checkout_renders_instead_of_crashing_qweb(self):
        """Returning {} would leave `api_url` undefined and make the
        redirect template raise, so the customer saw a traceback."""
        with patch(f"{PROVIDER}._raiffeisen_create_order_and_checkout",
                   side_effect=ValidationError("gateway said no")):
            values = self.tx._get_specific_rendering_values({})
        self.assertEqual(values["api_url"], "/payment/status")
        self.assertEqual(self.tx.state, "error")
        self.assertIn("gateway said no", self.tx.state_message)
