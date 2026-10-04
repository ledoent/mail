# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from datetime import datetime, timezone

from odoo import api, fields, models


class SesEvent(models.Model):
    _name = "mail.ses.event"
    _description = "Sanitized SES event"
    _order = "id desc"

    connection_id = fields.Many2one(
        "mail.ses.connection", required=True, ondelete="restrict"
    )
    event_key = fields.Char(required=True, index=True)
    payload = fields.Json(required=True)
    kind = fields.Char(required=True)
    token = fields.Char(index=True)
    processed = fields.Boolean(default=False)
    suppression_applied = fields.Boolean(default=False)
    attempt_id = fields.Many2one("mail.ses.attempt", ondelete="set null")

    _event_unique = models.Constraint(
        "UNIQUE(connection_id, event_key)", "SES event already received."
    )

    @api.model
    def _ingest(self, connection, data):
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (73638, connection.id)
        )
        event = self.search(
            [
                ("connection_id", "=", connection.id),
                ("event_key", "=", data["event_key"]),
            ],
            limit=1,
        )
        if not event:
            event = self.create(
                {
                    "connection_id": connection.id,
                    "event_key": data["event_key"],
                    "payload": data,
                    "kind": data["kind"],
                    "token": data["attempt"],
                }
            )
        event._process()
        return event

    def _process(self):
        for event in self.filtered(lambda e: not e.processed):
            data = event.payload
            attempt = (
                self.env["mail.ses.attempt"].search(
                    [
                        ("connection_id", "=", event.connection_id.id),
                        ("token", "=", event.token),
                    ],
                    limit=1,
                )
                if event.token
                else self.env["mail.ses.attempt"]
            )
            # Never resolve a message by address alone. Unknown events remain
            # inspectable; address-level complaints still protect future sends.
            if not event.suppression_applied and (
                data["kind"] == "Complaint"
                or (data["kind"] == "Bounce" and data["bounce_type"] == "Permanent")
            ):
                reason = "COMPLAINT" if data["kind"] == "Complaint" else "BOUNCE"
                for address in data["recipients"]:
                    self.env["mail.ses.suppression"]._block(
                        event.connection_id, address, reason
                    )
                event.suppression_applied = True
            if not attempt or attempt.recipient not in {
                a.lower() for a in data["recipients"]
            }:
                continue
            if attempt.ses_message_id and attempt.ses_message_id != data["ses_id"]:
                continue
            when = (
                datetime.fromisoformat(data["time"].replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
                if data.get("time")
                else fields.Datetime.now()
            )
            vals = {"ses_message_id": data["ses_id"], "transport_state": "accepted"}
            rfc = data.get("rfc_message_id")
            if rfc and rfc != attempt.original_message_id:
                vals["ses_rfc_message_id"] = rfc
            kind = data["kind"]
            if kind == "Delivery" and attempt.delivery_state != "failed":
                vals["delivery_state"] = "delivered"
            elif kind == "DeliveryDelay" and attempt.delivery_state == "pending":
                vals["delivery_state"] = "delayed"
            elif kind in {"Bounce", "Reject", "Rendering Failure"}:
                vals["delivery_state"] = "failed"
                vals["error_code"] = data.get("subtype") or kind
            elif kind == "Open" and (not attempt.opened_at or when < attempt.opened_at):
                vals["opened_at"] = when
            elif kind == "Click" and (
                not attempt.clicked_at or when < attempt.clicked_at
            ):
                vals["clicked_at"] = when
            elif kind == "Complaint":
                vals["complaint_at"] = when
            attempt.write(vals)
            attempt._update_tracking(kind, when, data)
            event.write({"attempt_id": attempt.id, "processed": True})

    @api.model
    def _reconcile(self, connection):
        self.search(
            [("connection_id", "=", connection.id), ("processed", "=", False)],
            limit=100,
        )._process()


class SesTracking(models.Model):
    _inherit = "mail.tracking.email"

    ses_attempt_id = fields.Many2one(
        "mail.ses.attempt",
        ondelete="set null",
        groups="base.group_system,mail_tracking_ses.group_ses_manager",
    )


class SesAttemptTracking(models.Model):
    _inherit = "mail.ses.attempt"

    def _update_tracking(self, kind, when, data):
        self.ensure_one()
        tracking = self.tracking_email_id
        if not tracking:
            message = self.env["mail.message"].browse(self.message_record_id).exists()
            if not message:
                return
            partner = self.env["res.partner"].search(
                [("email_normalized", "=", self.recipient)], limit=1
            )
            tracking = self.env["mail.tracking.email"].create(
                {
                    "name": message.subject or "SES message",
                    "mail_message_id": message.id,
                    "recipient": self.recipient,
                    "sender": message.email_from,
                    "partner_id": partner.id,
                    "ses_attempt_id": self.id,
                }
            )
            self.tracking_email_id = tracking
        mapping = {
            "Send": "sent",
            "Delivery": "delivered",
            "DeliveryDelay": "deferral",
            "Bounce": "hard_bounce"
            if data.get("bounce_type") == "Permanent"
            else "soft_bounce",
            "Complaint": "spam",
            "Reject": "reject",
            "Rendering Failure": "reject",
            "Open": "open",
            "Click": "click",
        }
        self.env["mail.tracking.event"].create(
            {
                "tracking_email_id": tracking.id,
                "recipient": self.recipient,
                "event_type": mapping[kind],
                "time": when,
                "timestamp": when.replace(tzinfo=timezone.utc).timestamp(),
                "url": data.get("url") or False,
            }
        )
        # Conservative OCA summary, independent observations remain on the journal.
        state = (
            "spam"
            if self.complaint_at
            else (
                "error"
                if self.delivery_state == "failed"
                else ("delivered" if self.delivery_state == "delivered" else "sent")
            )
        )
        tracking.state = state
