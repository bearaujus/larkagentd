from __future__ import annotations

import re
from dataclasses import dataclass

from .sanitize import token_count


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    section_path: str
    block_anchor: str | None
    text: str
    tokens: int


class DocumentTooLarge(ValueError):
    code = "too_large"


def check_limits(chunks, tokens, *, max_chunks=250, max_tokens=100_000):
    if len(chunks) > max_chunks or tokens > max_tokens:
        raise DocumentTooLarge("too_large")


def _split_block(text: str, limit: int) -> list[str]:
    """Split a block at lines or sentence boundaries, falling back to bounded characters."""
    if token_count(text) <= limit:
        return [text]
    result = []
    rest = text
    char_limit = max(1, (limit - 1) * 4)
    while rest:
        if token_count(rest) <= limit:
            result.append(rest)
            break
        edge = min(len(rest), char_limit)
        candidate = rest[:edge]
        cuts = [
            candidate.rfind("\n"),
            candidate.rfind(". "),
            candidate.rfind("! "),
            candidate.rfind("? "),
            candidate.rfind(" "),
        ]
        cut = max(cuts)
        if cut < max(1, edge // 3):
            cut = edge
        elif candidate[cut : cut + 1] in ".!?":
            cut += 1
        piece = rest[:cut]
        if not piece:
            piece, cut = rest[:edge], edge
        result.append(piece)
        rest = rest[cut:]
    return result


def _blocks(text: str):
    lines = text.splitlines(keepends=True)
    blocks = []
    current = []
    fenced = False
    for line in lines:
        fence = re.match(r"^\s*(```+|~~~+)", line)
        if fence:
            current.append(line)
            if not fenced:
                fenced = True
            else:
                fenced = False
                blocks.append("".join(current))
                current = []
        elif fenced:
            current.append(line)
        elif re.match(r"^\s{0,3}#{1,6}\s+", line):
            if current:
                blocks.append("".join(current))
                current = []
            blocks.append(line)
        elif not line.strip():
            if current:
                blocks.append("".join(current))
                current = []
            blocks.append(line)
        else:
            current.append(line)
    if current:
        blocks.append("".join(current))
    return blocks


def chunk_markdown(text, *, chunk_tokens=512, overlap=64):
    if not isinstance(text, str) or not text or not text.strip():
        return []
    if (
        not isinstance(chunk_tokens, int)
        or isinstance(chunk_tokens, bool)
        or chunk_tokens < 1
    ):
        raise ValueError("chunk_tokens must be positive")
    if (
        not isinstance(overlap, int)
        or isinstance(overlap, bool)
        or overlap < 0
        or overlap >= chunk_tokens
    ):
        raise ValueError("overlap must be non-negative and smaller than chunk_tokens")
    blocks = _blocks(text)
    section = []
    segments = []
    for block in blocks:
        if block.lstrip().startswith("#"):
            heading = re.match(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", block.strip())
            if heading:
                depth = len(block.lstrip()) - len(block.lstrip().lstrip("#"))
                section = section[: depth - 1] + [heading.group(1).strip()]
        path = " > ".join(section)
        if block.isspace():
            segments.append((path, None, block))
        else:
            # Leave room for the next chunk's overlap when splitting an oversized block.
            split_limit = max(1, chunk_tokens - overlap)
            for part in _split_block(block, split_limit):
                segments.append((path, None, part))
    chunks: list[Chunk] = []
    active_path = None
    buffer = ""

    def flush():
        nonlocal buffer
        if buffer.strip():
            chunks.append(
                Chunk(len(chunks), active_path or "", None, buffer, token_count(buffer))
            )
        buffer = ""

    for path, anchor, block in segments:
        if not block.strip():
            buffer += block
            continue
        if active_path is not None and path != active_path:
            flush()
        active_path = path
        if token_count(buffer + block) <= chunk_tokens:
            buffer += block
            continue
        flush()
        if chunks and chunks[-1].section_path == path and overlap:
            prior = chunks[-1].text
            carry = prior[-overlap * 4 :]
            while carry and token_count(carry) > overlap:
                carry = carry[1:]
            buffer = carry
        if token_count(buffer + block) <= chunk_tokens:
            buffer += block
        else:
            buffer = block
            flush()
    flush()
    return chunks
