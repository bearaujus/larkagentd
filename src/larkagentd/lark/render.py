"""Lark message bodies for answers: markdown as a post, with plain text as the fallback."""

import json
import re

MAX_CHARS = 8000
MAX_PARTS = 5  # messages one answer may take
PART_ROOM = 12  # kept free in a part for its "(2/5)" line
FENCE = re.compile(r"^\s*(```|~~~)")


def open_fence(text):
    """The marker of a code fence the text opens and never closes, or None."""
    opener = None
    for line in text.splitlines():
        match = FENCE.match(line)
        if match:
            opener = None if opener else match.group(1)
    return opener


def close_fence(text):
    opener = open_fence(text)
    return f"{text}\n{opener}" if opener else text


def final_post(text, footer):
    """Bound the plain or rich-post fallback to one message and keep usage outside code."""
    room = MAX_CHARS - len(footer) - 160
    text = close_fence(shorten(text, room))
    return f"{text}\n\n{footer}" if footer else text


def shorten(text, limit=MAX_CHARS):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0] or text[:limit]
    # A cut inside a code block must not leave the block open: the notice, and anything sent
    # after the answer, would otherwise be shown inside it.
    opener = open_fence(cut)
    if opener:
        cut += "\n" + opener
    return cut + "\n\n(Shortened: the full answer is too long to send here.)"


ANSWER_MIN = 1000
WIDE = 120
NOTE_ROOM = 120


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def records(block, width=WIDE):
    """A wide psql-style table as one `column: value` record per row.

    None when the table is narrow, or when a row does not split into as many cells as the
    header (a value containing the separator), so the raw table is shown instead.
    """
    lines = block.splitlines()
    table = [x for x in lines if x.startswith("|")]
    if len(table) < 3 or max(len(x) for x in lines) <= width:
        return None
    header = cells(table[0])
    rows = [cells(x) for x in table[2:]]
    if any(len(row) != len(header) for row in rows):
        return None
    return [
        "\n".join([f"-- row {i} --"] + [f"{h}: {v}" for h, v in zip(header, row)])
        for i, row in enumerate(rows, 1)
    ]


def cut_lines(text, share, note):
    lines, size = [], 0
    for line in text.splitlines():
        if size + len(line) + 1 > share:
            lines.append(note)
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def fit(block, share):
    """One block cut to `share` characters: whole rows for a wide table, else whole lines."""
    block = block.replace("```", "'''")
    recs = records(block)
    if recs is None:
        return cut_lines(block, share, "(Rows cut to fit one message.)")
    shown, size = [], 0
    for rec in recs:
        if shown and size + len(rec) + 2 > share - NOTE_ROOM:
            break
        shown.append(rec)
        size += len(rec) + 2
    body = "\n\n".join(shown)
    if len(shown) < len(recs):
        body += f"\n\n(Showing {len(shown)} of {len(recs)} rows. Ask for specific columns to see more.)"
    # A single row can be wider than the room: cut it by lines rather than overflow.
    return cut_lines(body, share, "(Row cut to fit one message.)")


def joined_blocks(blocks, limit=MAX_CHARS):
    """The blocks as fenced code blocks, each fitted to its share of the message."""
    share = (limit - ANSWER_MIN) // len(blocks) - 20
    return "\n\n".join(f"```\n{fit(block, share)}\n```" for block in blocks)


def answer_room(blocks, limit=MAX_CHARS):
    """How many characters of the answer fit in the message next to these blocks."""
    if not blocks:
        return limit
    return max(limit - len(joined_blocks(blocks, limit)) - 2, ANSWER_MIN)


def with_blocks(text, blocks, limit=MAX_CHARS):
    """The answer, then each block as a fenced code block, in one message.

    Blocks are fitted to their share and always leave room for the answer.
    """
    if not blocks:
        # Several messages carry it (split_message), so the cap is what they hold together.
        return shorten(text, limit * MAX_PARTS - 4000)
    return (
        shorten(text, answer_room(blocks, limit))
        + "\n\n"
        + joined_blocks(blocks, limit)
    )


def is_separator(line):
    """A markdown table's separator row, such as | --- | :-: |."""
    line = line.strip()
    return line.startswith("|") and "-" in line and set(line) <= set("|-: ")


def scan(body, opener, head):
    """The state after these lines: the line that opened a fence still open (or None), and the
    header and separator rows of a table the lines end in (or None)."""
    previous = None
    for line in body.splitlines():
        if FENCE.match(line):
            opener = None if opener else line.strip()
            head = previous = None
        elif opener:
            continue
        elif line.strip().startswith("|"):
            if is_separator(line) and previous is not None:
                head = [previous, line.rstrip()]
            previous = line.rstrip()
        else:
            head = previous = None
    return opener, head


def split_message(text, limit=MAX_CHARS):
    """The answer as the parts to send, in order, each at most `limit` characters.

    Cut at a line (else a space), as Hermes's own Lark adapter does. A cut inside a code block
    closes it and the next part opens it again with the same language; a cut inside a table
    repeats its header and separator rows, which Hermes does not, so every part shows as a
    table. Parts of a split answer end with `(2/3)`.
    """
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    opener = head = None
    while rest:
        if head and len("\n".join(head)) > limit // 4:
            head = None
        prefix = (opener + "\n" if opener else "") + (
            "\n".join(head) + "\n" if head else ""
        )
        if len(prefix) + len(rest) <= limit - PART_ROOM:
            parts.append(prefix + rest)
            break
        room = max(limit - PART_ROOM - len(prefix) - 4, 1)  # 4: a closing fence
        window = rest[:room]
        at = window.rfind("\n")
        if at < room // 2:
            at = max(at, window.rfind(" "))
        skip = 1
        if at < 1:
            at, skip = room, 0
        body, rest = rest[:at], rest[at + skip :]
        part = prefix + body
        opener, head = scan(body, opener, head)
        if opener:
            part += "\n" + opener[:3]
        if head and not rest.lstrip().startswith("|"):
            head = None
        parts.append(part)
    if len(parts) > 1:
        parts = [f"{part}\n\n({n}/{len(parts)})" for n, part in enumerate(parts, 1)]
    return parts


