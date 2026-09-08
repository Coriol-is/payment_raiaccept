"""Shared setup for the Odoo-side RaiAccept tests."""

from odoo.tests import TransactionCase


class RaiffeisenCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["payment.provider"].create({
            "name": "Raiffeisen Test",
            "code": "raiffeisen",
            "state": "test",
            "raiffeisen_sandbox_username": "sandbox-user",
            "raiffeisen_sandbox_password": "sandbox-pass",
            "raiffeisen_gateway_currency": "RSD",
            "raiffeisen_currency_rate": 1.0,
        })
        cls.partner = cls.env["res.partner"].create({
            "name": "Милица Петровић",
            "street": "Ресавска 1",
            "city": "Београд",
            "zip": "11000",
            "country_id": cls.env.ref("base.rs").id,
            "email": "milica@petrovic.rs",
            "phone": "+381 11 320 21 00",
        })
        cls.currency = cls.env.ref("base.EUR")
        # A fresh database activates only the company currency.
        cls.currency.active = True
        cls.card_method = cls.env.ref("payment.payment_method_card")

    def _create_tx(self, reference="S00042-1", amount=4000.0, **values):
        vals = {
            "provider_id": self.provider.id,
            "payment_method_id": self.card_method.id,
            "reference": reference,
            "amount": amount,
            "currency_id": self.currency.id,
            "partner_id": self.partner.id,
            "operation": "online_redirect",
        }
        vals.update(values)
        return self.env["payment.transaction"].create(vals)
