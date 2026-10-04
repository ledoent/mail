# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
from datetime import timedelta
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.base.models.ir_mail_server import IrMail_Server as CoreServer

from ..models.payload import protect_links, safe_url, sanitize_event


@tagged("post_install", "-at_install")
class TestSES(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["mail.ses.connection"].create(
            {
                "name": "Test SES",
                "account_id": "123456789012",
                "region": "us-east-1",
                "identity": "example.test",
                "configuration_set": "test-delivery",
                "engagement_configuration_set": "test-engagement",
                "topic_arn": "arn:aws:sns:us-east-1:123456789012:test",
                "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/test",
                "enabled": True,
                "last_poll": fields.Datetime.now(),
                "last_sync": fields.Datetime.now(),
            }
        )
        cls.server = cls.env["ir.mail_server"].create(
            {
                "name": "SES test",
                "smtp_host": "email-smtp.us-east-1.amazonaws.com",
                "smtp_port": 587,
                "smtp_encryption": "starttls",
                "ses_connection_id": cls.connection.id,
            }
        )
        cls.message = cls.env["mail.message"].create(
            {
                "subject": "Invoice",
                "body": "Hello",
                "email_from": "sender@example.test",
                "model": "res.partner",
                "res_id": cls.env.user.partner_id.id,
            }
        )
        cls.attempt = (
            cls.env["mail.ses.attempt"]
            .sudo()
            .create(
                {
                    "connection_id": cls.connection.id,
                    "mail_record_id": 91001,
                    "message_record_id": cls.message.id,
                    "original_message_id": cls.message.message_id,
                    "recipient": "ap@example.test",
                    "transport_state": "accepted",
                }
            )
        )

    def event(self, kind, token=None, **extra):
        data = {
            "kind": kind,
            "ses_id": "ses-message-1",
            "attempt": token or self.attempt.token,
            "time": "2026-10-04T12:00:00Z",
            "recipients": [self.attempt.recipient],
            "bounce_type": "",
            "subtype": "",
            "url": "",
            "rfc_message_id": "<ses-message-1@us-east-1.amazonses.com>",
            **extra,
        }
        data["event_key"] = kind + ":" + data["attempt"] + ":" + data["time"]
        return self.env["mail.ses.event"].sudo()._ingest(self.connection, data)

    def test_out_of_order_engagement_cannot_erase_complaint(self):
        self.event("Complaint")
        self.event("Open")
        self.event("Delivery")
        self.assertTrue(self.attempt.complaint_at)
        self.assertTrue(self.attempt.opened_at)
        self.assertEqual(self.attempt.delivery_state, "delivered")
        self.assertEqual(self.attempt.tracking_email_id.state, "spam")
        with self.assertRaises(UserError):
            self.connection._assert_ready("ap@example.test")

    def test_duplicate_event_and_activity(self):
        first = self.event("Complaint")
        second = self.event("Complaint")
        self.assertEqual(first, second)
        blocks = (
            self.env["mail.ses.suppression"]
            .sudo()
            .search([("email_normalized", "=", "ap@example.test")])
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks.activity_ids), 1)

    def test_hard_bounce_blocks_soft_bounce_does_not(self):
        self.event("Bounce", bounce_type="Transient")
        self.connection._assert_ready("ap@example.test")
        self.event("Bounce", bounce_type="Permanent", time="2026-10-04T13:00:00Z")
        with self.assertRaises(UserError):
            self.connection._assert_ready("ap@example.test")

    def test_unknown_correlation_is_not_assigned_by_address(self):
        event = self.event("Delivery", token="unknown")
        self.assertFalse(event.processed)
        self.assertEqual(self.attempt.delivery_state, "pending")

    def test_unknown_complaint_still_blocks(self):
        event = self.event("Complaint", token="unknown")
        self.assertFalse(event.processed)
        with self.assertRaises(UserError):
            self.connection._assert_ready("ap@example.test")

    def test_stale_pipeline_holds_sending(self):
        self.connection.last_poll = fields.Datetime.now() - timedelta(minutes=6)
        with self.assertRaises(UserError):
            self.connection._assert_ready("other@example.test")

    def test_aws_failure_does_not_unblock(self):
        record = (
            self.env["mail.ses.suppression"]
            .sudo()
            ._block(self.connection, "ap@example.test", "COMPLAINT")
        )
        record.write(
            {"resolution": "Recipient confirmed", "recipient_confirmation": True}
        )
        client = MagicMock()
        client.exceptions.NotFoundException = LookupError
        client.delete_suppressed_destination.side_effect = RuntimeError("AWS down")
        with (
            patch.object(type(self.connection), "_client", return_value=client),
            self.assertRaises(RuntimeError),
        ):
            record.action_resolve()
        self.assertTrue(record.active)

    def test_only_administrator_can_resolve(self):
        user = new_test_user(self.env, login="ses_regular", groups="base.group_user")
        record = (
            self.env["mail.ses.suppression"]
            .sudo()
            ._block(self.connection, "ap@example.test", "BOUNCE")
        )
        with self.assertRaises(AccessError):
            record.with_user(user).action_resolve()

    def test_event_sanitization_and_source_validation(self):
        payload = {
            "eventType": "Click",
            "mail": {
                "sendingAccountId": self.connection.account_id,
                "messageId": "ses",
                "destination": ["ap@example.test"],
                "tags": {
                    "ses:configuration-set": ["test-engagement"],
                    "odoo_attempt": [self.attempt.token],
                },
            },
            "click": {
                "timestamp": "2026-10-04T12:00:00Z",
                "link": "https://user:secret@example.test/path-token?access_token=secret",
                "ipAddress": "127.0.0.1",
            },
        }
        envelope = {
            "Type": "Notification",
            "TopicArn": self.connection.topic_arn,
            "Message": json.dumps(payload),
        }
        data = sanitize_event(
            envelope,
            self.connection.account_id,
            self.connection.topic_arn,
            {"test-engagement"},
        )
        self.assertEqual(data["url"], "https://example.test")
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("127.0.0.1", json.dumps(data))
        with self.assertRaises(ValueError):
            sanitize_event(
                envelope, "999999999999", self.connection.topic_arn, {"test-engagement"}
            )

    def test_sensitive_links_are_not_rewritten(self):
        protected = protect_links(
            '<p><a href="https://example.test/my/invoice?access_token=secret">Invoice</a></p>'
        )
        self.assertIn("ses:no-track", protected)
        self.assertEqual(safe_url("javascript:alert(1)"), "")

    def test_per_recipient_splitting_keeps_headers(self):
        mail = self.env["mail.mail"].create(
            {
                "subject": "Test",
                "body_html": "<p>Hello</p>",
                "email_from": "sender@example.test",
                "email_to": "one@example.test,two@example.test",
                "email_cc": "cc@example.test",
                "mail_server_id": self.server.id,
            }
        )
        outgoing = mail._prepare_outgoing_list(self.server)
        self.assertEqual(len(outgoing), 3)
        self.assertEqual(
            {tuple(v["email_to_normalized"]) for v in outgoing},
            {("one@example.test",), ("two@example.test",), ("cc@example.test",)},
        )
        for values in outgoing:
            self.assertIn("one@example.test", ",".join(values["email_to"]))
            self.assertIn("cc@example.test", ",".join(values["email_cc"]))
            self.assertNotIn("data-odoo-tracking-email", values["body"])

    def send_one(self, fail=False):
        message = EmailMessage()
        message["From"] = "sender@example.test"
        message["To"] = "ap@example.test"
        message["Message-ID"] = self.message.message_id
        message.set_content("Hello")
        metadata = {
            "mail_record_id": 91002,
            "message_record_id": self.message.id,
            "original_message_id": self.message.message_id,
            "recipient": "ap@example.test",
            "channel": "to",
        }
        model = self.env["ir.mail_server"].with_context(
            ses_delivery=metadata, ses_queue_worker=True
        )
        with patch.object(
            CoreServer,
            "send_email",
            side_effect=RuntimeError("Lost connection") if fail else None,
            return_value=message["Message-ID"],
        ) as sending:
            result = model.send_email(message, mail_server_id=self.server.id)
        return result, sending.call_count, message

    def test_accepted_recipient_not_resent(self):
        self.assertEqual(self.send_one()[1], 1)
        result, calls, message = self.send_one()
        self.assertEqual(calls, 0)
        self.assertTrue(result)

    def test_uncertain_send_is_not_automatically_retried(self):
        # Odoo assertRaises rolls back its savepoint, which would erase the
        # test-cursor journal. Preserve it to exercise retry protection.
        try:
            self.send_one(fail=True)
        except RuntimeError as error:
            self.assertIn("Lost connection", str(error))
        else:
            self.fail("Expected an ambiguous transport failure")
        with self.assertRaises(UserError):
            self.send_one()

    def test_ses_headers_and_no_database_header(self):
        _, _, message = self.send_one()
        self.assertEqual(message["X-SES-CONFIGURATION-SET"], "test-delivery")
        self.assertIn("odoo_attempt=", message["X-SES-MESSAGE-TAGS"])
        self.assertNotIn("X-Odoo-Database", message)

    def test_sqs_ack_waits_for_commit(self):
        client = MagicMock()
        client.get_queue_attributes.return_value = {
            "Attributes": {"ApproximateNumberOfMessages": "0"}
        }
        client.receive_message.return_value = {"Messages": []}
        with patch.object(type(self.connection), "_client", return_value=client):
            self.connection._poll()
        client.delete_message.assert_not_called()

    def test_actual_smtp_envelopes_and_attachments(self):
        attachment = self.env["ir.attachment"].create(
            {"name": "invoice.pdf", "raw": b"%PDF-test", "mimetype": "application/pdf"}
        )
        mail = self.env["mail.mail"].create(
            {
                "subject": "Invoice",
                "body_html": "<p>Invoice attached</p>",
                "email_from": "sender@example.test",
                "email_to": "one@example.test,two@example.test",
                "email_cc": "copy@example.test",
                "mail_server_id": self.server.id,
                "attachment_ids": [(6, 0, attachment.ids)],
            }
        )
        session = SimpleNamespace(
            from_filter=False,
            smtp_from=False,
            send_message=MagicMock(),
            quit=MagicMock(),
        )
        with (
            patch.object(CoreServer, "_connect__", return_value=session),
            patch.object(CoreServer, "_disable_send", return_value=False),
        ):
            mail.send(raise_exception=True)
        self.assertEqual(mail.state, "sent")
        self.assertEqual(session.send_message.call_count, 3)
        self.assertEqual(
            {tuple(call.args[2]) for call in session.send_message.call_args_list},
            {("one@example.test",), ("two@example.test",), ("copy@example.test",)},
        )
        tags = set()
        for call in session.send_message.call_args_list:
            message = call.args[0]
            self.assertIn("one@example.test", message["To"])
            self.assertIn("two@example.test", message["To"])
            self.assertIn("copy@example.test", message["Cc"])
            self.assertNotIn("X-Odoo-SES-Mail", message)
            self.assertNotIn("X-Odoo-Database", message)
            tags.add(message["X-SES-MESSAGE-TAGS"])
            self.assertTrue(
                any(
                    part.get_payload(decode=True) == b"%PDF-test"
                    for part in message.walk()
                )
            )
        self.assertEqual(len(tags), 3)

    def test_different_account_suppression_does_not_block(self):
        other = self.connection.copy(
            {
                "account_id": "999999999999",
                "topic_arn": "arn:aws:sns:us-east-1:999999999999:test",
                "queue_url": "https://sqs.us-east-1.amazonaws.com/999999999999/test",
            }
        )
        self.env["mail.ses.suppression"].sudo()._block(
            other, "ap@example.test", "COMPLAINT"
        )
        self.connection._assert_ready("ap@example.test")

    def test_unknown_complaint_reconciliation_does_not_reopen_review(self):
        event = self.event("Complaint", token="unmatched")
        block = (
            self.env["mail.ses.suppression"]
            .sudo()
            .search([("email_normalized", "=", "ap@example.test")])
        )
        block.active = False
        event._process()
        self.assertFalse(block.active)

    def test_manual_send_preflight_blocks_cc(self):
        recipient = self.env["res.partner"].create(
            {"name": "Copy", "email": "copy@example.test"}
        )
        self.env["mail.ses.suppression"].sudo()._block(
            self.connection, recipient.email, "BOUNCE"
        )
        with self.assertRaises(UserError):
            self.env["mail.thread"]._cc_check_recipients(
                recipient, "sender@example.test", self.server
            )

    def test_plain_user_cannot_read_diagnostics_or_edit_blocks(self):
        user = new_test_user(
            self.env, login="ses_no_diagnostics", groups="base.group_user"
        )
        block = (
            self.env["mail.ses.suppression"]
            .sudo()
            ._block(self.connection, "ap@example.test", "BOUNCE")
        )
        with self.assertRaises(AccessError):
            block.with_user(user).write({"active": False})
        with self.assertRaises(AccessError):
            self.attempt.with_user(user).read(["token"])

    def test_retention_preserves_unresolved_complaint(self):
        self.event("Complaint")
        self.attempt.create_date = fields.Datetime.now() - timedelta(days=400)
        self.env["mail.ses.attempt"].sudo()._cron_retention()
        self.assertTrue(self.attempt.exists())

    def test_reply_parser_normalizes_ses_message_id(self):
        self.event("Send")
        message = EmailMessage()
        message["From"] = "ap@example.test"
        message["To"] = "sender@example.test"
        message["Message-ID"] = "<reply@example.test>"
        message["In-Reply-To"] = "<ses-message-1@us-east-1.amazonses.com>"
        message.set_content("Thanks")
        parsed = self.env["mail.thread"].message_parse(message)
        self.assertEqual(parsed["in_reply_to"], self.message.message_id)

    def test_queued_recipient_snapshot_and_server_are_preserved(self):
        partner = self.env["res.partner"].create(
            {"name": "AP", "email": "original@example.test"}
        )
        mail = self.env["mail.mail"].create(
            {
                "subject": "Test",
                "body_html": "Hello",
                "email_from": "sender@example.test",
                "recipient_ids": [(6, 0, partner.ids)],
                "mail_server_id": self.server.id,
            }
        )
        partner.email = "replacement@example.test"
        outgoing = mail._prepare_outgoing_list(self.server)
        self.assertEqual(outgoing[0]["email_to_normalized"], ["original@example.test"])
        with self.assertRaises(UserError):
            mail.mail_server_id = False
