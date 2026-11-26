# Decisions

- 2026-11-26. Keep a single public assistant implementation. Install private connectors
  through a registered deployment extension and use a fresh deployment database.
- 2026-11-26. Model turns and their replies belong to a single Lark group thread. DMs
  provide account setup, dashboard access and usage through Gateway commands.
- 2026-11-26. Workspace and group Markdown are curated. Learned group facts carry
  independently rechecked message sources and can be retired.
- 2026-11-26. Recheck group and bot permissions before using an existing file. For a new
  file, verify the requester owns it and the group and bot can manage it. Keep idempotency
  receipts across retries and bind confirmations to the original requester.

- 2026-11-26. Publish a single initial schema on PostgreSQL. Account setup requires
  current Lark membership in an admitted group. Govern workspace pause independently
  of the dashboard sign-in and group access policies.
