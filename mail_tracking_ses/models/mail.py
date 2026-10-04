# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import re

from odoo import api, fields, models, tools
from odoo.exceptions import UserError
from odoo.modules import module

from .payload import protect_links


class SesMailServer(models.Model):
    _inherit = "ir.mail_server"

    ses_connection_id = fields.Many2one(
        "mail.ses.connection", groups="base.group_system", ondelete="restrict"
    )

    def _tracking_email_get(self, message):
        if message.get("X-SES-MESSAGE-TAGS"):
            # OCA's generic SMTP hook swallows exceptions when a tracking row
            # exists. SES owns its journal and must propagate delivery failures.
            return self.env["mail.tracking.email"]
        return super()._tracking_email_get(message)

    def send_email(self, message, mail_server_id=None, **kwargs):
        if message.get("X-Odoo-SES-Mail"):
            targets = self.env.context.get("send_validated_to", [])
            if len(targets) != 1:
                raise UserError(
                    self.env._("SES tracking requires one envelope recipient.")
                )
            cc = tools.email_normalize_all(message.get("Cc", ""))
            metadata = {
                "mail_record_id": int(message["X-Odoo-SES-Mail"]),
                "message_record_id": int(message["X-Odoo-SES-Message"]),
                "original_message_id": message["Message-ID"],
                "recipient": targets[0],
                "channel": "cc" if targets[0] in cc else "to",
            }
            engagement = message.get("X-Odoo-SES-Engagement") == "1"
            for header in (
                "X-Odoo-SES-Mail",
                "X-Odoo-SES-Message",
                "X-Odoo-SES-Engagement",
            ):
                del message[header]
            self = self.with_context(ses_delivery=metadata, ses_engagement=engagement)
        server = (
            self.browse(mail_server_id)
            if mail_server_id
            else self._find_mail_server(message["From"])[0]
        )
        connection = server.ses_connection_id.sudo() if server else False
        if not connection:
            return super().send_email(message, mail_server_id=mail_server_id, **kwargs)
        context = self.env.context
        metadata = context.get("ses_delivery")
        if not metadata or not context.get("ses_queue_worker"):
            raise UserError(
                self.env._("SES sends must pass through the Odoo mail queue.")
            )
        recipient = metadata["recipient"]
        journal = self.env["mail.ses.attempt"].sudo()
        reserved = journal._journal(
            "_reserve", dict(metadata, connection_id=connection.id)
        )
        if not reserved["new"]:
            if reserved["state"] == "accepted":
                return message["Message-ID"]
            if not journal._journal("_claim_retry", reserved["id"]):
                raise UserError(
                    self.env._(
                        "This recipient has an unresolved delivery attempt. "
                        "Review it before resending."
                    )
                )
        try:
            connection._assert_ready(recipient)
        except UserError:
            journal._journal(
                "_update_journal",
                reserved["id"],
                {
                    "transport_state": "blocked",
                    "error_code": "review_or_pipeline_hold",
                },
            )
            raise
        config = (
            connection.engagement_configuration_set
            if context.get("ses_engagement")
            else connection.configuration_set
        )
        for name in (
            "X-Odoo-Database",
            "X-Odoo-MailTracking-ID",
            "X-SES-CONFIGURATION-SET",
            "X-SES-MESSAGE-TAGS",
        ):
            if name in message:
                del message[name]
        message["X-SES-CONFIGURATION-SET"] = config
        message["X-SES-MESSAGE-TAGS"] = f"odoo_attempt={reserved['token']}"
        try:
            result = super().send_email(
                message, mail_server_id=mail_server_id, **kwargs
            )
            if not result:
                raise UserError(self.env._("SES acceptance was not confirmed."))
        except Exception:
            # Do not interpret a generic SMTP exception as proof of non-delivery.
            journal._journal(
                "_update_journal",
                reserved["id"],
                {"transport_state": "uncertain", "error_code": "smtp_result_uncertain"},
            )
            raise
        journal._journal(
            "_update_journal",
            reserved["id"],
            {"transport_state": "accepted", "error_code": False},
        )
        return result


