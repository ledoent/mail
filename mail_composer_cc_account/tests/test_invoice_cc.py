# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestInvoiceCC(AccountTestInvoicingCommon):
    def test_invoice_cc_survives_settings_and_post(self):
        partner = self.env["res.partner"].create(
            {"name": "Annie", "email": "annie@example.test"}
        )
        cc = self.env["res.partner"].create(
            {"name": "Josh", "email": "josh@example.test"}
        )
        invoice = self.init_invoice("out_invoice", partner=partner, amounts=[100])
        invoice.action_post()
        wizard = self.env["account.move.send.wizard"].create(
            {
                "move_id": invoice.id,
                "mail_partner_ids": [Command.set(partner.ids)],
                "cc_partner_ids": [Command.set(cc.ids)],
            }
        )
        settings = wizard._get_sending_settings()
        self.assertEqual(settings["cc_partner_ids"], cc.ids)
        sender = self.env["account.move.send"]
        data = sender._get_default_sending_settings(invoice, **settings)
        values = sender._get_mail_params(invoice, data)
        self.assertEqual(set(values["partner_ids"]), set((partner | cc).ids))
        self.assertEqual(values["cc_partner_ids"], cc.ids)
        message = invoice.with_context(mail_notify_force_send=False).message_post(
            message_type="comment", subtype_xmlid="mail.mt_comment", **values
        )
        self.assertIn("josh@example.test", message.cc_visible_cc)

    def test_template_partner_to_wins_over_cc(self):
        partner = self.env["res.partner"].create(
            {"name": "Explicit To", "email": "to@example.test"}
        )
        invoice = self.init_invoice("out_invoice", partner=partner, amounts=[100])
        template = self.env["mail.template"].create(
            {
                "name": "Invoice explicit To and CC",
                "model_id": self.env["ir.model"]._get_id("account.move"),
                "use_default_to": False,
                "partner_to": str(partner.id),
                "email_cc": partner.email,
            }
        )
        to = self.env["account.move.send"]._get_default_mail_partner_ids(
            invoice, template, "en_US"
        )
        self.assertIn(partner, to)
