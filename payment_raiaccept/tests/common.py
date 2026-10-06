"""Shared setup for the Odoo-side RaiAccept tests."""

from odoo.tests import TransactionCase


class RaiffeisenCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["payment.provider"].create({
            "name": "Raiffeisen Test",
            "code": "raiffeisen",
            "is_live": False,
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
        cls.card_method = cls.env.ref("payment_raiaccept.payment_method_card")

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
        # Odoo 20 refuses direct writes on transactions unless the caller
        # vouches for them; core's own tests set the same flag.
        return self.env["payment.transaction"].with_context(
            payment_safe_write=True
        ).create(vals)

    def _process_recorded(self, tx):
        """Run the processing the payment cron would run for ``tx``.

        Odoo 20 queues provider data as payment.data and processes it
        asynchronously; tests apply it synchronously here.
        """
        for payment_data in tx.payment_data_ids:
            tx.with_context(payment_safe_write=True)._process(
                payment_data.payload
            )
            payment_data.unlink()
