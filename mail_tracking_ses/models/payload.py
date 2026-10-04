# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

"""Small, pure helpers: no AWS calls or Odoo ORM side effects."""

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from lxml import html

EVENTS = {
    "Send",
    "Delivery",
    "DeliveryDelay",
    "Bounce",
    "Complaint",
    "Reject",
    "Rendering Failure",
    "Open",
    "Click",
}


def safe_url(value):
    """Never persist query strings, fragments, or embedded credentials."""
    try:
        url = urlsplit(value or "")
        if url.scheme not in {"http", "https"} or not url.hostname:
            return ""
        # Paths can themselves contain bearer tokens. Store only the origin.
        return urlunsplit((url.scheme, url.hostname, "", "", ""))
    except ValueError:
        return ""


def protect_links(body):
    if not body:
        return body
    root = html.fragment_fromstring(str(body), create_parent="div")
    for node in root.xpath(".//a[@href]"):
        href = node.get("href", "").lower()
        if any(
            word in href
            for word in ("token", "password", "signup", "reset", "auth", "signature")
        ):
            node.set("ses:no-track", "")
    return (root.text or "") + "".join(
        html.tostring(n, encoding="unicode") for n in root
    )


def sanitize_event(envelope, account_id, topic_arn, configuration_sets):
    if envelope.get("Type") != "Notification" or envelope.get("TopicArn") != topic_arn:
        raise ValueError("Unexpected SNS source")
    payload = json.loads(envelope["Message"])
    mail = payload.get("mail", {})
    tags = mail.get("tags", {})
    if mail.get("sendingAccountId") != account_id:
        raise ValueError("Unexpected SES account")
    if tags.get("ses:configuration-set", [None])[0] not in configuration_sets:
        raise ValueError("Unexpected configuration set")
    kind = payload.get("eventType")
    if kind not in EVENTS:
        raise ValueError("Unsupported event type")
    data = payload.get(
        {"Rendering Failure": "failure"}.get(kind, kind[:1].lower() + kind[1:]), {}
    )
    recipients = mail.get("destination", [])
    if kind == "Bounce":
        recipients = [r["emailAddress"] for r in data.get("bouncedRecipients", [])]
    elif kind == "Complaint":
        recipients = [r["emailAddress"] for r in data.get("complainedRecipients", [])]
    elif kind == "Delivery":
        recipients = data.get("recipients", recipients)
    safe = {
        "kind": kind,
        "ses_id": mail.get("messageId", ""),
        "attempt": tags.get("odoo_attempt", [""])[0],
        "time": data.get("timestamp", mail.get("timestamp")),
        "recipients": recipients,
        "bounce_type": data.get("bounceType", ""),
        "subtype": data.get("bounceSubType", data.get("complaintFeedbackType", "")),
        "url": safe_url(data.get("link")),
        "rfc_message_id": mail.get("commonHeaders", {}).get("messageId", ""),
    }
    # SNS duplicate publications can have distinct envelope IDs.
    canonical = json.dumps(safe, sort_keys=True, separators=(",", ":"))
    safe["event_key"] = hashlib.sha256(canonical.encode()).hexdigest()
    return safe
