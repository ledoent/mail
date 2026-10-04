# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import logging
from datetime import timedelta
from urllib.parse import urlsplit

import boto3

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .payload import sanitize_event

_logger = logging.getLogger(__name__)


class SesConnection(models.Model):
    _name = "mail.ses.connection"
    _description = "SES connection"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    enabled = fields.Boolean(default=False)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )
    region = fields.Char(required=True, default="us-east-1")
    account_id = fields.Char(required=True)
    identity = fields.Char(
        required=True, help="Verified sender domain or email identity."
    )
    configuration_set = fields.Char(
        required=True, help="Delivery-only configuration set."
    )
    engagement_configuration_set = fields.Char(required=True)
    engagement_enabled = fields.Boolean(default=True)
    topic_arn = fields.Char(required=True)
    queue_url = fields.Char(required=True)
    dead_letter_queue_url = fields.Char()
    administrator_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user
    )
    last_poll = fields.Datetime(readonly=True)
    last_sync = fields.Datetime(readonly=True)
    last_check = fields.Datetime(readonly=True)
    diagnostic = fields.Text(readonly=True)
    pending_count = fields.Integer(readonly=True)
    oldest_event_seconds = fields.Integer(readonly=True)
    dead_letter_count = fields.Integer(readonly=True)
    health = fields.Selection(
        [("disabled", "Disabled"), ("healthy", "Healthy"), ("stale", "Sending held")],
        compute="_compute_health",
    )

    @api.depends("enabled", "last_poll", "last_sync")
    def _compute_health(self):
        for connection in self:
            connection.health = (
                "disabled"
                if not connection.enabled
                else ("healthy" if connection._healthy() else "stale")
            )

    @api.constrains(
        "region",
        "account_id",
        "topic_arn",
        "queue_url",
        "dead_letter_queue_url",
        "configuration_set",
        "engagement_configuration_set",
    )
    def _check_configuration(self):
        for c in self:
            if not c.account_id.isdigit() or len(c.account_id) != 12:
                raise ValidationError(self.env._("Enter a 12-digit AWS account ID."))
            arn = c.topic_arn.split(":")
            if (
                len(arn) != 6
                or arn[0] != "arn"
                or arn[2:5] != ["sns", c.region, c.account_id]
            ):
                raise ValidationError(
                    self.env._("The SNS topic must match the account and region.")
                )
            for value in filter(None, [c.queue_url, c.dead_letter_queue_url]):
                url = urlsplit(value)
                expected = f"sqs.{c.region}.amazonaws.com"
                if (
                    url.scheme != "https"
                    or url.netloc != expected
                    or not url.path.startswith(f"/{c.account_id}/")
                    or url.query
                    or url.fragment
                ):
                    raise ValidationError(
                        self.env._(
                            "Use an AWS SQS URL in the configured account and region."
                        )
                    )
            if c.configuration_set == c.engagement_configuration_set:
                raise ValidationError(
                    self.env._(
                        "Delivery and engagement need distinct configuration sets."
                    )
                )

    def _client(self, service):
        self.ensure_one()
        # Credentials are supplied by the runtime's AWS credential chain, never
        # database fields or credentials embedded in user-controlled URLs.
        from botocore.config import Config

        return boto3.client(
            service,
            region_name=self.region,
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )

    def _healthy(self):
        now = fields.Datetime.now()
        return bool(
            self.last_poll
            and self.last_sync
            and now - self.last_poll < timedelta(minutes=5)
            and now - self.last_sync < timedelta(hours=2)
            and not self.dead_letter_count
            and self.oldest_event_seconds < 300
        )

    def _assert_ready(self, recipient):
        self.ensure_one()
        if not self.active or not self.enabled or not self._healthy():
            raise UserError(
                self.env._(
                    "SES sending is held: verify the event queue and suppression sync."
                )
            )
        blocked = (
            self.env["mail.ses.suppression"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account_id),
                    ("region", "=", self.region),
                    ("email_normalized", "=", recipient.lower()),
                    ("active", "=", True),
                ],
                limit=1,
            )
        )
        if blocked:
            raise UserError(
                self.env._(
                    "Sending to %s is blocked pending mail administrator review.",
                    recipient,
                )
            )

    def action_check(self):
        self.ensure_one()
        ses = self._client("sesv2")
        account = ses.get_account()
        identity = ses.get_email_identity(EmailIdentity=self.identity)
        caller = self._client("sts").get_caller_identity()
        issues = []
        if caller["Account"] != self.account_id:
            issues.append("AWS credentials belong to another account")
        if not account.get("SendingEnabled") or not account.get(
            "ProductionAccessEnabled"
        ):
            issues.append("SES sending disabled or account is in sandbox")
        if not identity.get("VerifiedForSendingStatus"):
            issues.append("Sender identity is not verified")
        if identity.get("DkimAttributes", {}).get("Status") != "SUCCESS":
            issues.append("DKIM is not verified")
        mail_from = identity.get("MailFromAttributes", {})
        if (
            mail_from.get("MailFromDomain")
            and mail_from.get("MailFromDomainStatus") != "SUCCESS"
        ):
            issues.append("Custom MAIL FROM is not verified")
        for name, engagement in [
            (self.configuration_set, False),
            (self.engagement_configuration_set, True),
        ]:
            config = ses.get_configuration_set(ConfigurationSetName=name)
            suppressed = set(
                config.get("SuppressionOptions", {}).get(
                    "SuppressedReasons",
                    account.get("SuppressionAttributes", {}).get(
                        "SuppressedReasons", []
                    ),
                )
            )
            if not {"BOUNCE", "COMPLAINT"} <= suppressed:
                issues.append(f"{name}: enable bounce and complaint suppression")
            destinations = ses.get_configuration_set_event_destinations(
                ConfigurationSetName=name
            )["EventDestinations"]
            types = set()
            for destination in destinations:
                if (
                    destination.get("Enabled")
                    and destination.get("SnsDestination", {}).get("TopicArn")
                    == self.topic_arn
                ):
                    types.update(destination.get("MatchingEventTypes", []))
            required = {
                "SEND",
                "DELIVERY",
                "DELIVERY_DELAY",
                "BOUNCE",
                "COMPLAINT",
                "REJECT",
                "RENDERING_FAILURE",
            }
            if engagement:
                required |= {"OPEN", "CLICK"}
            if not required <= types or (not engagement and {"OPEN", "CLICK"} & types):
                issues.append(f"{name}: event destination configuration does not match")
        self._client("sqs").get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["QueueArn"]
        )
        self.write(
            {
                "last_check": fields.Datetime.now(),
                "diagnostic": "\n".join(issues)
                or (
                    "Configuration checks passed. "
                    "Run queue and suppression sync before enabling."
                ),
            }
        )
        return True

    def action_enable(self):
        self.action_check()
        if not self.diagnostic.startswith("Configuration checks passed"):
            raise UserError(self.diagnostic)
        self._sync_suppressions()
        self._poll()
        self.enabled = True

    def _poll(self):
        self.ensure_one()
        client = self._client("sqs")
        attrs = client.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]
        self.pending_count = int(attrs.get("ApproximateNumberOfMessages", 0))
        if self.dead_letter_queue_url:
            dead = client.get_queue_attributes(
                QueueUrl=self.dead_letter_queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )["Attributes"]
            self.dead_letter_count = int(dead.get("ApproximateNumberOfMessages", 0))
        received = []
        for _batch in range(10):
            response = client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=0,
                VisibilityTimeout=120,
                MessageSystemAttributeNames=["SentTimestamp"],
            )
            batch = response.get("Messages", [])
            received.extend(batch)
            if not batch:
                break
        for message in received:
            try:
                with self.env.cr.savepoint():
                    envelope = json.loads(message["Body"])
                    data = sanitize_event(
                        envelope,
                        self.account_id,
                        self.topic_arn,
                        {self.configuration_set, self.engagement_configuration_set},
                    )
                    self.env["mail.ses.event"].sudo()._ingest(self, data)
                # SQS is at-least-once: only delete after the ORM transaction
                # commits. If deletion fails, the unique event key handles replay.
                queue_url, receipt = self.queue_url, message["ReceiptHandle"]
                registry = self.env.registry

                def acknowledge(
                    url=queue_url,
                    handle=receipt,
                    sqs=client,
                    key=data["event_key"],
                    connection_id=self.id,
                    db_registry=registry,
                ):
                    try:
                        # An outer savepoint can roll back after this callback
                        # was registered. Verify durability on a fresh cursor.
                        with db_registry.cursor() as cr:
                            env = api.Environment(cr, SUPERUSER_ID, {})
                            if not env["mail.ses.event"].search_count(
                                [
                                    ("connection_id", "=", connection_id),
                                    ("event_key", "=", key),
                                ]
                            ):
                                return
                        sqs.delete_message(QueueUrl=url, ReceiptHandle=handle)
                    except Exception:
                        _logger.warning(
                            "SES event acknowledgement failed; SQS will redeliver"
                        )

                self.env.cr.postcommit.add(acknowledge)
            except (ValueError, KeyError, TypeError):
                _logger.warning("Invalid SES event left on SQS for dead-letter redrive")
        sent = [int(m.get("Attributes", {}).get("SentTimestamp", 0)) for m in received]
        self.oldest_event_seconds = (
            max(0, int(fields.Datetime.now().timestamp() - min(sent) / 1000))
            if sent
            else 0
        )
        self.last_poll = fields.Datetime.now()

    def _sync_suppressions(self):
        self.ensure_one()
        client = self._client("sesv2")
        for page in client.get_paginator("list_suppressed_destinations").paginate():
            for item in page.get("SuppressedDestinationSummaries", []):
                self.env["mail.ses.suppression"].sudo()._block(
                    self,
                    item["EmailAddress"],
                    item["Reason"],
                    source="AWS suppression list",
                )
        # Deliberately do not clear local complaints absent from AWS's list.
        self.last_sync = fields.Datetime.now()

    @api.model
    def _cron_poll(self):
        for connection in self.search([("enabled", "=", True)]):
            try:
                with self.env.cr.savepoint():
                    connection._poll()
                    self.env["mail.ses.event"]._reconcile(connection)
            except Exception:
                _logger.exception("SES poll failed for connection %s", connection.id)

    @api.model
    def _cron_sync(self):
        for connection in self.search([("enabled", "=", True)]):
            try:
                with self.env.cr.savepoint():
                    connection._sync_suppressions()
            except Exception:
                _logger.warning(
                    "SES suppression sync failed for connection %s", connection.id
                )
