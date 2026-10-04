The three addons are independent of general contact-role routing and
campaign UI. OCA mail_tracking is pinned in repos.yaml. Its
mail_tracking_mass_mailing bridge is shipped because it auto-installs
when Email Marketing is present; no custom campaign feature is
introduced by these addons.

Transactional flow: mail.mail is queued, then the scheduler prepares
individual SMTP envelopes. Before SMTP IO an independently owned cursor
commits an opaque recipient attempt. It never commits the caller's
transaction. Accepted copies are skipped on retry; ambiguous acceptance
remains held for explicit review. This is not an exactly-once guarantee:
an administrator-authorized retry of an uncertain attempt can deliver a
second copy. Each such retry has a new token and preserves previous
attempt history. Normal Odoo transaction tests use their rollback
cursor; exercise the owned-cursor path separately before production.

SES tags correlate events to attempts. SQS events are source-validated,
sanitized, deduplicated and persisted. A postcommit callback verifies
durable persistence on a fresh cursor before acknowledging SQS.
Redelivery is safe. Unknown correlations remain available for
reconciliation, while complaints and permanent bounces still block their
explicitly reported addresses. Do not map messages by address alone.

Tracking summaries never allow an open/click to overwrite a complaint or
failure. The message action checks the caller's access before
constructing a transient recipient view. Raw operational models require
administrator access. BCC is not exposed by the recipient view; this
release does not provide a BCC composer.

Sanitized processed events expire after 90 days; accepted non-complaint
journals after 365 days. Unresolved events, ambiguous sends and
complaint records remain for review. Click URLs retain only the origin.
IP addresses, user agents, raw payloads and message bodies are not
copied into the event inbox.

Run Odoo with --test-enable and --test-tags
/mail_composer_cc,/mail_composer_cc_account,/mail_tracking_ses against
an isolated local database. Disable outgoing servers and cron workers
before and after module installation. AWS calls and SMTP are mocked in
the module tests; a captured MIME test validates actual envelope
generation.

The owned-cursor smoke test must run outside Odoo's transaction-test
harness. Use an isolated database named erp_ses_test, with all cron
workers and outgoing servers disabled, then run:

    odoo shell -d erp_ses_test --no-http --max-cron-threads=0 < mail_tracking_ses/tests/owned_cursor_smoke.py

Supply your local database connection and addons-path options as needed.
This script makes no AWS or SMTP calls and removes its test records.
