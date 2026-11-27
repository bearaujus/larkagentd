"""Refresh Lark source passages with fenced claims in the Tool Service."""

import logging
import time
import uuid

from sqlalchemy import distinct, func, select

from ..core.diagnostics import emit
from ..toolservice.larkcli import LarkError
from .attachments import attachment_link, attachments, passages
from .chunking import DocumentTooLarge, check_limits, chunk_markdown
from .library import GLOBAL, sources
from .policy import effective
from .sanitize import sanitize
from .wiki import UnlistedBranch

INTERVAL = 15 * 60
LEASE = 180


class LostClaim(Exception):
    pass


def _chunks(page, content, source_id, lease):
    title = sanitize(content.title, max_chars=160)
    if not title.text or title.blocked:
        return []
    chunks = chunk_markdown(content.markdown)
    check_limits(chunks, sum(chunk.tokens for chunk in chunks))
    result = []
    digest = content.digest()
    for chunk in chunks:
        body = sanitize(chunk.text)
        section = sanitize(chunk.section_path, max_chars=240)
        if (
            body.text
            and not body.blocked
            and (not chunk.section_path or not section.blocked)
        ):
            result.append(
                dict(
                    source_id=source_id,
                    doc=page.link.token,
                    doc_kind=page.link.kind,
                    node=page.node,
                    ordinal=chunk.ordinal,
                    title=title.text,
                    section=section.text or "",
                    body=body.text,
                    digest=digest,
                    seen=lease,
                )
            )
    return result


class AttachmentSync:
    def __init__(self, store, reader, config):
        self.store, self.reader, self.config = store, reader, config

    def run_one(self, stop=None):
        now = time.time()
        with self.store.engine.begin() as conn:
            row = (
                conn.execute(
                    select(attachments, sources.c.scope, sources.c.name)
                    .join(sources)
                    .where(attachments.c.due <= now)
                    .order_by(attachments.c.due, attachments.c.source_id)
                    .limit(1)
                    .with_for_update(of=attachments, skip_locked=True)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            source_id, root = row["source_id"], attachment_link(row["url"])
            scope = row["scope"]
            state = effective(conn, self.config, scope) if scope != GLOBAL else None
            if state and not state.serving:
                conn.execute(
                    attachments.update()
                    .where(attachments.c.source_id == source_id)
                    .values(status="paused", due=now + 60, lease=None, error=None)
                )
                return True
            lease = uuid.uuid4().hex
            conn.execute(
                attachments.update()
                .where(attachments.c.source_id == source_id)
                .values(lease=lease, status="syncing", due=now + LEASE, error=None)
            )
        target = (attachments.c.source_id == source_id) & (attachments.c.lease == lease)
        owned = passages.c.source_id == source_id

        def lock(conn):
            if (
                state
                and effective(conn, self.config, scope).generation != state.generation
            ):
                raise LostClaim
            if stop is not None and stop.is_set():
                raise LostClaim
            if not conn.scalar(
                select(attachments.c.source_id).where(target).with_for_update()
            ):
                raise LostClaim

        def heartbeat():
            with self.store.engine.begin() as conn:
                lock(conn)
                conn.execute(
                    attachments.update().where(target).values(due=time.time() + LEASE)
                )

        skipped, error, complete = 0, None, False
        inventory_complete = True
        try:
            if row["name"] is None:
                try:
                    title = self.reader.root_title(root)
                    title = (
                        sanitize(title, max_chars=512).text
                        if isinstance(title, str)
                        else None
                    )
                    with self.store.engine.begin() as conn:
                        lock(conn)
                        conn.execute(
                            attachments.update().where(target).values(title=title)
                        )
                except LarkError as exc:
                    error = exc.code
                    if exc.code in {"forbidden", "not_found"}:
                        with self.store.engine.begin() as conn:
                            lock(conn)
                            conn.execute(
                                attachments.update().where(target).values(title=None)
                            )
            for page in self.reader.walk(root, heartbeat):
                if isinstance(page, UnlistedBranch):
                    inventory_complete, error = False, page.error
                    skipped += 1
                    continue
                if page is None:
                    skipped += 1
                    continue
                heartbeat()
                try:
                    content = self.reader.read(page)
                    rows = _chunks(page, content, source_id, lease)
                except DocumentTooLarge:
                    rows = []
                except LarkError as exc:
                    if exc.code in {"timeout", "rate_limited"}:
                        raise
                    if exc.code not in {"forbidden", "not_found"}:
                        inventory_complete, error = False, exc.code
                        skipped += 1
                        continue
                    if root.kind in {"docx", "docs"}:
                        error = exc.code
                    rows = []
                with self.store.engine.begin() as conn:
                    lock(conn)
                    doc = owned & (passages.c.doc == page.link.token)
                    if (
                        rows
                        and conn.execute(
                            passages.update()
                            .where(
                                doc,
                                passages.c.digest == rows[0]["digest"],
                                passages.c.node.is_not_distinct_from(page.node),
                            )
                            .values(seen=lease)
                        ).rowcount
                    ):
                        continue
                    conn.execute(passages.delete().where(doc))
                    if rows:
                        conn.execute(passages.insert(), rows)
                    else:
                        skipped += 1
            complete = inventory_complete
        except LostClaim:
            return True
        except LarkError as exc:
            error = exc.code
        with self.store.engine.begin() as conn:
            if not conn.scalar(
                select(attachments.c.source_id).where(target).with_for_update()
            ):
                return True
            if complete:
                conn.execute(passages.delete().where(owned, passages.c.seen != lease))
            indexed = conn.scalar(
                select(func.count(distinct(passages.c.doc))).where(owned)
            )
            values = dict(
                indexed=indexed,
                skipped=skipped,
                status="partial" if error else "ready",
                error=error,
                due=time.time() + (60 if error else INTERVAL),
                lease=None,
            )
            if not error:
                values["last_success"] = time.time()
            conn.execute(attachments.update().where(target).values(**values))
        return True

    def run(self, stop):
        while not stop.is_set():
            try:
                if self.run_one(stop):
                    continue
            except Exception:
                # Exceptions can include SQL parameters or upstream content.
                emit("knowledge.sync_failed", logging.WARNING, reason="worker_error")
            stop.wait(5)
