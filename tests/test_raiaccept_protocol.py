"""Protocol-level tests against the shapes RaiAccept documents.

Every expected value here comes from https://docs.raiaccept.com
(read 2026-08-22) or from a fixture captured verbatim from it. The
module this covers was rewritten because the previous implementation
was internally consistent and wrong about the API: unit tests that
assert our own assumptions cannot catch that, so the assertions below
are pinned to the documentation instead.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from conftest import load_fixture, manifest, raiaccept  # noqa: E402


# ── Endpoints and identity ───────────────────────────────────────────

def test_auth_and_api_hosts_match_the_documentation():
    assert raiaccept.AUTH_BASE == "https://auth.raiaccept.com"
    assert raiaccept.LOGIN_ENDPOINT == "/auth/api/login"
    assert raiaccept.REFRESH_ENDPOINT == "/auth/api/refresh"
    assert raiaccept.API_BASE == "https://trapi.raiaccept.com"


def test_integration_version_matches_the_manifest():
    """The bank sees this version string; a stale constant misreports."""
    assert raiaccept.INTEGRATION_VERSION == manifest()["version"]


def test_integration_context_shape():
    ctx = raiaccept.integration_context("Prodavnica")
    assert ctx == {
        "type": "CODE",
        "data": {
            "name": "Prodavnica",
            "version": raiaccept.INTEGRATION_VERSION,
            "vendor": raiaccept.INTEGRATION_VENDOR,
        },
    }


def test_integration_context_falls_back_and_truncates():
    assert raiaccept.integration_context("")["data"]["name"] == "Odoo"
    assert len(raiaccept.integration_context("x" * 200)["data"]["name"]) == 64


# ── Amounts ──────────────────────────────────────────────────────────

def test_amount_is_sent_in_major_units():
    """RaiAccept amounts are plain decimals — never minor units.

    The documented order payload carries `"amount": 4000.00` for a
    4000 RSD order, and `transactionAmount: 2000.00` for a 2000 RSD
    refund. An implementation that multiplies by 100 for EUR charges
    a hundred times the order total.
    """
    assert raiaccept.gateway_amount(4000) == 4000.00
    assert raiaccept.gateway_amount(34.90) == 34.90
    assert raiaccept.gateway_amount(825.50) == 825.50


def test_amount_applies_the_configured_rate():
    assert raiaccept.gateway_amount(100, 117.5) == 11750.00


def test_amount_rounds_to_two_decimals():
    assert raiaccept.gateway_amount(10.005) == 10.01
    assert raiaccept.gateway_amount(1 / 3) == 0.33


def test_amount_treats_a_zero_rate_as_one():
    assert raiaccept.gateway_amount(50, 0) == 50.00


# ── merchantOrderReference ───────────────────────────────────────────

def test_reference_strips_characters_the_api_rejects():
    """Only alphanumerics, hyphens and underscores are accepted."""
    assert raiaccept.sanitize_merchant_reference(
        "INV/2026/00001"
    ) == "INV-2026-00001"
    assert raiaccept.sanitize_merchant_reference("S00042-1") == "S00042-1"
    assert raiaccept.sanitize_merchant_reference("a b#c") == "a-b-c"


def test_reference_is_capped_at_the_documented_length():
    assert len(raiaccept.sanitize_merchant_reference("x" * 400)) == 150


def test_reference_falls_back_when_nothing_survives():
    assert raiaccept.sanitize_merchant_reference("", "ODOO-7") == "ODOO-7"
    assert raiaccept.sanitize_merchant_reference(None, "ODOO-7") == "ODOO-7"


# ── Consumer fields ──────────────────────────────────────────────────

def test_phone_accepts_both_documented_formats():
    assert raiaccept.format_phone("+381 11 320 21 00") == "+381113202100"
    assert raiaccept.format_phone("00381113202100") == "00381113202100"


def test_phone_is_dropped_when_it_cannot_fit():
    """15 characters is the documented ceiling; truncating would lie."""
    assert raiaccept.format_phone("+3811132021001234567") == ""
    assert raiaccept.format_phone("") == ""
    assert raiaccept.format_phone("+") == ""


def test_transliteration_of_serbian_cyrillic():
    assert raiaccept.transliterate("Милица Петровић") == "Milica Petrovic"
    assert raiaccept.transliterate("Ђорђе Њгош") == "Djordje Njgos"


def test_transliteration_respects_the_field_limit():
    assert len(raiaccept.transliterate("a" * 100, 32)) == 32


def test_country_table_maps_to_alpha3():
    assert raiaccept.COUNTRY_ISO3["RS"] == "SRB"
    assert all(len(v) == 3 for v in raiaccept.COUNTRY_ISO3.values())


# ── Status tables ────────────────────────────────────────────────────

def test_order_status_table_covers_exactly_the_documented_set():
    """Documented under "Retrieve order details"."""
    assert set(raiaccept.ORDER_STATUS_MAP) == {
        "DRAFT", "CHECKOUT", "PAID", "PARTIALLY_REFUNDED",
        "FULLY_REFUNDED", "FAILED", "CANCELED", "ABANDONED",
    }


def test_transaction_status_table_covers_exactly_the_documented_set():
    """Documented under "Retrieve transaction status"."""
    assert set(raiaccept.TRANSACTION_STATUS_MAP) == {
        "DRAFT", "PENDING", "SUCCESS", "FAILED",
    }


def test_a_refunded_order_still_counts_as_paid():
    """The refund is a separate child transaction in Odoo."""
    assert raiaccept.ORDER_STATUS_MAP["PARTIALLY_REFUNDED"] == "done"
    assert raiaccept.ORDER_STATUS_MAP["FULLY_REFUNDED"] == "done"


def test_checkout_is_not_a_final_state():
    assert raiaccept.ORDER_STATUS_MAP["CHECKOUT"] == "pending"


# ── Webhook ──────────────────────────────────────────────────────────

def test_webhook_is_read_from_the_levels_the_docs_use():
    """Status lives on `transaction`, the reference under `order.invoice`.

    Reading `order.status` and `order.merchantOrderReference` — as an
    earlier version did — yields an empty status and no reference, and
    fails silently.
    """
    data = raiaccept.normalize_webhook(
        load_fixture("webhook_purchase_success.json")
    )
    assert data["transactionStatus"] == "SUCCESS"
    assert data["merchantOrderReference"] == "12345678940"
    assert data["transactionId"] == \
        "R-001-TX-ab2301a6-104c-436e-bf60-ccc0bc9d6ae2"
    assert data["orderIdentification"] == \
        "R-001-ORD-d68b0e14-66c0-4a89-940a-9438cf5e2e57"
    assert data["transactionType"] == "PURCHASE"
    assert data["statusCode"] == raiaccept.SUCCESS_STATUS_CODE


def test_webhook_survives_junk():
    for junk in (None, [], "string", {}, {"order": "x", "transaction": 3}):
        data = raiaccept.normalize_webhook(junk)
        assert set(data) == {
            "orderIdentification", "merchantOrderReference",
            "transactionId", "transactionType", "transactionStatus",
            "statusCode",
        }
        assert all(v is None for v in data.values())


# ── Transactions ─────────────────────────────────────────────────────

def test_transaction_list_accepts_a_bare_object():
    """The docs' example response is a single object, not an array."""
    txs = raiaccept.normalize_transaction_list(
        load_fixture("transactions_single.json")
    )
    assert len(txs) == 1
    assert txs[0]["transactionType"] == "PURCHASE"


