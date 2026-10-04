# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Run via Odoo shell against an isolated erp_ses_* database only.

Exercise the real owned journal cursor, outside Odoo's rollback test harness.
No AWS or SMTP calls are made. The runner disables cron and outgoing servers.
"""

import logging

from odoo.modules import module

env = globals()["env"]
assert env.cr.dbname.startswith("erp_ses_") and "prod" not in env.cr.dbname
assert not module.current_test
c = env["mail.ses.connection"].create(
    {
        "name": "Local journal test",
        "region": "us-east-1",
        "account_id": "123456789012",
        "identity": "example.test",
        "configuration_set": "local-delivery",
        "engagement_configuration_set": "local-engagement",
        "topic_arn": "arn:aws:sns:us-east-1:123456789012:local",
        "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/local",
    }
)
env.cr.commit()
values = {
    "connection_id": c.id,
    "mail_record_id": 2147000001,
    "message_record_id": 2147000001,
    "original_message_id": "<journal-test@example.test>",
    "recipient": "recipient@example.test",
}
Journal = env["mail.ses.attempt"]
try:
    first = Journal._journal("_reserve", values)
    assert first["new"] and first["state"] == "uncertain"
    env.cr.rollback()
    again = Journal._journal("_reserve", values)
    assert (
        not again["new"]
        and again["id"] == first["id"]
        and again["state"] == "uncertain"
    )
    Journal._journal("_update_journal", first["id"], {"transport_state": "accepted"})
    env.cr.rollback()
    final = Journal._journal("_reserve", values)
    assert not final["new"] and final["state"] == "accepted"
    logging.getLogger(__name__).info(
        "OWNED JOURNAL: uncertain and accepted state survive caller rollback; "
        "no duplicate reservation"
    )
finally:
    env["mail.ses.attempt"].search([("connection_id", "=", c.id)]).unlink()
    c.unlink()
    env.cr.commit()
