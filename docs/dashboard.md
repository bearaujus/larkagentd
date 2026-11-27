# Dashboard

The dashboard screenshots use synthetic data. They can be regenerated with
`npm --prefix frontend run build` and `npm --prefix frontend run screenshots`.

## Overview

![Overview showing recorded tokens, model sessions, tool activity, and coverage](screenshots/overview.png)

Overview shows estimated spend, pricing coverage, active people and groups, token usage, and tool success for the selected period. Adoption counts distinct requesters and groups with recorded model sessions. Tool success uses finished calls. The daily bars cover the displayed dates. Spend by model ranks the three highest priced totals across the full filtered period and groups the rest under Other models. **Expand report** opens a recording view on supported browsers and keeps the data source visible.

When model sessions have no USD estimates, Overview shows recorded activity and links to model rates in Governance. Rate changes apply to new sessions. Use **Filters** to narrow the overview or **Reporting coverage** to inspect missing usage and scan estimates.

## Groups

![Registered groups and their access modes](screenshots/groups.png)

## Group knowledge

![Added group sources and a learned decision with its message evidence](screenshots/group-knowledge.png)

Open a registered group in **Knowledge** to see **Saved from conversations**. The assistant can save a useful fact after a successful reply to a group member. Each saved fact shows the message that supports it and how long it can be used. **Retire fact** stops the assistant from using the saved entry. The original message remains in Lark and can still appear in conversation recall. Workspace admins who belong to the group can review source-backed corrections in the group's **Learning settings**.

Workspace admins can open **Knowledge > Add source** to attach a Lark Wiki or Doc for registered groups. Use **View knowledge for** to switch between the workspace and a registered group. Group admins can add sources on their own group dashboard. Paste a wiki space settings link to include the entire space, a wiki page link to include its branch, or a current or classic Doc link for one document. The preview shows which pages the source includes. Leave **Custom name** blank to follow the document, page or space title in Lark. Enter a name to keep your own label.

**Workspace sources** are references available in registered groups. **Group sources** are references added for just the selected group. The **Saved from conversations** section shows source-backed facts the assistant learned from that group's human messages. To check the distinction in Lark, ask the assistant to search workspace sources only, group sources only, or facts saved from conversations.

The Tool Service bot needs read access to the space and documents. It indexes supported pages on attachment and every 15 minutes. **Refresh now** requests an earlier sync. A partial sync retries and keeps the previous inventory until traversal completes. Search checks the receiving group's Lark access and the page version before quoting an excerpt. Removing a source drops its local index and leaves the Lark pages intact.

## Governance

![Workspace pause, model selection, and token governance](screenshots/governance.png)
