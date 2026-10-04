# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMessageCC(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.to = cls.env["res.partner"].create(
            {"name": "AP One", "email": "ap@example.test"}
        )
        cls.cc = cls.env["res.partner"].create(
            {"name": "Purchasing", "email": "buyer@example.test"}
        )
        cls.target = cls.env["res.partner"].create({"name": "Customer"})

    def composer(self, **values):
        return self.env["mail.compose.message"].create(
            {
                "model": "res.partner",
                "res_ids": str(self.target.ids),
                "composition_mode": "comment",
                "body": "Invoice details",
                "subject": "Invoice",
                "subtype_id": self.env.ref("mail.mt_comment").id,
                "partner_ids": [Command.set(self.to.ids)],
                "cc_partner_ids": [Command.set(self.cc.ids)],
                "force_send": False,
                **values,
            }
        )

    def test_cc_is_explicit_and_does_not_subscribe(self):
        wizard = self.composer()
        messages = wizard._action_send_mail_comment(self.target.ids)
        self.assertEqual(messages.cc_partner_ids, self.cc)
        self.assertEqual(messages.cc_to_partner_ids, self.to)
        self.assertNotIn(self.cc, self.target.message_partner_ids)
        mails = self.env["mail.mail"].search([("mail_message_id", "in", messages.ids)])
        outgoing = [item for mail in mails for item in mail._prepare_outgoing_list()]
        envelopes = [tuple(item["email_to_normalized"]) for item in outgoing]
        self.assertIn(("ap@example.test",), envelopes)
        self.assertIn(("buyer@example.test",), envelopes)
        for item in outgoing:
            if item["email_to_normalized"] in [
                ["ap@example.test"],
                ["buyer@example.test"],
            ]:
                self.assertIn("ap@example.test", "".join(item["email_to"]))
                self.assertIn("buyer@example.test", "".join(item["email_cc"]))

    def test_template_cc_separate_and_manual_changes_survive_body_edit(self):
        template = self.env["mail.template"].create(
            {
                "name": "Test",
                "model_id": self.env.ref("base.model_res_partner").id,
                "email_to": self.to.email,
                "email_cc": self.cc.email,
                "body_html": "Hello",
            }
        )
        wizard = self.composer(template_id=template.id)
        self.assertEqual(wizard.cc_partner_ids, self.cc)
        self.assertNotIn(self.cc, wizard.partner_ids)
        wizard.cc_partner_ids = False
        wizard.body = "Edited"
        self.assertFalse(wizard.cc_partner_ids)

    def test_to_takes_precedence(self):
        wizard = self.composer(cc_partner_ids=[Command.set((self.cc | self.to).ids)])
        values = wizard._prepare_mail_values_rendered(self.target.ids)[self.target.id]
        self.assertEqual(values["cc_partner_ids"], self.cc.ids)

    def test_invalid_cc_prevents_send(self):
        self.cc.email = "not-an-address"
        with self.assertRaises(ValidationError):
            self.composer()._prepare_mail_values_rendered(self.target.ids)
