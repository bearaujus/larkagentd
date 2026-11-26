# Architecture

## Event to reply

The Gateway verifies the Lark mention, sender and registered chat. It applies admission
and token governance before enqueueing and again before starting a queued run. It binds the
Hermes session to the originator, chat, thread and current credential version. The plugin
loads the authenticated tool catalog from the tool service at startup and forwards calls
with the Hermes session. Callers cannot supply a user, group or credential version.
The tool registry and the group's allowed tool list both apply. The Gateway edits one
compact activity card with fixed tool labels, elapsed time and outcomes. A recovered turn
keeps its original timer. The final reply contains the response, any confirmation control,
and a collapsed usage breakdown. Interrupted runs report the specific outcome in the same
layout. A single rich post serves as the fallback.
File downloads use the attachment zone in the final rich-text reply. Lark uses separate
file messages when a response also needs an interactive confirmation, and account setup
stays in direct chat.
A revoked or erased reply target does not turn into a chat-wide post.

The existing usage ledger records settled token usage by model, rate and quota tier.
Administrators can change the model and supported reasoning effort and set personal and
tier-wide token limits. Limit changes affect queued admission. A running turn can finish
above a cap. USD estimates require known token counts and rates and expose coverage.

## Account setup and dashboard login

A direct message runs Gateway commands for account setup, dashboard login or
personal usage. A verified interaction in an admitted group anchors setup. The
worker checks the sender's current membership through Lark's group member API
before opening setup and starting provider steps. Final credential confirmation
rechecks membership after the provider call. Secure setup cards and one-time
dashboard tokens are delivered to the caller's direct chat.

Workspace admins and managers use a verified Lark identity for dashboard login.
A group owner or administrator can obtain a dashboard session scoped to their
registered group. Governance can pause new group turns and account setup across
the workspace. Dashboard access remains available during the pause.

## Knowledge

Workspace Markdown and attached MCP resources apply to all admitted groups. The group
Markdown library applies to that group. The administrator supplies MCP endpoints or
trusted stdio command paths in deployment configuration. The dashboard attaches a named
resource URI. Retrieval calls only the resource read API. Managed groups can search
visible Lark history on request. History queries can start at the oldest visible message,
continue with Lark's cursor, and cite provider-supplied conversation jump links.

With collective memory enabled, the model can nominate one durable fact before it
finishes a group turn. The Gateway rereads the cited group messages and saves the fact
after the turn succeeds under the same group policy. Facts are searchable without an
embedding service. Messages outside assistant turns are searched on request. Learned records
link to messages re-read by ID in the originating group. A later retrieval compares their
current sanitized content to the recorded revision and omits records with missing, moved
or changed sources. Current Lark group owners and admins can retire records in their
scoped dashboard. Workspace governance and group registration use separate admin access.

## Lark files and confirmation

File tools use an app-only Lark CLI session. The Gateway records links supplied by the
requester. A follow-up can also read a link from the requester's earlier live thread message.
For a file shared earlier by a group member, the assistant finds the link in live
group history. The tool service rereads the posting before opening it. History
lookups supply read references only. Each read checks bot access and group visibility. For reads, it also
accepts organization-only link sharing when both the document and the originating group are
verified as internal to the same tenant through the receiving app. Editing requires effective
bot Edit and a group Edit grant when Lark exposes the collaborator list. When Lark withholds
the list for lack of Share permission, confirmed Docs edits can use the bot's Edit grant
if the same organization-link check verifies visibility for the group. The requester also
confirms edits that use a verified earlier link. The tool service rechecks access before
it executes the edit. Creation uses
the same app as the Gateway, transfers ownership to the requester, retains bot Manage,
and grants group Manage. The service releases a link after reading back ownership and grants.

Write receipts persist before a create or edit. A duplicate call returns a verified
receipt when available. If a response or permission transfer is uncertain, inspect the
recorded artifact in Lark before another attempt. Confirmations bind the action to its
requester, chat and thread. The continuation consumes the approval, rechecks access and,
for a row deletion, verifies the same workbook revision and matching row positions.

## Extensions

Install an operator-controlled distribution with an entry point under
`larkagentd.extensions`. Set `LARKAGENTD_EXTENSION` to its name on Gateway and tools.
The factory provides `authentication()` and `tools(store)`. The first returns a
credential provider or `None`. The second returns a mapping of names to
`larkagentd.toolservice.tools.Tool` definitions, including capability and risk. The caller
context exposes the server-resolved identity and version. Only explicit tool definitions
are registered. Each executor owns validation, upstream rights and result limits.
Restart Hermes after changing the registered catalog.

## Dashboard knowledge and connector metadata

Workspace and group sources have stable IDs. An editor renames a source through its ID. Learned facts retain their source message revisions. The dashboard rechecks the messages before displaying text or links, shows temporary outages separately, and rechecks the active session and group policy after the external read. A workspace admin needs current group membership to view learned conversation text.

Account providers return choices with `id` and `label`. Credentials include an expiry timestamp in `expires` and a display time in `expiration`. The provider verifies the upstream identity and owns its credential format. Extension tools can supply a human-readable label, credential authority, execution project, timeout and activity category. Register separate group-grantable capabilities for independently shared resources.

## Group replies

For a live group turn, the Gateway looks up display names for the requester and observed thread participants using the bot’s Lark contact permissions. Conversation history can retain a display name with the speaker’s opaque key. Missing names fall back to ordinary conversation roles. An anonymous label in a forwarded conversation or historical search is local to that transcript.

The Gateway attaches the verified requester’s mention to the first final answer in the original thread. It uses the native rich-post, card, or plain-text syntax and retries without a mention if Lark refuses it. The model writes the answer using the built-in `lark-replies` skill. [Lark message formats](https://open.larksuite.com/document/server-docs/im-v1/faq)
