# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models, tools


class MailMessage(models.Model):
    _inherit = "mail.message"

    cc_partner_ids = fields.Many2many(
        "res.partner",
        "mail_message_cc_partner_rel",
        "message_id",
        "partner_id",
        string="CC",
    )
    cc_to_partner_ids = fields.Many2many(
        "res.partner",
        "mail_message_to_partner_rel",
        "message_id",
        "partner_id",
        string="Explicit To",
    )
    cc_visible_to = fields.Char(readonly=True)
    cc_visible_cc = fields.Char(readonly=True)
    cc_recipient_snapshot = fields.Json(readonly=True)

    def _cc_snapshot(self):
        for message in self:
            if message.cc_partner_ids and not message.cc_visible_cc:
                to_addresses = list(
                    dict.fromkeys(message.cc_to_partner_ids.mapped("email_formatted"))
                )
                seen = set(message.cc_to_partner_ids.mapped("email_normalized"))
                cc_addresses = []
                for partner in message.cc_partner_ids:
                    if (
                        partner.email_normalized
                        and partner.email_normalized not in seen
                    ):
                        seen.add(partner.email_normalized)
                        cc_addresses.append(partner.email_formatted)
                message.write(
                    {
                        "cc_visible_to": ", ".join(to_addresses),
                        "cc_visible_cc": ", ".join(cc_addresses),
                        "cc_recipient_snapshot": {
                            str(p.id): p.email_normalized
                            for p in message.cc_to_partner_ids | message.cc_partner_ids
                        },
                    }
                )


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    def _get_message_create_valid_field_names(self):
        return super()._get_message_create_valid_field_names() | {
            "cc_partner_ids",
            "cc_to_partner_ids",
        }

    def _message_create(self, values_list):
        prepared = []
        for values in values_list:
            values = dict(values)
            for name in ("cc_partner_ids", "cc_to_partner_ids"):
                if name in values:
                    values[name] = [fields.Command.set(values[name])]
            prepared.append(values)
        return super()._message_create(prepared)

    def _cc_check_recipients(self, partners, email_from, mail_server=False):
        """Provider hook for manual-send preflight, before creating messages."""
        return True

    def _notify_get_recipients(self, message, msg_vals=False, **kwargs):
        recipients = super()._notify_get_recipients(
            message, msg_vals=msg_vals, **kwargs
        )
        if message.cc_partner_ids:
            message._cc_snapshot()
            explicit = set((message.cc_partner_ids | message.cc_to_partner_ids).ids)
            for recipient in recipients:
                if recipient["id"] in explicit:
                    recipient["notif"] = "email"
        return recipients


class MailMail(models.Model):
    _inherit = "mail.mail"

    def _prepare_outgoing_list(self, mail_server=False, doc_to_followers=None):
        outgoing = super()._prepare_outgoing_list(mail_server, doc_to_followers)
        message = self.mail_message_id
        if not message.cc_visible_cc:
            return outgoing
        to = tools.mail.email_split_and_format_normalize(message.cc_visible_to or "")
        cc = tools.mail.email_split_and_format_normalize(message.cc_visible_cc)
        explicit = set(tools.email_normalize_all(",".join(to + cc)))
        seen = set()
        result = []
        for values in outgoing:
            partner = values.get("partner_id")
            frozen = (
                (message.cc_recipient_snapshot or {}).get(str(partner.id))
                if partner
                else False
            )
            if frozen:
                values = dict(values, email_to_normalized=[frozen], email_to_raw=frozen)
            # Keep Odoo's personalized body and envelope. Only explicit recipients
            # get the shared conversation headers; unrelated followers stay private.
            targets = values["email_to_normalized"]
            if targets and all(address in explicit for address in targets):
                if all(address in seen for address in targets):
                    continue
                seen.update(targets)
                values = dict(
                    values, email_to=to, email_cc=cc, headers=dict(values["headers"])
                )
                values["headers"].pop("X-Msg-To-Add", None)
            result.append(values)
        return result