class SesMail(models.Model):
    _inherit = "mail.mail"

    ses_recipient_snapshot = fields.Json(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        mails = super().create(vals_list)
        for mail in mails:
            server = mail.sudo()._ses_server()
            if server and server.ses_connection_id:
                mail.sudo().write(
                    {
                        "mail_server_id": server.id,
                        "ses_recipient_snapshot": {
                            "to": mail.email_to or "",
                            "cc": mail.email_cc or "",
                            "partners": {
                                str(p.id): p.email_formatted for p in mail.recipient_ids
                            },
                        },
                    }
                )
        return mails

    def write(self, vals):
        if "mail_server_id" in vals:
            for mail in self:
                if (
                    mail.ses_recipient_snapshot
                    and vals["mail_server_id"] != mail.mail_server_id.id
                ):
                    raise UserError(
                        self.env._(
                            "A queued SES message cannot switch servers. "
                            "Cancel it and compose a new message."
                        )
                    )
        return super().write(vals)

    ses_engagement = fields.Boolean(
        default=True,
        help="SES open/click observations; authentication mail is always excluded.",
    )

    def _ses_server(self):
        self.ensure_one()
        return (
            self.mail_server_id
            or self.env["ir.mail_server"]._find_mail_server(self.email_from)[0]
        )

    def send(self, auto_commit=False, raise_exception=False, post_send_callback=None):
        ses = self.filtered(
            lambda mail: mail._ses_server() and mail._ses_server().ses_connection_id
        )
        ordinary = self - ses
        if ordinary:
            super(SesMail, ordinary).send(
                auto_commit=auto_commit,
                raise_exception=raise_exception,
                post_send_callback=post_send_callback,
            )
        if ses and (auto_commit or module.current_test):
            # Only scheduler-owned, already committed mail may reach SES.
            return super(SesMail, ses.with_context(ses_queue_worker=True)).send(
                auto_commit=auto_commit,
                raise_exception=raise_exception,
                post_send_callback=post_send_callback,
            )
        return True  # Keep SES mail queued until the normal mail scheduler runs.

    def _prepare_outgoing_list(self, mail_server=False, doc_to_followers=None):
        outgoing = super()._prepare_outgoing_list(mail_server, doc_to_followers)
        server = mail_server or self._ses_server()
        connection = server.ses_connection_id if server else False
        if not connection:
            return outgoing
        result = []
        seen = set()
        for values in outgoing:
            tracking_id = self.env["ir.mail_server"]._tracking_email_id_body_get(
                values["body"]
            )
            if tracking_id:
                self.env["mail.tracking.email"].sudo().browse(int(tracking_id)).unlink()
            snapshot = self.ses_recipient_snapshot
            if snapshot:
                values = dict(values)
                partner = values.get("partner_id")
                if partner:
                    frozen = snapshot["partners"].get(str(partner.id), "")
                    values["email_to_normalized"] = tools.email_normalize_all(frozen)
                    if not self.mail_message_id.cc_visible_cc:
                        values["email_to"] = (
                            tools.mail.email_split_and_format_normalize(frozen)
                        )
                else:
                    values["email_to"] = tools.mail.email_split_and_format_normalize(
                        snapshot["to"]
                    )
                    values["email_cc"] = tools.mail.email_split_and_format_normalize(
                        snapshot["cc"]
                    )
                    values["email_to_normalized"] = tools.email_normalize_all(
                        snapshot["to"] + "," + snapshot["cc"]
                    )
            if not values["email_to_normalized"]:
                result.append(values)
            for recipient in values["email_to_normalized"]:
                if recipient in seen:
                    continue
                seen.add(recipient)
                item = dict(
                    values,
                    headers=dict(values["headers"]),
                    email_to_normalized=[recipient],
                )
                # OCA's marker is removed; this integration creates tracking rows
                # when authoritative SES events arrive, including CC recipients.
                item["body"] = protect_links(
                    self.env["ir.mail_server"]._tracking_img_remove(item["body"])
                )
                item["headers"]["X-Odoo-SES-Mail"] = str(self.id)
                item["headers"]["X-Odoo-SES-Message"] = str(self.mail_message_id.id)
                item["headers"]["X-Odoo-SES-Engagement"] = (
                    "1"
                    if self.ses_engagement
                    and connection.engagement_enabled
                    and self.model != "res.users"
                    and not any(
                        x in str(self.body_html).lower()
                        for x in ("/web/reset_password", "/web/signup")
                    )
                    else "0"
                )
                result.append(item)
        return result


class SesThread(models.AbstractModel):
    _inherit = "mail.thread"

    def _cc_check_recipients(self, partners, email_from, mail_server=False):
        result = super()._cc_check_recipients(partners, email_from, mail_server)
        server = (
            mail_server
            or self.env["ir.mail_server"].sudo()._find_mail_server(email_from)[0]
        )
        connection = server.sudo().ses_connection_id if server else False
        if connection:
            for address in partners.mapped("email_normalized"):
                connection._assert_ready(address)
        return result

    @api.model
    def message_parse(self, message, save_original=False):
        # Keep core routing and authorization checks; normalize known SES reply
        # references to their original Odoo message IDs before invoking them.
        for header in ("In-Reply-To", "References"):
            value = message.get(header, "")
            for reference in re.findall(r"<[^>]+>", value):
                identifier = reference[1:-1].split("@", 1)[0]
                attempt = (
                    self.env["mail.ses.attempt"]
                    .sudo()
                    .search(
                        [
                            "|",
                            ("ses_rfc_message_id", "=", reference),
                            ("ses_message_id", "=", identifier),
                        ],
                        limit=1,
                    )
                )
                if attempt:
                    value = value.replace(reference, attempt.original_message_id)
            if header in message:
                message.replace_header(header, value)
        return super().message_parse(message, save_original=save_original)


class SesMessage(models.Model):
    _inherit = "mail.message"

    def action_ses_delivery(self):
        self.ensure_one()
        self.check_access("read")
        if not self.env.user.has_group("base.group_user"):
            raise UserError(
                self.env._("Delivery details are available to internal users only.")
            )
        attempts = (
            self.env["mail.ses.attempt"]
            .sudo()
            .search([("message_record_id", "=", self.id), ("channel", "!=", "bcc")])
        )
        rows = []
        for attempt in attempts:
            delivery = dict(attempt._fields["delivery_state"].selection)[
                attempt.delivery_state
            ]
            if attempt.delivery_state == "pending":
                delivery = dict(attempt._fields["transport_state"].selection)[
                    attempt.transport_state
                ]
            rows.append(
                fields.Command.create(
                    {
                        "recipient": attempt.recipient,
                        "channel": attempt.channel.upper(),
                        "delivery": delivery,
                        "opened_at": attempt.opened_at,
                        "clicked_at": attempt.clicked_at,
                        "attention": "Spam complaint — sending blocked"
                        if attempt.complaint_at
                        else (attempt.error_code or ""),
                    }
                )
            )
        wizard = self.env["mail.ses.delivery.wizard"].create({"line_ids": rows})
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("SES delivery"),
            "res_model": wizard._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }


class SesPartner(models.Model):
    _inherit = "res.partner"

    ses_email_status = fields.Char(
        compute="_compute_ses_email_status", groups="base.group_user"
    )

    @api.depends("email")
    def _compute_ses_email_status(self):
        for partner in self:
            blocked = (
                self.env["mail.ses.suppression"]
                .sudo()
                .search(
                    [
                        ("email_normalized", "=", partner.email_normalized),
                        ("active", "=", True),
                        ("connection_id.company_id", "in", self.env.companies.ids),
                    ],
                    limit=1,
                )
                if partner.email_normalized
                else False
            )
            partner.ses_email_status = (
                "Blocked — contact the mail administrator"
                if blocked
                else "No SES block recorded"
            )
