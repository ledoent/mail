Provision one event stack per Odoo database/environment using
`infrastructure/ses-events.json`. Supply a unique NamePrefix and an
existing operational AlarmTopicArn. Attach the returned RuntimePolicyArn
to the runtime AWS identity. Credentials use boto3's standard runtime
chain; they are not stored in Odoo. Existing SES SMTP credentials remain
unchanged.

The stack creates delivery-only and engagement configuration sets, SNS,
SQS, dead-letter queues and alarms. It does not change sender
identities, DNS, default identity configuration sets, or feedback
forwarding. Use a verified identity in the same region. The runtime
policy's account-level SES operations use Resource "\*" because
suppression APIs do not support address resource ARNs.

In SES mail \> Connections, enter the account, region, sender identity,
configuration set names, topic ARN and queue URLs from the stack
outputs. Choose a mail administrator with the SES mail administrator
group. Use Check configuration, then Verify and enable. Select this
connection on the intended outgoing SMTP server. Enabling a connection
starts polling; attaching it to a server activates the SMTP guard. Do
not attach an unverified connection to a busy server.

Keep both bounce and complaint suppression enabled. Keep existing email
feedback forwarding until a controlled delivery/bounce/complaint test
has passed. Monitoring must include both dead-letter queues. An event
queue outage exceeding five minutes, stale suppression import or a
nonempty processing dead-letter queue holds sends. Inspect, correct and
redrive failed events; do not discard them to clear the hold.

Engagement uses a separate configuration set. Disable engagement at
connection level for delivery-only operation. Authentication messages
use delivery-only; links containing credentials are marked ses:no-track.
Opens/clicks are observations, not proof of human reading. Some
providers do not report complaints to SES.
