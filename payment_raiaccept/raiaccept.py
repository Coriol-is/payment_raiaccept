"""Pure RaiAccept protocol helpers — no Odoo imports.

Everything in this module is a function of its arguments: endpoint
constants, the code tables, and the small transformations that sit
between Odoo's data and the shapes RaiAccept documents. Keeping them
free of the ORM means they can be tested with plain pytest, without a
database, which is where the module's earlier divergence from the API
would have been caught.

Reference: https://docs.raiaccept.com/code-integration.html (read
2026-08-22).
"""

import re
import unicodedata

# ── Endpoints ────────────────────────────────────────────────────────
# One API host serves both environments; which one a call lands in is
# decided by the credentials, and every response echoes it back as
# `isProduction`.
AUTH_BASE = "https://auth.raiaccept.com"
LOGIN_ENDPOINT = "/auth/api/login"
REFRESH_ENDPOINT = "/auth/api/refresh"
API_BASE = "https://trapi.raiaccept.com"

# ── Integration identity ─────────────────────────────────────────────
# `integrationContext` identifies the integration to RaiAccept on every
# authentication call; the bank tracks integrations by vendor. These are
# module constants, not settings — a merchant must not be able to claim
# someone else's integration. INTEGRATION_VERSION is asserted against
# the manifest version by the test suite.
INTEGRATION_VENDOR = "Coriolis Lab"
INTEGRATION_VERSION = "19.0.2.1.0"

# Refresh the access token this many seconds before it actually
# expires, so a request never starts with a token that dies in flight.
TOKEN_EXPIRY_MARGIN = 30

# ── Status tables ────────────────────────────────────────────────────
# Both sets are closed and documented. An order that has been refunded
# still describes a payment that succeeded: in Odoo the refund lives on
# its own child transaction, so the parent stays done.
ORDER_STATUS_MAP = {
    "DRAFT": "pending",
    "CHECKOUT": "pending",
    "PAID": "done",
    "PARTIALLY_REFUNDED": "done",
    "FULLY_REFUNDED": "done",
    "FAILED": "error",
    "CANCELED": "cancel",
    "ABANDONED": "cancel",
}

TRANSACTION_STATUS_MAP = {
    "DRAFT": "pending",
    "PENDING": "pending",
    "SUCCESS": "done",
    "FAILED": "error",
}

# Documented transaction response code for success. Everything else is
# an error code listed under "Transaction responses and error codes".
SUCCESS_STATUS_CODE = "0000"

# ── Payment method preference ────────────────────────────────────────
# `paymentMethodPreference` is a required ENUM of the create-order and
# checkout requests: CARD, GOOGLE_PAY or APPLE_PAY, exactly one value.
# With CARD, wallets that are enabled on the merchant account still
# appear on the hosted page alongside cards; the wallet values make the
# hosted page open directly in that wallet. Wallets do not exist in the
# Sandbox environment.
PAYMENT_METHOD_PREFERENCES = {
    "apple_pay": "APPLE_PAY",
    "google_pay": "GOOGLE_PAY",
}


def payment_method_preference(method_code):
    """Map an Odoo payment.method code to a gateway preference."""
    return PAYMENT_METHOD_PREFERENCES.get(method_code or "", "CARD")

# ── Field constraints ────────────────────────────────────────────────
# RaiAccept accepts only these characters in merchantOrderReference,
# 1-150 of them. Odoo references built from an invoice contain slashes
# (INV/2026/00001), which the API rejects with HTTP 400.
_REFERENCE_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")
MERCHANT_REFERENCE_MAX = 150
PHONE_MAX = 15

# ISO 3166-1 alpha-2 → alpha-3
COUNTRY_ISO3 = {
    # SEE / CEE (primary market)
    "AL": "ALB", "AT": "AUT", "BA": "BIH", "BG": "BGR", "CH": "CHE",
    "CZ": "CZE", "DE": "DEU", "GR": "GRC", "HR": "HRV", "HU": "HUN",
    "ME": "MNE", "MK": "MKD", "PL": "POL", "RO": "ROU", "RS": "SRB",
    "SI": "SVN", "SK": "SVK", "XK": "XKX",
    # Western Europe
    "BE": "BEL", "DK": "DNK", "ES": "ESP", "FI": "FIN", "FR": "FRA",
    "GB": "GBR", "IE": "IRL", "IT": "ITA", "LU": "LUX", "NL": "NLD",
    "NO": "NOR", "PT": "PRT", "SE": "SWE",
    # Eastern Europe / CIS
    "BY": "BLR", "EE": "EST", "LT": "LTU", "LV": "LVA", "MD": "MDA",
    "RU": "RUS", "UA": "UKR",
    # Middle East / Asia / Americas (common)
    "AE": "ARE", "AU": "AUS", "BR": "BRA", "CA": "CAN", "CN": "CHN",
    "IL": "ISR", "IN": "IND", "JP": "JPN", "KR": "KOR", "MX": "MEX",
    "NZ": "NZL", "SA": "SAU", "SG": "SGP", "TR": "TUR", "US": "USA",
    "ZA": "ZAF",
}

