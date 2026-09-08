#!/usr/bin/env python3
"""Live smoke test against the RaiAccept Sandbox.

Reads credentials from the repo-root .env (gitignored) and walks the
same call sequence the Odoo module makes, using the protocol constants
from payment_raiaccept.raiaccept:

  1. POST /auth/api/login        with integrationContext
  2. POST /auth/api/refresh      with the refresh token
  3. POST /orders                CreateOrderEntryRequest
  4. POST /orders/{id}/checkout  -> paymentRedirectURL
  5. GET  /orders/{id}           -> order status

It stops before the actual card payment: that happens on the hosted
payment page (open the printed paymentRedirectURL in a browser and pay
with a test card, e.g. 4999 9999 9999 0011). Never prints credentials
or tokens.
"""
import json
import sys
import uuid
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "payment_raiaccept"))
from raiaccept import (  # noqa: E402
    API_BASE,
    AUTH_BASE,
    LOGIN_ENDPOINT,
    REFRESH_ENDPOINT,
    integration_context,
)


def load_env():
    creds = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            creds[key.strip()] = value.strip()
    username = creds.get("RAIACCEPT_SANDBOX_USERNAME")
    password = creds.get("RAIACCEPT_SANDBOX_PASSWORD")
    if not username or not password:
        sys.exit("FAIL: RAIACCEPT_SANDBOX_USERNAME/PASSWORD not set in .env")
    return username, password


def step(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def main():
    username, password = load_env()

    # 1. login
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
    if resp.status_code != 200:
        step("login", False, f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    access = data.get("accessToken")
    refresh = data.get("refreshToken")
    step("login", bool(access),
         f"isProduction={data.get('isProduction')} "
         f"accessTokenExpiresIn={data.get('accessTokenExpiresIn')} "
         f"refreshToken={'yes' if refresh else 'no'}")

    # 2. refresh
    if refresh:
        resp = requests.post(
            AUTH_BASE + REFRESH_ENDPOINT,
            json={
                "refreshToken": refresh,
                "integrationContext": integration_context("Sandbox smoke"),
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        got = resp.status_code == 200 and resp.json().get("accessToken")
        step("refresh", bool(got), f"HTTP {resp.status_code}"
             + ("" if got else f": {resp.text[:300]}"))

    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
    }
    ref = "SMOKE-" + uuid.uuid4().hex[:12]
    payload = {
        "billingAddress": {
            "firstName": "Anton",
            "lastName": "Seledets",
            "addressStreet1": "Lomina 51",
            "city": "Beograd",
            "postalCode": "11000",
            "country": "SRB",
        },
        "shippingAddress": {
            "firstName": "Anton",
            "lastName": "Seledets",
            "addressStreet1": "Lomina 51",
            "city": "Beograd",
            "postalCode": "11000",
            "country": "SRB",
        },
        "consumer": {
            "firstName": "Anton",
            "lastName": "Seledets",
            "email": "anton.seledets@gmail.com",
        },
        "invoice": {
            "amount": 100.00,
            "currency": "RSD",
            "description": "Sandbox smoke order",
            "merchantOrderReference": ref,
            "items": [{
                "description": "Sandbox smoke order",
                "numberOfItems": 1,
                "price": 100.00,
            }],
        },
        "urls": {
            "successUrl": "https://coriol.co/smoke/return",
            "failUrl": "https://coriol.co/smoke/return",
            "cancelUrl": "https://coriol.co/smoke/return",
            "notificationUrl": "https://coriol.co/smoke/webhook",
        },
        "paymentMethodPreference": "CARD",
    }

    # 3. create order
    resp = requests.post(API_BASE + "/orders", json=payload,
                         headers=headers, timeout=30)
    if resp.status_code >= 400:
        step("create order", False,
             f"HTTP {resp.status_code}: {resp.text[:500]}")
    order = resp.json()
    order_id = order.get("orderIdentification")
    step("create order", bool(order_id),
         f"orderIdentification={order_id} ref={ref}")

    # 4. checkout
    resp = requests.post(API_BASE + f"/orders/{order_id}/checkout",
                         json=payload, headers=headers, timeout=30)
    if resp.status_code >= 400:
        step("checkout", False,
             f"HTTP {resp.status_code}: {resp.text[:500]}")
    redirect = resp.json().get("paymentRedirectURL")
    step("checkout", bool(redirect))

    # 5. order status
    resp = requests.get(API_BASE + f"/orders/{order_id}",
                        headers=headers, timeout=30)
    if resp.status_code >= 400:
        step("order status", False,
             f"HTTP {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    status = body.get("status") or body.get("order", {}).get("status")
    step("order status", True, f"status={status}")
    print()
    print("Full order response:")
    print(json.dumps(body, indent=2)[:2000])
    print()
    print("Pay manually with a test card (4999 9999 9999 0011) at:")
    print(redirect)


if __name__ == "__main__":
    main()