def test_transaction_list_accepts_arrays_and_wrappers():
    one = {"transactionId": "a", "transactionType": "PURCHASE"}
    assert raiaccept.normalize_transaction_list([one]) == [one]
    assert raiaccept.normalize_transaction_list({"transactions": [one]}) == [one]
    assert raiaccept.normalize_transaction_list({"content": [one]}) == [one]


def test_transaction_list_returns_empty_on_nonsense():
    assert raiaccept.normalize_transaction_list(None) == []
    assert raiaccept.normalize_transaction_list({"nothing": 1}) == []
    assert raiaccept.normalize_transaction_list(["x", 1]) == []


def test_purchase_selection_prefers_a_successful_purchase():
    failed = {"transactionId": "1", "transactionType": "PURCHASE",
              "status": "FAILED"}
    ok = {"transactionId": "2", "transactionType": "PURCHASE",
          "status": "SUCCESS"}
    refund = {"transactionId": "3", "transactionType": "REFUND",
              "status": "SUCCESS"}
    assert raiaccept.select_purchase_transaction([failed, ok, refund]) is ok
    assert raiaccept.select_purchase_transaction([refund, failed]) is failed
    assert raiaccept.select_purchase_transaction([refund]) is None
    assert raiaccept.select_purchase_transaction([]) is None


def test_unwrap_transaction_handles_both_shapes():
    wrapped = load_fixture("transaction_refund.json")
    assert raiaccept.unwrap_transaction(wrapped)["transactionType"] == "REFUND"
    bare = load_fixture("transactions_single.json")
    assert raiaccept.unwrap_transaction(bare)["transactionId"] == \
        bare["transactionId"]
    assert raiaccept.unwrap_transaction(None) == {}


def test_documented_refund_amount_is_decimal():
    """Guards the unit question from the other direction."""
    tx = raiaccept.unwrap_transaction(load_fixture("transaction_refund.json"))
    assert tx["transactionAmount"] == 2000.00
    assert raiaccept.gateway_amount(2000) == tx["transactionAmount"]