PREVIEW_LINES = 12  # lines of a long code block that stay in the message
MIN_CODE = 1200  # a code block shorter than this stays in the message whole
MAX_CODE_FILES = 4
EXTENSIONS = {
    "sql": "sql",
    "python": "py",
    "py": "py",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "bash": "sh",
    "sh": "sh",
    "shell": "sh",
    "javascript": "js",
    "js": "js",
    "typescript": "ts",
    "ts": "ts",
    "xml": "xml",
    "csv": "csv",
    "html": "html",
    "markdown": "md",
    "md": "md",
    "diff": "diff",
}


def code_blocks(lines):
    """(first, last, language, code lines) for each fenced block. `last` is the closing fence, or
    the final line when the block is never closed. Fences toggle, as in post()."""
    found, opened = [], None
    for index, line in enumerate(lines):
        match = FENCE.match(line)
        if not match:
            continue
        if opened is None:
            tag = line.strip()[len(match.group(1)) :].split()
            opened = (index, tag[0].lower() if tag else "")
        else:
            found.append((opened[0], index, opened[1], lines[opened[0] + 1 : index]))
            opened = None
    if opened is not None:
        found.append((opened[0], len(lines) - 1, opened[1], lines[opened[0] + 1 :]))
    return found


def attach_code(text, room):
    """(text, files): the answer with its longest code blocks sent as files instead.

    Only when the answer is longer than `room`, the characters that fit in the message. Then the
    longest code blocks (those of at least MIN_CODE characters and more than PREVIEW_LINES
    lines), largest first and only as many as it takes to fit, are each replaced by their first
    lines and a note; the whole block goes out as a file. At most MAX_CODE_FILES, so an answer
    that is still too long is cut by shorten() as before."""
    if len(text) <= room:
        return text, []
    lines = text.split("\n")
    found = [
        b
        for b in code_blocks(lines)
        if len(b[3]) > PREVIEW_LINES and len("\n".join(b[3])) >= MIN_CODE
    ]
    chosen = []
    for block in sorted(found, key=lambda b: -len("\n".join(b[3]))):
        if len(chosen) >= MAX_CODE_FILES:
            break
        chosen.append(block)
        built, files = replace_blocks(lines, sorted(chosen))
        if len(built) <= room:
            return built, files
    if not chosen:
        return text, []
    return replace_blocks(lines, sorted(chosen))


def replace_blocks(lines, chosen):
    out, files, cursor = [], [], 0
    for number, (first, last, language, code) in enumerate(chosen, 1):
        name = f"code-{number}.{EXTENSIONS.get(language, 'txt')}"
        marker = FENCE.match(lines[first]).group(1)
        out += lines[cursor:first]
        out += [lines[first], *code[:PREVIEW_LINES], marker]
        out.append(
            f"(First {PREVIEW_LINES} of {len(code)} lines. The full code is attached as `{name}`.)"
        )
        files.append({"name": name, "text": "\n".join(code) + "\n"})
        cursor = last + 1
    out += lines[cursor:]
    return "\n".join(out), files


def mention_id(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]{1,128}", value)
        else None
    )


def inert_mentions(text):
    """Only the Gateway may create a notification from a model answer."""
    return re.sub(r"<(?=/?at(?:\s|>))", "&lt;", text, flags=re.I)


def addressed_card(card, mention):
    data = json.loads(inert_mentions(card))
    if mention:
        elements = data["body"]["elements"] if "body" in data else data["elements"]
        tagged = f"<at id={mention}></at>"
        element = (
            {"tag": "markdown", "content": tagged}
            if "body" in data
            else {"tag": "div", "text": {"tag": "lark_md", "content": tagged}}
        )
        elements.insert(0, element)
    return json.dumps(data, ensure_ascii=False)


def post(text, mention=None, attachments=()):
    """A post body with one markdown element per prose run and per fenced code block.

    Lark's markdown renderer can swallow text that follows a fence inside one large
    element, so each fence gets its own row.
    """
    rows, current, in_code = [], [], False

    def flush():
        segment = "\n".join(current)
        current.clear()
        if segment.strip():
            rows.append([{"tag": "md", "text": segment}])

    for line in text.splitlines():
        fence = bool(FENCE.match(line))
        if fence and not in_code:
            flush()
        current.append(line)
        if fence:
            in_code = not in_code
            if not in_code:
                flush()
    flush()
    rows = rows or [[{"tag": "md", "text": text}]]
    if mention:
        rows.insert(0, [{"tag": "at", "user_id": mention}])
    content = {"zh_cn": {"content": rows}}
    if attachments:
        content["files"] = [{"key": key} for key in attachments]
    return json.dumps(content, ensure_ascii=False)


def plain(text, mention=None):
    if mention:
        text = f'<at user_id="{mention}"></at> ' + text
    return json.dumps({"text": text}, ensure_ascii=False)
