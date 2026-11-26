"""A table preview as a Lark card: a paged table, notes, and rows to copy.

Lark limits a card message to about 30 KB, a table to 50 columns and 10 rows a page, and a
card to a few tables. The card is sized to fit: full values first, then shortened values,
then fewer rows. The copy block is added only when everything still fits. None means the
table cannot be laid out, and the caller sends the text layout instead.
"""

import csv
import io
import json
from collections import namedtuple

from .render import cells, shorten

PAGE_ROWS = 10
MAX_COLUMNS = 50
MAX_TABLES = 3
# Measured on the request body, where the card is a string inside JSON, so every quote
# costs two bytes. Lark's limit is about 30,000.
BUDGET = 28_000
CELL_CAPS = (None, 60, 30)
ANSWER_CHARS = 2000

# One table to show. `all_columns` is the real column count before the 50 column cut.
Part = namedtuple("Part", "header rows limit requested all_columns")


def parse(text):
    """(header, rows) of a psql-style table, or None when its rows do not line up."""
    lines = [line for line in text.splitlines() if line.startswith("|")]
    if len(lines) < 3:
        return None
    header = cells(lines[0])
    rows = [cells(line) for line in lines[2:]]
    if not header or any(len(row) != len(header) for row in rows):
        return None
    return header, rows


def make_part(preview):
    table = parse(preview["text"])
    if table is None:
        return None
    header, rows = table
    columns = len(header)
    return Part(
        header[:MAX_COLUMNS],
        [row[:MAX_COLUMNS] for row in rows],
        preview.get("limit"),
        preview.get("requested"),
        columns,
    )


def clip(value, cap):
    if cap is None or len(value) <= cap:
        return value
    return value[: cap - 1] + "\u2026"


def table_element(part, count, cap):
    names = [f"c{i}" for i in range(len(part.header))]
    return {
        "tag": "table",
        "page_size": PAGE_ROWS,
        "columns": [
            {"name": name, "display_name": title, "data_type": "text"}
            for name, title in zip(names, part.header)
        ],
        "rows": [
            {name: clip(value, cap) for name, value in zip(names, row)}
            for row in part.rows[:count]
        ],
    }


def copy_element(part):
    """The rows as CSV in a code block, whose copy icon is the only copy control a card has."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(part.header)
    writer.writerows(part.rows)
    body = out.getvalue().rstrip("\n").replace("```", "'''")
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {"title": {"tag": "plain_text", "content": "Copy rows as CSV"}},
        "elements": [{"tag": "markdown", "content": f"```\n{body}\n```"}],
    }


def size(card):
    """Bytes of the request body that carries the card, close enough to stay under the cap."""
    return len(json.dumps(json.dumps(card, ensure_ascii=False)).encode())


def notes(parts, shown, cap, copy_left_out):
    lines = []
    for part, count in zip(parts, shown):
        if part.limit and part.requested and part.requested > part.limit:
            lines.append(
                f"Preview limit: {part.limit} rows. You asked for {part.requested}, so "
                f"only the first {part.limit} are shown."
            )
        if count < len(part.rows):
            lines.append(
                f"{len(part.rows) - count} more rows were left out to fit Lark's card "
                "size limit. Ask for specific columns to see more rows."
            )
        if part.all_columns > MAX_COLUMNS:
            lines.append(
                f"Only the first {MAX_COLUMNS} of {part.all_columns} columns are shown. "
                "Ask for specific columns to see the rest."
            )
    if cap is not None:
        lines.append("Long values are shortened in the table.")
    if copy_left_out:
        lines.append("The result is too large to offer as copyable text here.")
    return lines


def build(answer, parts, shown, cap, copies=False):
    """The card; `copies` is True (add copy blocks), False (none) or "left out" (say so)."""
    elements = [{"tag": "markdown", "content": shorten(answer, ANSWER_CHARS)}]
    for part, count in zip(parts, shown):
        elements.append(table_element(part, count, cap))
    lines = notes(parts, shown, cap, copies == "left out")
    if lines:
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
    if copies is True:
        elements.extend(copy_element(part) for part in parts)
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "Table preview"}},
        "body": {"elements": elements},
    }


def preview_card(answer, previews, budget=BUDGET):
    """The card as a JSON string, or None when the previews cannot be laid out as tables."""
    if not previews or len(previews) > MAX_TABLES:
        return None
    parts = [make_part(preview) for preview in previews]
    if None in parts:
        return None

    shown = [len(part.rows) for part in parts]
    for cap in CELL_CAPS:
        if size(build(answer, parts, shown, cap)) <= budget:
            break
    else:
        while size(build(answer, parts, shown, cap)) > budget:
            longest = max(range(len(shown)), key=shown.__getitem__)
            if shown[longest] <= 1:
                return None
            shown[longest] -= 1

    # Copy blocks carry the full values, so they are offered only when nothing was cut.
    whole = cap is None and all(n == len(p.rows) for n, p in zip(shown, parts))
    card = build(answer, parts, shown, cap, True) if whole else None
    if card is None or size(card) > budget:
        card = build(answer, parts, shown, cap, "left out")
    return json.dumps(card, ensure_ascii=False)
