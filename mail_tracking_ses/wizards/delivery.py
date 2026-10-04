# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class SesDeliveryWizard(models.TransientModel):
    _name = "mail.ses.delivery.wizard"
    _description = "Message delivery details"

    line_ids = fields.One2many("mail.ses.delivery.line", "wizard_id", readonly=True)


class SesDeliveryLine(models.TransientModel):
    _name = "mail.ses.delivery.line"
    _description = "Recipient delivery detail"

    wizard_id = fields.Many2one(
        "mail.ses.delivery.wizard", required=True, ondelete="cascade"
    )
    recipient = fields.Char(readonly=True)
    channel = fields.Char(readonly=True)
    delivery = fields.Char(readonly=True)
    opened_at = fields.Datetime(string="Open observed", readonly=True)
    clicked_at = fields.Datetime(string="Click observed", readonly=True)
    attention = fields.Char(readonly=True)
