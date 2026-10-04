# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class AccountMoveSendWizard(models.TransientModel):
    _inherit = "account.move.send.wizard"

    cc_partner_ids = fields.Many2many(
        "res.partner",
        "account_send_cc_partner_rel",
        "wizard_id",
        "partner_id",
        string="CC",
        compute="_compute_cc_partner_ids",
        store=True,
        readonly=False,
    )

    @api.depends("template_id", "lang")
    def _compute_cc_partner_ids(self):
        for wizard in self:
            wizard.cc_partner_ids = wizard._cc_partners(
                wizard.move_id, wizard.template_id, wizard.lang
            )

    def _get_sending_settings(self):
        values = super()._get_sending_settings()
        values["cc_partner_ids"] = self.cc_partner_ids.ids
        return values
