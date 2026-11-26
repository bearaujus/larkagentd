"""Conversation recall for one group.

Phase 1 recall reads the group's own recent history live from Lark through a `GroupSource`, rebuilds
discussions, and ranks them with exact identifiers and keywords. It never reads any other chat, takes
no chat id from the caller, returns only sanitized text, and reports honestly when it could not cover
the window. Every result is a typed dict with a `status` from `models.STATUSES`."""

import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from .catalog import Catalog
from .discussions import reconstruct
from .lark_sources import SourceError
from .policy import effective
from .retrieval import HybridRetrieval
from .sanitize import sanitize
from .tables import (
    gb_discussion_messages,
    gb_discussion_participants,
    gb_discussions,
    gb_messages,
)

PAGE_SIZE = 50
MAX_PAGES = 6  # at most 300 messages are read from Lark for one search
MAX_THREAD_EXPANSIONS = 8
MAX_QUERY = 1000
SNIPPET = 240
TITLE = 120
SOURCES_PER_RESULT = 3
STOPWORDS = frozenset(
    "a an and are as at be by did do for from how i in is it my of on or the this that to was we what when where who why with".split()
)
TOKEN = re.compile(r"[A-Za-z0-9_.\-]{2,}")
IDENTIFIER = re.compile(r"(?=[A-Za-z0-9_.\-]*[0-9_])[A-Za-z0-9_.\-]{3,}|[A-Z]{3,}")
STATUS_OF_SOURCE_ERROR = {
    "forbidden": ("source_unavailable", "lark_permission"),
    "not_found": ("source_unavailable", "not_found"),
    "rate_limited": ("rate_limited", None),
    "timeout": ("dependency_unavailable", "timeout"),
    "unavailable": ("dependency_unavailable", "lark_unavailable"),
    "bad_response": ("dependency_unavailable", "bad_response"),
}


