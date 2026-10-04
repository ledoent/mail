# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

from odoo import api, fields, models, tools
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


class SesSuppression(models.Model):
    _name = "mail.ses.suppression"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "SES blocked address"
    _rec_name = "email"

    connection_id = fields.Many2one(
        "mail.ses.connection", required=True, ondelete="restrict"
    )
    account_id = fields.Char(related="connection_id.account_id", store=True)
    region = fields.Char(related="connection_id.region", store=True)
    email = fields.Char(
        required=True, help="Original case is retained for AWS suppression operations."
    )
    email_normalized = fields.Char(required=True, index=True)
    reason = fields.Selection(
        [("BOUNCE", "Permanent bounce"), ("COMPLAINT", "Spam complaint")], required=True
    )
    active = fields.Boolean(default=True)
    source = fields.Char()
    resolution = fields.Text()
    recipient_confirmation = fields.Boolean(
        help="Recipient permission to resume has been documented in the resolution."
    )
    resolved_by = fields.Many2one("res.users", readonly=True)
    resolved_at = fields.Datetime(readonly=True)
    aws_sync_pending = fields.Boolean(default=True, readonly=True)

    _address_unique = models.Constraint(
        "UNIQUE(account_id, region, email_normalized)",
        "This address already has a suppression record.",
    )

    def write(self, vals):
        if not self.env.su and set(vals) - {
            "resolution",
            "recipient_confirmation",
            "activity_ids",
        }:
            raise AccessError(
                self.env._(
                    "Use the reviewed resolution action to change a suppression."
                )
            )
        return super().write(vals)

    @api.model
    def _block(self, connection, email, reason, source="SES event"):
        normalized = tools.email_normalize(email)
        if not normalized:
            return self.browse()
        record = self.with_context(active_test=False).search(
            [
                ("account_id", "=", connection.account_id),
                ("region", "=", connection.region),
                ("email_normalized", "=", normalized),
            ],
            limit=1,
        )
        new = not record or not record.active
        vals = {"active": True, "aws_sync_pending": source != "AWS suppression list"}
        if source == "AWS suppression list":
            vals["email"] = email
        if not record:
            vals.update(
                connection_id=connection.id,
                email=email,
                email_normalized=normalized,
                reason=reason,
                source=source,
            )
            record = self.create(vals)
        else:
            if reason == "COMPLAINT":
                vals["reason"] = reason
            if new:
                vals.update(
                    resolution=False,
                    recipient_confirmation=False,
                    resolved_by=False,
                    resolved_at=False,
                )
            record.write(vals)
        if new:
            record.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=connection.administrator_id.id,
                summary="Review blocked SES address",
                note="Further SES mail is held until this address is reviewed.",
            )
        return record

    def action_resolve(self):
        if not (
            self.env.su
            or self.env.user.has_group("mail_tracking_ses.group_ses_manager")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError(
                self.env._("Only a mail administrator can resolve blocked addresses.")
            )
        for record in self:
            if not record.resolution or (
                record.reason == "COMPLAINT" and not record.recipient_confirmation
            ):
                raise UserError(
                    self.env._(
                        "Document the resolution and recipient confirmation "
                        "for a complaint."
                    )
                )
            # AWS is changed first. On failure retain the local block. A DB
            # rollback after AWS deletion is also safe: local blocking remains.
            client = record.connection_id._client("sesv2")
            try:
                client.delete_suppressed_destination(EmailAddress=record.email)
            except client.exceptions.NotFoundException:
                _logger.info("SES suppression already absent for record %s", record.id)
            record.sudo().write(
                {
                    "active": False,
                    "aws_sync_pending": False,
                    "resolved_by": self.env.user.id,
                    "resolved_at": fields.Datetime.now(),
                }
            )
            record.activity_feedback(
                ["mail.mail_activity_data_todo"], feedback=record.resolution
            )

    @api.model
    def _cron_publish(self):
        for record in self.search([("aws_sync_pending", "=", True)], limit=100):
            try:
                record.connection_id._client("sesv2").put_suppressed_destination(
                    EmailAddress=record.email, Reason=record.reason
                )
            except Exception:
                continue  # Local protection remains; show pending in admin UI.
            record.aws_sync_pending = False
