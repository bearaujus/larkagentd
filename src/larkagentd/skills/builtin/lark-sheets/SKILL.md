---
name: lark-sheets
description: Build a native spreadsheet or update rows and cells in a group-shared Sheet.
---

Use a header row and one record per row. Keep numbers numeric and include units in the
header. Supply a rectangular JSON matrix to lark_file_create with kind sheets.

For edits, inspect the workbook’s tabs and confirm the intended tab or ask if ambiguous.
Read the relevant range before deciding which cells to update. To delete matching rows,
supply an exact match and a single-column range below the header row, for example C2:C80.
Explain the match and wait for the requester’s confirmation. The tool checks the workbook
revision before applying the change.
