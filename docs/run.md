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
3. Fill the model provider settings. Set available model names and per-model reasoning
   options. Start the stack and verify Gateway, tools and Hermes readiness before any
   message test. The empty LarkAgentD database initializes at revision `0001`.
4. Register two dedicated test groups. Try a group question before opening a DM. Verify
   the thread response, permissions on both groups, and system-only DM handling.
   With a credential provider enabled, mention the bot with `/setup` in an admitted group,
   then send `/setup` in a DM. Remove the test user's group membership and verify setup
   and any in-flight credential confirmation are refused. Verify workspace and group
   dashboard login use their respective administrator roles.
5. Create and edit each native file type in the test group. Check actual owner, group
   Manage, bot Manage and final content. Exercise group View with independently granted
   bot Edit, group Edit with bot View, wrong group, removed sharing, and a failed transfer.
6. Pause the workspace in Governance or lower a token limit while a model run is queued. Confirm the
   dashboard shows model, count coverage, the estimate at the original rate and the
   saved limits. Confirm the configured Hermes provider honors selected reasoning effort.

[App configuration](https://open.larksuite.com/app) and
[Lark permission scopes](https://open.larkoffice.com/document/ukTMukTMukTM/uYTM5UjL2ETO14iNxkTN/scope-list)
are the references for the actual scopes exposed by your Lark app.

## Database baseline

Use a fresh database with this unreleased baseline. An earlier disposable checkout
with pre-release migration history needs a new database. The baseline does not
automatically reset a database.

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
