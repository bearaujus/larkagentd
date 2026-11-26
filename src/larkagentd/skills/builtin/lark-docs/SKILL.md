---
name: lark-docs
description: Draft a native Lark Doc or edit a shared Doc.
---

For a new document, use a short title and organize Markdown into sections by reader need.
Cite or link the specific sources that support decisions and facts. Mark unassigned owners,
dates and unverifiable claims. Use checklist items for actions the user asks to track.

Use lark_file_create with kind docx. For existing work, read the shared link and use
lark_file_edit with append or exact text replace. Choose the smallest edit that meets the
request and preserve surrounding text. The tool checks the bot's Edit access and the
group's grant. When Lark hides the collaborator list, the tool can still request
confirmation for an organization-shared Doc the bot can edit. Wait whenever the tool
requests confirmation. Use the tool's note if a check or write fails. Share the resulting URL
after the tool confirms the operation.
