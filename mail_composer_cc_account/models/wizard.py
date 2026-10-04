# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models, tools
from odoo.exceptions import ValidationError


class AccountMoveSend(models.AbstractModel):
    _inherit = "account.move.send"

    def _cc_partners(self, move, template, lang):
        if not template or template.use_default_to:
            return self.env["res.partner"]
        emails = self._get_mail_default_field_value_from_template(
            template, lang, move, "email_cc"
        )
        return move._partner_find_from_emails_single(
            tools.email_split(emails or ""), no_create=False
        )

    def _get_default_mail_partner_ids(self, move, mail_template, mail_lang):
        partners = super()._get_default_mail_partner_ids(move, mail_template, mail_lang)
        cc = self._cc_partners(move, mail_template, mail_lang)
        # Preserve explicit To even if the template also lists it as CC.
        to = (
            self._get_mail_default_field_value_from_template(
                mail_template, mail_lang, move, "email_to"
            )
            if mail_template
            else ""
        )
        to_emails = set(tools.email_normalize_all(to or ""))
        if mail_template and not mail_template.use_default_to:
            partner_to = self._get_mail_default_field_value_from_template(
                mail_template, mail_lang, move, "partner_to"
            )
            explicit_to = (
                self.env["res.partner"]
                .browse(mail_template._parse_partner_to(partner_to or ""))
                .exists()
            )
            to_emails.update(explicit_to.mapped("email_normalized"))
        return partners - cc.filtered(lambda p: p.email_normalized not in to_emails)

    def _get_default_sending_settings(self, move, from_cron=False, **custom_settings):
        values = super()._get_default_sending_settings(
            move, from_cron=from_cron, **custom_settings
        )
        values["cc_partner_ids"] = custom_settings.get(
            "cc_partner_ids",
            self._cc_partners(
                move, values.get("mail_template"), values.get("mail_lang")
            ).ids,
        )
        return values

    def _get_mail_params(self, move, move_data):
        values = super()._get_mail_params(move, move_data)
        to = self.env["res.partner"].browse(values["partner_ids"])
        cc = self.env["res.partner"].browse(move_data.get("cc_partner_ids", []))
        for partner in to | cc:
            if not partner.active or not tools.email_normalize(partner.email):
                raise ValidationError(
                    self.env._(
                        "Recipient %s needs an active, valid email address.",
                        partner.display_name,
                    )
                )
        cc = cc.filtered(
            lambda p: p.email_normalized not in to.mapped("email_normalized")
        )
        template = move_data.get("mail_template")
        self.env["mail.thread"]._cc_check_recipients(
            to | cc, self.env.user.email, template.mail_server_id if template else False
        )
        values.update(
            partner_ids=(to | cc).ids, cc_partner_ids=cc.ids, cc_to_partner_ids=to.ids
        )
        return values
