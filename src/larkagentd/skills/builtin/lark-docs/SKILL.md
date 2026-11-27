---
name: lark-docs
description: Draft or edit a Lark Doc and help users open its link in Lark.
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

If asked why a Doc link opens in a browser on desktop, explain that Lark uses the
user's link preference. To open Docs inside Lark, use the desktop profile menu
and select Settings -> Efficiency -> Open links -> In Lark navigation bar.
Keep using the verified document URL. [Lark desktop Docs settings](https://www.larksuite.com/hc/en-US/articles/360038195414-set-the-default-method-for-opening-documents)
