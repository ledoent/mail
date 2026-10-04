# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, tools
from odoo.exceptions import ValidationError


class MailComposeMessage(models.TransientModel):
    _inherit = "mail.compose.message"

    cc_partner_ids = fields.Many2many(
        "res.partner",
        "mail_composer_cc_partner_rel",
        "wizard_id",
        "partner_id",
        string="CC",
        compute="_compute_cc_partner_ids",
        store=True,
        readonly=False,
    )

    @api.depends("template_id", "res_ids", "model", "composition_mode")
    def _compute_cc_partner_ids(self):
        for wizard in self:
            wizard.cc_partner_ids = False
            if (
                wizard.template_id
                and wizard.composition_mode == "comment"
                and not wizard.composition_batch
                and not wizard.template_id.use_default_to
            ):
                rid = (wizard._evaluate_res_ids() or [0])[0]
                rendered = wizard.template_id._render_field("email_cc", [rid])[rid]
                emails = tools.email_split(rendered or "")
                if emails:
                    partners = self.env["res.partner"]._find_or_create_from_emails(
                        emails
                    )
                    wizard.cc_partner_ids = self.env["res.partner"].concat(*partners)

    def _generate_template_for_composer(self, res_ids, render_fields, **kwargs):
        if self.composition_mode == "comment" and not self.composition_batch:
            render_fields = set(render_fields) - {"email_cc"}
        return super()._generate_template_for_composer(res_ids, render_fields, **kwargs)

    def _prepare_mail_values_rendered(self, res_ids):
        values = super()._prepare_mail_values_rendered(res_ids)
        if self.composition_mode != "comment" or self.subtype_is_log:
            return values
        for partner in self.partner_ids | self.cc_partner_ids:
            if not partner.active or not tools.email_normalize(partner.email):
                raise ValidationError(
                    self.env._(
                        "Recipient %s needs an active, valid email address.",
                        partner.display_name,
                    )
                )
        to_emails = set(self.partner_ids.mapped("email_normalized"))
        cc = self.cc_partner_ids.filtered(lambda p: p.email_normalized not in to_emails)
        self.env["mail.thread"]._cc_check_recipients(
            self.partner_ids | cc, self.email_from, self.mail_server_id
        )
        for vals in values.values():
            vals.update(
                {
                    "cc_to_partner_ids": self.partner_ids.ids,
                    "cc_partner_ids": cc.ids,
                    "partner_ids": (self.partner_ids | cc).ids,
                }
            )
        return values