def opaque(prefix, *parts):
    """A stable opaque id: the same inputs always give the same id, and it reveals nothing."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return f"{prefix}_{digest[:20]}"


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_moment(value):
    """A date (UTC midnight) or an RFC 3339 time as an aware UTC datetime. None for None. A bad
    value raises ValueError. A date is the first instant of that day: as a start it is included and as
    an end it is not, so 2026-01-01 to 2026-01-02 is exactly the first of January."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)
        return datetime.combine(day, time(0), tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError from None


def terms_of(query):
    """(keywords, exact identifiers) from a search phrase, lowercased and deduplicated."""
    words = TOKEN.findall(query)
    identifiers = [w for w in words if IDENTIFIER.fullmatch(w)]
    keywords = [w.lower() for w in words if w.lower() not in STOPWORDS]
    return list(dict.fromkeys(keywords)), list(
        dict.fromkeys(i.lower() for i in identifiers)
    )


def score(texts, keywords, identifiers):
    """A lexical score: exact identifiers count most. 0 means no match at all."""
    blob = "\n".join(texts).lower()
    hits = sum(1 for k in keywords if k in blob)
    exact = sum(1 for i in identifiers if i in blob)
    if not hits and not exact:
        return 0.0
    return exact * 3.0 + hits / max(1, len(keywords))


def snippet(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def refusal(status, reason=None):
    result = {"status": status, "results": []}
    if reason:
        result["reason"] = reason
    return result


class Recall:
    """Live recall for the group of one verified turn."""

    def __init__(self, runtime):
        self.runtime = runtime

    # --- gate ---

    def gate(self, group_key, capability="recall"):
        """(effective state, None) when the group may serve `capability`; else (None, refusal).
        Runs before any call to Lark or a provider."""
        with self.runtime.connection() as conn:
            state = effective(conn, self.runtime.config, group_key)
        if not state.allows(capability):
            return None, refusal(
                "disabled", state.reasons[0] if state.reasons else None
            )
        if self.runtime.source is None:
            return None, refusal("dependency_unavailable", "lark_reader_unavailable")
        return state, None

    # --- search ---

    def search_discussions(self, group_key, requester_id, arguments, now=None):
        state, blocked = self.gate(group_key)
        if blocked:
            return blocked
        try:
            query, participant, start, end, limit = self.parse(arguments, state, now)
        except ValueError:
            return refusal("invalid_request")
        if participant == "unsupported":
            return refusal("invalid_request", "participant_ref_unsupported")
        keywords, identifiers = terms_of(query)
        if not keywords and not identifiers:
            return refusal("invalid_request", "query_too_vague")
        try:
            scan = self.scan(group_key, start, end, keywords, identifiers)
        except SourceError as exc:
            status, reason = STATUS_OF_SOURCE_ERROR.get(
                exc.code, ("dependency_unavailable", None)
            )
            return refusal(status, reason)
        state = self.runtime.effective(group_key)
        if not state.allows("recall"):
            return refusal("disabled", state.reasons[0] if state.reasons else None)
        ranked = []
        for discussion, texts in scan["discussions"]:
            relevance = score(texts, keywords, identifiers)
            if not relevance:
                continue
            mine = any(sender == requester_id for sender, _ in discussion.participants)
            if participant == "self" and not mine:
                continue
            ranked.append((relevance, discussion.ended_at, discussion, mine))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        results = [
            self.evidence(group_key, scan, discussion, mine, keywords, identifiers)
            for _, _, discussion, mine in ranked
        ]
        fused = {
            item["discussion_id"]: 1 / (60 + rank)
            for rank, item in enumerate(results, start=1)
        }
        candidates, catalog_coverage = HybridRetrieval(self.runtime).search(
            group_key,
            query,
            keywords,
            identifiers,
            start,
            end,
            requester_id,
            self_only=participant == "self",
            limit=limit,
        )
        seen = {item["discussion_id"] for item in results}
        catalog = Catalog()
        for rank, (row, _rrf_score) in enumerate(candidates, start=1):
            key = (
                row["native_thread_id"]
                or row["stable_window_key"]
                or row["root_message_id"]
            )
            discussion_id = opaque("d", group_key, row["kind"], key)
            if discussion_id in seen:
                fused[discussion_id] += 1 / (60 + rank)
                continue
            with self.runtime.connection() as conn:
                sources = catalog.sources_of(conn, group_key, row["id"])
                participants = (
                    conn.execute(
                        select(gb_discussion_participants.c.sender_id).where(
                            gb_discussion_participants.c.group_key == group_key,
                            gb_discussion_participants.c.discussion_id == row["id"],
                        )
                    )
                    .scalars()
                    .all()
                )
            if not sources:
                continue
            results.append(
                {
                    "evidence_id": opaque("ev", discussion_id),
                    "kind": "discussion",
                    "discussion_id": discussion_id,
                    "discussion_type": row["kind"],
                    "title": snippet(row["title"], TITLE),
                    "snippet": snippet(row["summary"], SNIPPET),
                    "started_at": iso(row["started_at"]),
                    "ended_at": iso(row["ended_at"]),
                    "participant_match": requester_id in participants,
                    "participant_count": len(participants),
                    "sources": [
                        {
                            "source_id": opaque(
                                "src", group_key, source["lark_message_id"]
                            ),
                            "link_type": "message"
                            if source["message_link"]
                            else "none",
                            "url": source["message_link"],
                            "timestamp": iso(source["created_at"]),
                            "verified": True,
                        }
                        for source in sources[:SOURCES_PER_RESULT]
                    ],
                    "revision": row["source_revision"],
                }
            )
            fused[discussion_id] = 1 / (60 + rank)
            seen.add(discussion_id)
        results.sort(
            key=lambda item: (
                -fused[item["discussion_id"]],
                -parse_moment(item["ended_at"]).timestamp(),
                item["discussion_id"],
            )
        )
        coverage = dict(scan["coverage"])
        coverage["mode"] = catalog_coverage["mode"]
        if catalog_coverage.get("reason"):
            coverage["reason"] = catalog_coverage["reason"]
        return {
            "status": "ok" if scan["complete"] else "partial",
            "coverage": coverage,
            "results": results[:limit],
        }

    def parse(self, arguments, state, now=None):
        query = arguments.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= MAX_QUERY:
            raise ValueError
        participant = arguments.get("participant", "any")
        if participant not in ("any", "self"):
            raise ValueError
        if arguments.get("participant_ref"):
            # Identity resolution is not available yet: refuse rather than guess a person.
            participant = "unsupported"
        limit = arguments.get("limit", self.runtime.config.top_k)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            raise ValueError
        now = now or datetime.now(timezone.utc)
        floor = now - timedelta(days=state.history_window_days)
        start = parse_moment(arguments.get("date_from")) or floor
        end = parse_moment(arguments.get("date_to")) or now + timedelta(seconds=1)
        if start >= end:
            raise ValueError
        # Never reach back further than the group's approved window.
        return query.strip(), participant, max(start, floor), end, limit

    def scan(self, group_key, start, end, keywords, identifiers, expand=True):
        """Read the group's history newest first down to `start`, rebuild its discussions and return
        them with sanitized texts and the coverage that was actually achieved."""
        chat_id = group_key.removeprefix("group:")
        source = self.runtime.source
        messages, token, reached_start, pages = [], None, False, 0
        page_failed = False
        while pages < MAX_PAGES:
            try:
                page = source.list_messages(chat_id, page_token=token, limit=PAGE_SIZE)
            except SourceError:
                if pages == 0:
                    raise
                page_failed = True
                break
            pages += 1
            for message in page.messages:
                if message.chat_id != chat_id:
                    continue  # a message of another chat is never used, whatever the source returned
                if message.created_at < start:
                    reached_start = True
                elif message.created_at < end:
                    messages.append(message)
            if reached_start or page.complete:
                reached_start = True
                break
            if not page.next_page_token or page.next_page_token == token:
                page_failed = True
                break
            token = page.next_page_token
        clean, skipped = self.clean(messages)
        known_complete = (
            self.expand_threads(chat_id, clean, keywords, identifiers)
            if expand
            else set()
        )
        discussions = reconstruct([m for m, _ in clean], thread_complete=known_complete)
        texts = {m.message_id: text for m, text in clean}
        built = [
            (d, [texts[mid] for mid in d.message_ids if mid in texts])
            for d in discussions
        ]
        complete = reached_start and not any(d.partial for d in discussions)
        coverage = {
            "mode": "live",
            "complete": bool(complete),
            "reason": "page_unavailable"
            if page_failed
            else None
            if complete
            else self.why_partial(reached_start, discussions),
            "window": {
                "from": iso(start),
                "to": iso(min(end, datetime.now(timezone.utc))),
            },
            "timezone": "UTC",
        }
        return {
            "discussions": built,
            "complete": complete,
            "reached_start": reached_start,
            "coverage": coverage,
            "texts": texts,
            "messages": {m.message_id: m for m, _ in clean},
            "skipped": skipped,
        }

    @staticmethod
    def why_partial(reached_start, discussions):
        if not reached_start:
            return "scan_limit"
        if any(d.partial for d in discussions):
            return "thread_replies_missing"
        return "unknown"

    @staticmethod
    def clean(messages):
        """Messages with their sanitized text. A message that cannot be used safely is dropped and
        only counted: nothing about it, not even a reason per message, reaches the caller."""
        kept, skipped = [], 0
        for message in messages:
            if message.sender_type not in ("user", "app"):
                skipped += 1
                continue
            result = sanitize(message.text)
            if result.blocked or not result.text:
                skipped += 1
                continue
            kept.append((message, result.text))
        return kept, skipped

    def expand_threads(self, chat_id, clean, keywords, identifiers):
        """Read the full replies of the native threads that look relevant, so participation is judged
        on every author, not on the messages the keyword happened to hit. Returns the thread ids that
        are now known complete. Bounded: at most MAX_THREAD_EXPANSIONS threads."""
        by_thread = {}
        for message, text in clean:
            if message.thread_id:
                by_thread.setdefault(message.thread_id, []).append(text)
        ranked = sorted(
            by_thread,
            key=lambda t: score(by_thread[t], keywords, identifiers),
            reverse=True,
        )
        complete = set()
        seen = {m.message_id for m, _ in clean}
        for thread_id in ranked[:MAX_THREAD_EXPANSIONS]:
            if not score(by_thread[thread_id], keywords, identifiers):
                break
            page = self.runtime.source.thread_messages(
                chat_id, thread_id, limit=PAGE_SIZE
            )
            extra, _ = self.clean(
                [
                    m
                    for m in page.messages
                    if m.chat_id == chat_id and m.message_id not in seen
                ]
            )
            clean.extend(extra)
            seen.update(m.message_id for m, _ in extra)
            if page.complete:
                complete.add(thread_id)
        return complete

    def read_thread(self, group_key, scan, discussion):
        """The discussion of a native thread rebuilt from its full replies. New messages are added to
        `scan`; the thread's completeness decides `scan["complete"]`."""
        chat_id = group_key.removeprefix("group:")
        page = self.runtime.source.thread_messages(
            chat_id, discussion.native_thread_id, limit=PAGE_SIZE
        )
        extra, _ = self.clean(
            [
                m
                for m in page.messages
                if m.chat_id == chat_id and m.message_id not in scan["messages"]
            ]
        )
        for message, text in extra:
            scan["messages"][message.message_id] = message
            scan["texts"][message.message_id] = text
        members = [
            m
            for m in scan["messages"].values()
            if m.thread_id == discussion.native_thread_id
        ]
        complete = {discussion.native_thread_id} if page.complete else set()
        rebuilt = reconstruct(members, thread_complete=complete)
        return (rebuilt[0] if rebuilt else discussion), page.complete

    def evidence(self, group_key, scan, discussion, mine, keywords, identifiers):
        key = (
            discussion.native_thread_id
            or discussion.window_key
            or discussion.root_message_id
        )
        discussion_id = opaque("d", group_key, discussion.kind, key)
        best = max(
            discussion.message_ids,
            key=lambda mid: score([scan["texts"].get(mid, "")], keywords, identifiers),
        )
        first = scan["texts"].get(discussion.message_ids[0], "")
        sources = [
            self.source(group_key, scan["messages"][mid])
            for mid in discussion.message_ids[:1]
        ]
        if best != discussion.message_ids[0]:
            sources.append(self.source(group_key, scan["messages"][best]))
        return {
            "evidence_id": opaque("ev", discussion_id),
            "kind": "discussion",
            "discussion_id": discussion_id,
            "discussion_type": discussion.kind,
            "title": snippet(first, TITLE),
            "snippet": snippet(scan["texts"].get(best, ""), SNIPPET),
            "started_at": iso(discussion.started_at),
            "ended_at": iso(discussion.ended_at),
            "participant_match": mine,
            "participant_count": len(discussion.participants),
            "sources": sources[:SOURCES_PER_RESULT],
            "revision": 1,
        }

    @staticmethod
    def source(group_key, message):
        """A source reference. The link is the verified official one the source returned, or none:
        a synthetic id is never turned into a URL."""
        link = message.link
        return {
            "source_id": opaque("src", group_key, message.message_id),
            "link_type": "message" if link else "none",
            "url": link,
            "timestamp": iso(message.created_at),
            "verified": True,
        }

    # --- one discussion ---

    def get_discussion(
        self, group_key, requester_id, arguments, now=None, *, observe_files=None
    ):
        state, blocked = self.gate(group_key)
        if blocked:
            return {k: v for k, v in blocked.items() if k != "results"}
        discussion_id = arguments.get("discussion_id")
        limit = arguments.get("limit", 20)
        if (
            not isinstance(discussion_id, str)
            or not re.fullmatch(r"d_[0-9a-f]{20}", discussion_id)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 20
        ):
            return {"status": "invalid_request"}
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(days=state.history_window_days)
        try:
            scan = self.scan(
                group_key, start, now + timedelta(seconds=1), [], [], expand=False
            )
        except SourceError as exc:
            status, reason = STATUS_OF_SOURCE_ERROR.get(
                exc.code, ("dependency_unavailable", None)
            )
            return {"status": status, **({"reason": reason} if reason else {})}
        state = self.runtime.effective(group_key)
        if not state.allows("recall"):
            return {
                "status": "disabled",
                **({"reason": state.reasons[0]} if state.reasons else {}),
            }
        for discussion, _ in scan["discussions"]:
            key = (
                discussion.native_thread_id
                or discussion.window_key
                or discussion.root_message_id
            )
            if opaque("d", group_key, discussion.kind, key) != discussion_id:
                continue
            whole = True
            if discussion.native_thread_id:
                # Read every reply of this one thread, so who took part is judged on all its authors.
                try:
                    discussion, whole = self.read_thread(group_key, scan, discussion)
                except SourceError as exc:
                    status, reason = STATUS_OF_SOURCE_ERROR.get(
                        exc.code, ("dependency_unavailable", None)
                    )
                    return {"status": status, **({"reason": reason} if reason else {})}
            labels = {}
            lines = []
            for mid in discussion.message_ids[:limit]:
                message = scan["messages"][mid]
                who = (
                    "you"
                    if message.sender_id == requester_id
                    else labels.setdefault(
                        message.sender_id, f"Person {len(labels) + 1}"
                    )
                )
                words = scan["texts"][mid]
                if observe_files is not None and message.sender_type == "user":
                    observe_files(mid, words)
                lines.append(
                    {
                        "person": who,
                        "time": iso(message.created_at),
                        "text": words,
                        "source": self.source(group_key, message),
                    }
                )
            # This one discussion is complete when the window was read back to its start and, for a
            # native thread, every reply was read. Other discussions' gaps do not matter here.
            complete = bool(scan["reached_start"] and whole)
            coverage = dict(
                scan["coverage"],
                complete=complete,
                reason=None
                if complete
                else (
                    "scan_limit"
                    if not scan["reached_start"]
                    else "thread_replies_missing"
                ),
            )
            return {
                "status": "ok" if complete else "partial",
                "coverage": coverage,
                "discussion_id": discussion_id,
                "discussion_type": discussion.kind,
                "messages": lines,
                "truncated": len(discussion.message_ids) > limit,
            }
        # The live window may not reach an indexed discussion. Revalidate its source rows before
        # serving any catalog text.
        with self.runtime.connection() as conn:
            discussions = (
                conn.execute(
                    select(gb_discussions).where(
                        gb_discussions.c.group_key == group_key,
                        gb_discussions.c.status == "active",
                        gb_discussions.c.expires_at > now,
                        gb_discussions.c.started_at >= start,
                    )
                )
                .mappings()
                .all()
            )
            catalog = Catalog()
            for row in discussions:
                key = (
                    row["native_thread_id"]
                    or row["stable_window_key"]
                    or row["root_message_id"]
                )
                if opaque("d", group_key, row["kind"], key) != discussion_id:
                    continue
                valid_sources = catalog.sources_of(conn, group_key, row["id"])
                links = {
                    source["lark_message_id"]: source["message_link"]
                    for source in valid_sources
                }
                members = (
                    conn.execute(
                        select(
                            gb_messages.c.lark_message_id,
                            gb_messages.c.sender_id,
                            gb_messages.c.created_at,
                            gb_messages.c.sanitized_text,
                        )
                        .select_from(
                            gb_discussion_messages.join(
                                gb_messages,
                                (
                                    gb_discussion_messages.c.group_key
                                    == gb_messages.c.group_key
                                )
                                & (
                                    gb_discussion_messages.c.message_id
                                    == gb_messages.c.id
                                ),
                            )
                        )
                        .where(
                            gb_discussion_messages.c.group_key == group_key,
                            gb_discussion_messages.c.discussion_id == row["id"],
                            gb_messages.c.status == "active",
                            gb_messages.c.visibility_status == "visible",
                            gb_messages.c.expires_at > now,
                            gb_messages.c.sanitized_text.is_not(None),
                        )
                        .order_by(gb_discussion_messages.c.ordinal)
                    )
                    .mappings()
                    .all()
                )
                members = [m for m in members if m["lark_message_id"] in links]
                if not members:
                    return {"status": "not_authorized"}
                labels, lines = {}, []
                for member in members[:limit]:
                    sender_id = member["sender_id"]
                    link = links[member["lark_message_id"]]
                    lines.append(
                        {
                            "person": "you"
                            if sender_id == requester_id
                            else labels.setdefault(
                                sender_id, f"Person {len(labels) + 1}"
                            ),
                            "time": iso(member["created_at"]),
                            "text": member["sanitized_text"],
                            "source": {
                                "source_id": opaque(
                                    "src", group_key, member["lark_message_id"]
                                ),
                                "link_type": "message" if link else "none",
                                "url": link,
                                "timestamp": iso(member["created_at"]),
                                "verified": True,
                            },
                        }
                    )
                complete = row["coverage"] == "complete"
                return {
                    "status": "ok" if complete else "partial",
                    "coverage": {
                        "mode": "indexed",
                        "complete": complete,
                        "reason": None if complete else "catalog_partial",
                    },
                    "discussion_id": discussion_id,
                    "discussion_type": row["kind"],
                    "messages": lines,
                    "truncated": len(members) > limit,
                }
        # Unknown and not-this-group's look the same.
        return {"status": "not_authorized"}