_CYRILLIC = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D",
    "Ђ": "Dj", "Е": "E", "Ж": "Z", "З": "Z", "И": "I",
    "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj", "М": "M",
    "Н": "N", "Њ": "Nj", "О": "O", "П": "P", "Р": "R",
    "С": "S", "Т": "T", "Ћ": "C", "У": "U", "Ф": "F",
    "Х": "H", "Ц": "C", "Ч": "C", "Џ": "Dz", "Ш": "S",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "ђ": "dj", "е": "e", "ж": "z", "з": "z", "и": "i",
    "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m",
    "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "ћ": "c", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "c", "џ": "dz", "ш": "s",
}


def transliterate(text, max_len=127):
    """Transliterate non-Latin characters and limit length."""
    if not text:
        return ""
    result = []
    for char in text:
        if char in _CYRILLIC:
            result.append(_CYRILLIC[char])
        else:
            nfkd = unicodedata.normalize("NFKD", char)
            ascii_char = nfkd.encode("ascii", "ignore").decode("ascii")
            result.append(ascii_char if ascii_char else "")
    return "".join(result)[:max_len]


def format_phone(phone):
    """Return a phone number in a format RaiAccept accepts, or "".

    The API takes `+381...` or `00381...`, at most 15 characters and
    without spaces. Everything else is stripped; a number that still
    does not fit is dropped rather than truncated, because a truncated
    phone number is worse than none.
    """
    if not phone:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone)
    if cleaned.startswith("+"):
        cleaned = "+" + cleaned[1:].replace("+", "")
    else:
        cleaned = cleaned.replace("+", "")
    if not cleaned.strip("+") or len(cleaned) > PHONE_MAX:
        return ""
    return cleaned


def sanitize_merchant_reference(reference, fallback=""):
    """Return an API-safe merchantOrderReference."""
    sanitized = _REFERENCE_DISALLOWED.sub(
        "-", reference or ""
    )[:MERCHANT_REFERENCE_MAX]
    return sanitized or fallback


def gateway_amount(amount, rate=1.0):
    """Convert a store amount into the amount RaiAccept expects.

    RaiAccept amounts are ordinary decimal numbers — the docs show
    `"amount": 4000.00` and `"transactionAmount": 2000.00`, and no
    endpoint documents minor units for any currency. Multiplying by 100
    (as an earlier version of this module did for EUR) charges the
    customer a hundred times the order total.
    """
    return round(float(amount) * float(rate or 1.0), 2)


def integration_context(store_name):
    """Build the `integrationContext` object required by auth calls."""
    return {
        "type": "CODE",
        "data": {
            "name": (store_name or "Odoo")[:64],
            "version": INTEGRATION_VERSION,
            "vendor": INTEGRATION_VENDOR,
        },
    }


def normalize_webhook(payload):
    """Flatten a RaiAccept webhook body into flat payment data.

    The notification nests the parts that matter in different places
    than an earlier version of this module assumed: the status belongs
    to `transaction`, not to `order`, and the merchant reference sits
    under `order.invoice`. Reading them from the wrong level yields an
    empty status and no reference, and does so silently — the
    transaction simply never resolves.
    """
    payload = payload if isinstance(payload, dict) else {}
    tx_data = payload.get("transaction")
    tx_data = tx_data if isinstance(tx_data, dict) else {}
    order_data = payload.get("order")
    order_data = order_data if isinstance(order_data, dict) else {}
    invoice = order_data.get("invoice")
    invoice = invoice if isinstance(invoice, dict) else {}
    return {
        "orderIdentification": order_data.get("orderIdentification"),
        "merchantOrderReference": invoice.get("merchantOrderReference"),
        "transactionId": tx_data.get("transactionId"),
        "transactionType": tx_data.get("transactionType"),
        "transactionStatus": tx_data.get("status"),
        "statusCode": tx_data.get("statusCode"),
    }


def normalize_transaction_list(data):
    """Coerce a transactions response into a list of dicts.

    The docs label the endpoint "Retrieve all transactions" but document
    it as a POST whose example response is a single transaction object
    rather than an array. Until that is settled against the live API,
    accept every plausible shape.
    """
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    if isinstance(data, dict):
        for key in ("transactions", "content", "items"):
            nested = data.get(key)
            if isinstance(nested, list):
                return [t for t in nested if isinstance(t, dict)]
        if data.get("transactionId"):
            return [data]
    return []


def select_purchase_transaction(transactions):
    """Return the PURCHASE transaction that represents the payment."""
    transactions = [t for t in transactions if isinstance(t, dict)]
    return next(
        (t for t in transactions
         if t.get("transactionType") == "PURCHASE"
         and t.get("status") == "SUCCESS"),
        None,
    ) or next(
        (t for t in transactions
         if t.get("transactionType") == "PURCHASE"),
        None,
    )


def unwrap_transaction(data):
    """Return the transaction object from a single-transaction reply.

    The webhook and the get-transaction endpoint wrap it under
    `transaction`; some replies are the bare object.
    """
    if not isinstance(data, dict):
        return {}
    nested = data.get("transaction")
    if isinstance(nested, dict):
        return nested
    return data
