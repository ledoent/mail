Deployment gate: verify AWS mailbox-simulator events and a
controlled-inbox reply against the installed AWS stack before enabling a
production server. Unit tests and CloudFormation validation do not
substitute for this end-to-end check.

Optional follow-ups: SES API transport, campaign-specific statistics and
consent, BCC UI, customer-role recipient routing, and attachment
preview. None is required for the transactional SMTP integration.
