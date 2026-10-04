# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Amazon SES mail tracking",
    "summary": "SES delivery, engagement, complaint review and safe SMTP retries",
    "version": "19.0.1.0.0",
    "license": "AGPL-3",
    "author": "Ledo Enterprises, Odoo Community Association (OCA)",
    "website": "https://github.com/OCA/mail",
    "depends": ["mail_tracking", "mail_composer_cc"],
    "external_dependencies": {"python": ["boto3"]},
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/ses.xml",
        "data/cron.xml",
    ],
    "assets": {
        "web.assets_backend": ["mail_tracking_ses/static/src/message_actions.esm.js"]
    },
    "installable": True,
}
