# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import uuid
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields, models
from odoo.modules import module


class SesAttempt(models.Model):
    _name = "mail.ses.attempt"
    _description = "SES recipient delivery"
    _order = "id desc"

    connection_id = fields.Many2one(
        "mail.ses.connection", required=True, ondelete="restrict"
    )
    company_id = fields.Many2one(related="connection_id.company_id", store=True)
    # Integers deliberately avoid FK locks against the sending transaction.
    # Only committed mail is sent by the SES queue integration.
    mail_record_id = fields.Integer(required=True, index=True)
    message_record_id = fields.Integer(required=True, index=True)
    original_message_id = fields.Char(required=True, index=True)
    recipient = fields.Char(required=True, index=True)
    channel = fields.Selection(
        [("to", "To"), ("cc", "CC"), ("bcc", "BCC")], default="to", required=True
    )
    token = fields.Char(
        required=True, default=lambda self: uuid.uuid4().hex, index=True
    )
    ses_message_id = fields.Char(index=True)
    ses_rfc_message_id = fields.Char(index=True)
    transport_state = fields.Selection(
        [
            ("uncertain", "Delivery uncertain"),
            ("accepted", "Accepted by SES"),
            ("failed", "Failed"),
            ("blocked", "Blocked"),
        ],
        default="uncertain",
        required=True,
    )
    delivery_state = fields.Selection(
        [
            ("pending", "Awaiting delivery"),
            ("delivered", "Delivered to mail server"),
            ("delayed", "Delayed"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
    )
    opened_at = fields.Datetime()
    clicked_at = fields.Datetime()
    complaint_at = fields.Datetime()
    error_code = fields.Char()
    retry_reason = fields.Text()
    tracking_email_id = fields.Many2one("mail.tracking.email", ondelete="set null")

    _token_unique = models.Constraint(
        "UNIQUE(token)", "Attempt identifiers must be unique."
    )

    def write(self, vals):
        if not self.env.su and set(vals) - {"retry_reason"}:
            from odoo.exceptions import AccessError

            raise AccessError(self.env._("Delivery observations cannot be edited."))
        return super().write(vals)

    @api.model
    def _journal(self, operation, *args):
        """A separately owned transaction is necessary around external SMTP IO.

        Never commits the caller's transaction. Tests use their rollback cursor.
        There are no calls here from a browser request: sending is queue-only.
        """
        if module.current_test:
            return getattr(self.sudo(), operation)(*args)
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            result = getattr(env[self._name], operation)(*args)
            # Only this explicitly owned cursor is committed, never the caller.
            cr.commit()  # pylint: disable=invalid-commit
            return result

    @api.model
    def _reserve(self, values):
        # Serialize competing workers before checking the unique journal key.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (73637, values["mail_record_id"])
        )
        attempt = self.search(
            [
                ("mail_record_id", "=", values["mail_record_id"]),
                ("recipient", "=", values["recipient"]),
            ],
            limit=1,
        )
        if attempt:
            return {
                "id": attempt.id,
                "token": attempt.token,
                "state": attempt.transport_state,
                "new": False,
            }
        attempt = self.create(values)
        return {
            "id": attempt.id,
            "token": attempt.token,
            "state": attempt.transport_state,
            "new": True,
        }

    @api.model
    def _update_journal(self, attempt_id, values):
        attempt = self.browse(attempt_id)
        if values.get("transport_state") == "uncertain" and attempt.ses_message_id:
            return True  # A concurrently received SES event already confirms it.
        attempt.write(values)
        return True

    def action_allow_retry(self):
        self.ensure_one()
        if not (
            self.env.su
            or self.env.user.has_group("mail_tracking_ses.group_ses_manager")
            or self.env.user.has_group("base.group_system")
        ):
            from odoo.exceptions import AccessError

            raise AccessError(
                self.env._("Only a mail administrator can authorize a resend.")
            )
        from odoo.exceptions import UserError

        if self.transport_state == "accepted" or self.delivery_state == "delivered":
            raise UserError(
                self.env._(
                    "This copy was accepted. Compose a new message to send it again."
                )
            )
        if not self.retry_reason:
            raise UserError(
                self.env._(
                    "Record why another send is appropriate; "
                    "uncertain mail may already have arrived."
                )
            )
        self.connection_id._assert_ready(self.recipient)
        mail = self.env["mail.mail"].sudo().browse(self.mail_record_id).exists()
        if not mail:
            raise UserError(
                self.env._("The queued email is unavailable. Compose a new message.")
            )
        # Retain the original journal and create a fresh token so late events
        # cannot be mistaken for the explicitly authorized new attempt.
        self.sudo().copy(
            {
                "token": uuid.uuid4().hex,
                "transport_state": "failed",
                "error_code": "retry_authorized",
                "delivery_state": "pending",
                "opened_at": False,
                "clicked_at": False,
                "complaint_at": False,
                "ses_message_id": False,
                "ses_rfc_message_id": False,
                "tracking_email_id": False,
            }
        )
        mail.write({"state": "outgoing", "failure_reason": False})

    @api.model
    def _claim_retry(self, attempt_id):
        self.env.cr.execute(
            "SELECT id FROM mail_ses_attempt WHERE id=%s FOR UPDATE", (attempt_id,)
        )
        attempt = self.browse(attempt_id)
        attempt.invalidate_recordset()
        if attempt.error_code != "retry_authorized":
            return False
        attempt.write({"transport_state": "uncertain", "error_code": False})
        return True

    @api.model
    def _cron_retention(self):
        self.env["mail.ses.event"].search(
            [
                ("create_date", "<", fields.Datetime.now() - timedelta(days=90)),
                ("processed", "=", True),
            ]
        ).unlink()
        self.search(
            [
                ("create_date", "<", fields.Datetime.now() - timedelta(days=365)),
                ("transport_state", "=", "accepted"),
                ("complaint_at", "=", False),
            ]
        ).unlink()
