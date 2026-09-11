{
    "name": "Payment Provider: Raiffeisen RaiAccept",
    "version": "19.0.2.1.0",
    "category": "Accounting/Payment Providers",
    "sequence": 370,
    "summary": "Accept online card payments via Raiffeisen Bank RaiAccept gateway (Serbia & SEE).",
    "description": " ",  # Non-empty to avoid loading README as description.
    "author": "Coriolis Lab, Adria Mart",
    "website": "https://coriol.co/odoo/",
    "support": "odoo@coriol.co",
    "license": "LGPL-3",
    "price": 0,
    "currency": "EUR",
    # First entry is the store cover: the Apps grid renders it in a
    # strict 2:1 box with background-size: cover.
    "images": [
        "static/description/cover.png",
        "static/description/banner.png",
    ],
    "depends": [
        "payment",
    ],
    "data": [
        "views/redirect_form_templates.xml",
        "views/payment_provider_views.xml",
        "data/payment_provider_data.xml",
    ],
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "application": False,
    "installable": True,
    "auto_install": False,
}
