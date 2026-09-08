#!/usr/bin/env python3
"""Phase 2 of the Sandbox smoke: after the manual test-card payment.

Walks the post-payment half of the client: order status (expect PAID),
transaction listing (settles the GET-vs-POST question from the docs),
single-transaction read, partial refund, and the resulting statuses.

Usage: python3 tools/sandbox_smoke2.py <orderIdentification>
"""
import json
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "payment_raiffeisen"))
from raiaccept import (  # noqa: E402
    API_BASE,
    AUTH_BASE,
    LOGIN_ENDPOINT,
    integration_context,
    normalize_transaction_list,
    select_purchase_transaction,
    unwrap_transaction,
)
from sandbox_smoke import load_env, step  # noqa: E402


def main():
    order_id = sys.argv[1] if len(sys.argv) > 1 else sys.exit(
        "usage: sandbox_smoke2.py <orderIdentification>")
    username, password = load_env()

    resp = requests.post(
        AUTH_BASE + LOGIN_ENDPOINT,
        json={
            "username": username,
            "password": password,
            "integrationContext": integration_context("Sandbox smoke"),
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    headers = {
        "Authorization": f"Bearer {resp.json()['accessToken']}",
        "Content-Type": "application/json",
    }

    # 1. order status after payment
    resp = requests.get(API_BASE + f"/orders/{order_id}",
                        headers=headers, timeout=30)
    step("order status", resp.status_code == 200,
         f"HTTP {resp.status_code}")
    order = resp.json()
    status = order.get("status")
    step("order is PAID", status == "PAID", f"status={status}")

    # 2. list transactions — try GET first, like the module does
    endpoint = API_BASE + f"/orders/{order_id}/transactions"
    resp = requests.get(endpoint, headers=headers, timeout=30)
    method = "GET"
    if resp.status_code in (404, 405):
        resp = requests.post(endpoint, json={}, headers=headers, timeout=30)
        method = "POST"
    step("list transactions", resp.status_code == 200,
         f"{method} HTTP {resp.status_code}: "
         + ("" if resp.status_code == 200 else resp.text[:300]))
    raw = resp.json()
    print("Raw transactions response shape:",
          type(raw).__name__,
          list(raw.keys()) if isinstance(raw, dict) else f"len={len(raw)}")
    txs = normalize_transaction_list(raw)
    step("normalize_transaction_list", bool(txs), f"{len(txs)} transaction(s)")
    purchase = select_purchase_transaction(txs)
    step("PURCHASE found", bool(purchase),
         f"id={purchase and purchase.get('transactionId')} "
         f"status={purchase and purchase.get('status')} "
         f"statusCode={purchase and purchase.get('statusCode')}")
    tx_id = purchase["transactionId"]

    # 3. single transaction read
    resp = requests.get(
        API_BASE + f"/orders/{order_id}/transactions/{tx_id}",
        headers=headers, timeout=30)
    step("get transaction", resp.status_code == 200,
         f"HTTP {resp.status_code}")
    tx = unwrap_transaction(resp.json())
    print("Purchase transaction:",
          json.dumps(tx, indent=2)[:1200])

    # 4. partial refund: 40.00 of 100.00 RSD
    resp = requests.post(
        API_BASE + f"/orders/{order_id}/transactions/{tx_id}/refund",
        json={"amount": 40.00, "currency": "RSD"},
        headers=headers, timeout=30)
    step("partial refund accepted", resp.status_code < 400,
         f"HTTP {resp.status_code}: {resp.text[:300]}")
    refund_resp = resp.json() if resp.content else {}
    print("Refund response:", json.dumps(refund_resp, indent=2)[:600])

    # 5. order status after refund
    resp = requests.get(API_BASE + f"/orders/{order_id}",
                        headers=headers, timeout=30)
    status = resp.json().get("status")
    step("order after refund", resp.status_code == 200, f"status={status}")

    # 6. transactions after refund
    resp = requests.get(endpoint, headers=headers, timeout=30)
    if resp.status_code in (404, 405):
        resp = requests.post(endpoint, json={}, headers=headers, timeout=30)
    txs = normalize_transaction_list(resp.json())
    print("Transactions after refund:")
    for t in txs:
        print(f"  {t.get('transactionType'):10} {t.get('status'):8} "
              f"code={t.get('statusCode')} amount={t.get('transactionAmount')}"
              f" id={t.get('transactionId')}")


if __name__ == "__main__":
    main()
