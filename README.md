# Odoo 19 Payment Provider: Raiffeisen RaiAccept

[![License: LGPL-3](https://img.shields.io/badge/License-LGPL--3-blue.svg)](https://www.gnu.org/licenses/lgpl-3.0)
[![Odoo Version](https://img.shields.io/badge/Odoo-19.0-875A7B)](https://www.odoo.com/documentation/19.0/)

Open-source Odoo 19 payment provider module for **Raiffeisen Bank RaiAccept** gateway. Accepts Visa, Mastercard, and DinaCard payments through Raiffeisen's hosted checkout in Serbia and the wider SEE/CEE region.

> **This is a maintained fork.** The original module was built by
> [Adria Mart](https://adriamart.rs) ([AdriaMartHQ/payment_raiffeisen](https://github.com/AdriaMartHQ/payment_raiffeisen)).
> This fork is maintained by **[Coriolis Lab](https://coriol.co/)**: the client has been
> realigned with the published RaiAccept API (authentication with `integrationContext`,
> decimal amounts, webhook payload shapes, refund flow) and validated end-to-end against
> the live RaiAccept Sandbox — login/refresh, order, hosted checkout, test-card payment,
> status queries, and partial refunds (2026-09-08).

## Install

- **From source**: clone this repo and copy `payment_raiffeisen/` into your Odoo addons path.

> The `payment_raiffeisen` listing currently on the Odoo Apps Store is the
> original Adria Mart build, which predates the API realignment in this fork.
> Until this fork is published there, install from source.

## Highlights

- Full RaiAccept API integration (order creation, hosted checkout redirect, status query)
- Settlement in **RSD** or **EUR**, with built-in currency conversion
- Partial and full refunds from the Odoo backend
- Production + sandbox credentials separated
- Serbian Cyrillic-to-Latin transliteration for addresses
- Webhook processing with authoritative gateway-side amount / currency verification
- Billing address mapping for 50+ countries

Supported markets: Serbia, Austria, Croatia, Bosnia & Herzegovina, Kosovo, Albania, Romania, Hungary, Czech Republic, Slovakia, Ukraine (all Raiffeisen business accounts).

## Repository layout

```
payment_raiffeisen/        The actual Odoo module (copy this into your addons path)
  ├── README.md            Detailed docs, changelog, supported cards/countries
  ├── __manifest__.py      Module manifest
  ├── controllers/         Webhook + redirect handlers
  ├── models/              payment.provider / payment.transaction overrides
  ├── views/               Provider config + redirect form
  ├── data/                Provider data record
  └── static/description/  Apps Store assets
```

Full documentation is in [`payment_raiffeisen/README.md`](payment_raiffeisen/README.md).

## Documentation

- RaiAccept API reference, test cards, onboarding: [docs.raiaccept.com](https://docs.raiaccept.com/index.html)
- Module README: [`payment_raiffeisen/README.md`](payment_raiffeisen/README.md)

## License

LGPL-3 — see [`payment_raiffeisen/LICENSE`](payment_raiffeisen/LICENSE).

## Maintainer

This fork is maintained by **Coriolis Lab** — [coriol.co](https://coriol.co/) — publisher of
Odoo apps for the Serbian market. Pull requests and issues welcome.

- GitHub Issues: [Coriol-is/payment_raiffeisen/issues](https://github.com/Coriol-is/payment_raiffeisen/issues)

### Original module

Originally built by **Adria Mart** (sole proprietorship, Belgrade, Serbia) — [adriamart.rs](https://adriamart.rs) — during their pre-launch technical preparation.

- Blog: [Open-Source Odoo 19 Payment Provider: Raiffeisen RaiAccept](https://adriamart.rs/blog/resources-3/8)
- Email: balkan@adriamart.rs
- Upstream: [AdriaMartHQ/payment_raiffeisen](https://github.com/AdriaMartHQ/payment_raiffeisen)
