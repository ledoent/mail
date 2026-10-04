# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Invoice message CC",
    "summary": "Per-send CC recipients in the invoice sending wizard",
    "version": "19.0.1.0.0",
    "license": "AGPL-3",
    "author": "Ledo Enterprises, Odoo Community Association (OCA)",
    "website": "https://github.com/OCA/mail",
    "depends": ["account", "mail_composer_cc"],
    "data": ["views/wizard.xml"],
    "installable": True,
}
