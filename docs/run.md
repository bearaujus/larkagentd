# Deployment and acceptance

1. Start with a clean database and a test Lark app. Configure bot message events through
   WebSocket, group message history, message replies, interactive-card callbacks,
   chat details and any required directory fields in Developer Console. For an account
   provider, enable listing group members through `im.v1.chatMembers.get`.
   [Lark group member API scopes](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/mcp_integration/mcp_installation)
2. For authoring, grant the app read/write scopes for the Docs, Sheets and Slides surfaces
   you enable, and Drive metadata, collaborator list/grant, permission check and ownership
   transfer. Use the same app for receiving group requests and native authoring. Scopes
   grant the API surface. Individual files also require their Drive grants.
   For attached knowledge sources, enable wiki node listing and lookup, current and classic
   document reads, and Drive permission and collaborator reads for the Tool Service bot.
   Share the selected space or pages with that bot. To quote a page, the bot must also be
   able to verify the receiving group's view grant or applicable organization sharing.
3. Fill the model provider settings. Set available model names and per-model reasoning
   options. Start the stack and verify Gateway, tools and Hermes readiness before any
   message test. The database migrates to the current Alembic revision.
4. Register two dedicated test groups. Try a group question before opening a DM. Verify
   the thread response, permissions on both groups, and system-only DM handling.
   With a credential provider enabled, mention the bot with `/setup` in an admitted group,
   then send `/setup` in a DM. Remove the test user's group membership and verify setup
   and any in-flight credential confirmation are refused. Verify workspace and group
   dashboard login use their respective administrator roles. With collective memory enabled, share
   a durable test decision as a person and look for it under Saved from conversations after the reply. Ask for
   the first visible group message and for the link to the decision to exercise live history.
5. Create and edit each native file type in the test group. Check actual owner, group
   Manage, bot Manage and final content. Exercise group View with independently granted
   bot Edit, group Edit with bot View, wrong group, removed sharing, and a failed transfer.
   Read an organization-link document in an internal group when the bot has View and Lark
   withholds the collaborator list. Retry in the same thread without reposting the link.
   This read path uses Drive public permission settings and chat details with the receiving app.
6. Pause the workspace in Governance or lower a token limit while a model run is queued. Confirm the
   dashboard shows model, count coverage, the estimate at the original rate and the
   saved limits. Confirm the configured Hermes provider honors selected reasoning effort.

[App configuration](https://open.larksuite.com/app) and
[Lark permission scopes](https://open.larkoffice.com/document/ukTMukTMukTM/uYTM5UjL2ETO14iNxkTN/scope-list)
are the references for the actual scopes exposed by your Lark app.

## Planned restarts

`make run` builds first, then waits for active turns and queued replies before stopping
the Gateway and Hermes. During the wait, new turns stay queued for the replacement
Gateway. If existing work takes more than five minutes, the restart is cancelled and the
current stack resumes accepting turns.

## Configure who can trigger a reply

Leave `LARK_ALLOWED_SENDERS` empty to accept people only. A nonempty list
accepts exactly the entries you give it. For example, `LARK_ALLOWED_SENDERS=user,cli_example`
accepts people and that specific Lark app. Use app IDs from the Lark developer console.

To receive an app's mention, the receiving app needs the **Receive mentions from other
bots and users** permission (`im:message.group_at_msg.include_bot:readonly`), the
`im.message.receive_v1` subscription, and access to read the message back.
[Lark receive event](https://open.larkenterprise.com/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive)

App requests require an explicit mention in a registered group with All users access.
The sending app uses its own free-tier quota and can call the group's permitted read
tools. Human sign-in, confirmation actions and workspace management are for people.
Set the list in the Gateway's environment and restart it after changes.

## Diagnose Lark file access

Find the `tool.called` event in the tool service log. Its `job_id` matches the Gateway
request. For a permission refusal, `stage` identifies the check that stopped the action.
For example, `stage=collaborator_list access=edit bot_access=granted provider_code=1063004`
means Lark verified bot Edit and withheld the collaborator list. `access_basis=bot_edit`
marks a Docs edit that can proceed with confirmation and verified organization-link visibility.
`stage=confirmation_requested` records a queued confirmation, `stage=write_request`
identifies a failed write call, and `stage=write_verified` records a completed edit with
readback. The events omit document content and Lark resource identifiers.

For Slides, `slide_validation` means the payload failed before Lark was called.
`create_request` with provider code `99991672` means Lark refused creation. Check
the bot's `slides:presentation:create` scope in Developer Console. Adding pages uses
`slides:presentation:update` or `slides:presentation:write_only`. Readback uses
`slides:presentation:read`. Release the app changes for them to take effect.

## Dashboard addresses

Set `DASHBOARD_PUBLIC_URL` to the primary browser origin. To keep localhost and
an HTTPS tunnel available together, add the second exact origin to
`DASHBOARD_ADDITIONAL_ORIGINS` (comma-separated). Use origins without paths.
HTTP is supported on loopback. The proxy should preserve the browser's Host header.

## Database baseline

Revision `0001` creates the current unreleased schema. Use a fresh database
when an earlier development checkout has a different schema.

### Local pgvector

`make run` starts PostgreSQL 16 with pgvector. `make spawn-db` enables the
`vector` extension and creates the Group Brain collection if it does not exist.
It works with both new databases and existing Compose volumes. Back up an
existing database before switching images; keep the named volume.

To enable semantic recall, set `GROUP_BRAIN_EMBEDDING_BASE_URL` and
`GROUP_BRAIN_EMBEDDING_API_KEY` in `.env` for an approved OpenAI-compatible
embeddings endpoint. Set `GROUP_BRAIN_EMBEDDING_MODEL` and
`GROUP_BRAIN_EMBEDDING_DIMENSIONS` to match the provider. Without an embedding
endpoint, recall still uses keyword search and live lookups.

The default collection is `group_brain_v1` with 1024 dimensions. If you change
the embedding model or dimensions after creating a collection, give
`GROUP_BRAIN_COLLECTION` a new name before running `make run`. Provisioning
does not alter or delete existing collections.

With `make run LOCAL_DATABASE=false`, the operator must enable pgvector and
provision the matching collection in the external `DATABASE_URL` database.

## Knowledge and connector configuration

`KNOWLEDGE_MCP_SERVERS` is a JSON object mapping a name to either
`{"url":"https://knowledge.example.com/mcp"}` or
`{"argv":["/opt/connectors/knowledge-server","--stdio"]}`.
A workspace administrator attaches resources by exact URI in the dashboard. Stdio executables
must exist in the tool service image or an operator-managed mount. The tool service reads text
resources through Streamable HTTP or stdio. Use a dedicated read-only resource server.
Install the `mcp` extra outside Docker when these integrations are configured.
[MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

The optional GitLab reader needs a host and scoped read API token. Enable its read
tools only for groups authorized to see the token's resources. Similarly, give the optional
Google reader a service account limited to files intended for its registered groups.
The Brave Search key enables consent-based public search. An unset key leaves it unavailable.

For group history, use the Lark message links returned by the CLI. Confirm exact-message
jump and preview behavior with both fixture groups in the test tenant. When the provider
returns no link, LarkAgentD reports the matching excerpt and timestamp with a link unavailable
status. [Lark CLI message guidance](https://github.com/larksuite/cli/blob/main/skills/lark-im/SKILL.md)
