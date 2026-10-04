# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Message composer CC",
    "summary": "Explicit, per-message carbon copy recipients",
    "version": "19.0.1.0.0",
    "license": "AGPL-3",
    "author": "Ledo Enterprises, Odoo Community Association (OCA)",
    "website": "https://github.com/OCA/mail",
    "depends": ["mail"],
    "data": ["views/composer.xml"],
    "assets": {"web.assets_backend": ["mail_composer_cc/static/src/composer.xml"]},
    "installable": True,
}
