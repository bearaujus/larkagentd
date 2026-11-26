from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select

from . import policy
from .catalog import Catalog
from .providers.base import namespace_for
from .tables import gb_discussion_participants, gb_discussions, gb_index_entries


class HybridRetrieval:
    """Fetch group-scoped lexical and verified semantic candidates, then RRF-fuse them."""

    def __init__(self, runtime, catalog=None):
        self.runtime = runtime
        self.catalog = catalog or Catalog()

    def search(
        self,
        group_key,
        query,
        keywords,
        identifiers,
        start,
        end,
        requester_id,
        self_only=False,
        limit=50,
    ):
        with self.runtime.connection() as conn:
            state = policy.effective(conn, self.runtime.config, group_key)
            generation = state.generation
            if not state.allows("recall"):
                return [], {
                    "mode": "live",
                    "reason": state.reasons[0] if state.reasons else "disabled",
                }
            lexical = self.catalog.lexical_search(
                conn,
                group_key,
                keywords,
                identifiers,
                start,
                end,
                self_only=self_only,
                requester_id=requester_id,
                limit=limit,
            )
            if generation > 1:
                revalidated = set(
                    conn.execute(
                        select(gb_index_entries.c.canonical_id)
                        .select_from(
                            gb_index_entries.join(
                                gb_discussions,
                                (
                                    gb_index_entries.c.group_key
                                    == gb_discussions.c.group_key
                                )
                                & (
                                    gb_index_entries.c.canonical_id
                                    == gb_discussions.c.id
                                ),
                            )
                        )
                        .where(
                            gb_index_entries.c.group_key == group_key,
                            gb_index_entries.c.kind == "discussion",
                            gb_index_entries.c.state == "active",
                            gb_index_entries.c.generation == generation,
                            gb_discussions.c.status == "active",
                            gb_discussions.c.expires_at > func.now(),
                            gb_discussions.c.source_revision
                            == gb_index_entries.c.source_revision,
                        )
                    )
                    .scalars()
                    .all()
                )
                lexical = [row for row in lexical if row["id"] in revalidated]
        rankings = []
        for row in lexical:
            rankings.append(("lexical", row["id"], row))
        semantic_ok = (
            self.runtime.index is not None and self.runtime.config.embedding_configured
        )
        failure = None
        if semantic_ok:
            namespace = namespace_for(
                self.runtime.config.collection, group_key, "discussion"
            )
            try:
                with self.runtime.connection() as conn:
                    current = policy.effective(conn, self.runtime.config, group_key)
                if not current.allows("recall") or current.generation != generation:
                    return [], {
                        "mode": "live",
                        "reason": current.reasons[0]
                        if current.reasons
                        else "stale_generation",
                    }
                hits = self.runtime.index.search(
                    namespace=namespace, query=query, top_k=min(limit * 4, 100)
                )
                for rank, hit in enumerate(hits, start=1):
                    with self.runtime.connection() as conn:
                        state = policy.effective(conn, self.runtime.config, group_key)
                        if not state.allows("recall") or state.generation != generation:
                            break
                        entry = conn.execute(
                            select(
                                gb_index_entries.c.canonical_id,
                                gb_index_entries.c.source_revision,
                                gb_index_entries.c.idempotency_key,
                                gb_index_entries.c.namespace,
                                gb_index_entries.c.generation,
                            )
                            .select_from(
                                gb_index_entries.join(
                                    gb_discussions,
                                    (
                                        gb_index_entries.c.group_key
                                        == gb_discussions.c.group_key
                                    )
                                    & (
                                        gb_index_entries.c.canonical_id
                                        == gb_discussions.c.id
                                    ),
                                )
                            )
                            .where(
                                gb_index_entries.c.group_key == group_key,
                                gb_index_entries.c.kind == "discussion",
                                gb_index_entries.c.state == "active",
                                gb_index_entries.c.provider_id == hit.provider_id,
                                gb_index_entries.c.namespace == namespace,
                                gb_index_entries.c.generation == generation,
                                gb_discussions.c.status == "active",
                                gb_discussions.c.expires_at > func.now(),
                                gb_discussions.c.source_revision
                                == gb_index_entries.c.source_revision,
                            )
                        ).first()
                    if entry is None:
                        continue
                    metadata = hit.metadata
                    if (
                        not isinstance(metadata, dict)
                        or metadata.get("kind") != "discussion"
                        or metadata.get("canonical_id") != entry.canonical_id
                        or metadata.get("revision") != entry.source_revision
                        or metadata.get("index_key") != entry.idempotency_key
                        or metadata.get("group_key", group_key) != group_key
                    ):
                        continue
                    with self.runtime.connection() as conn:
                        current = policy.effective(conn, self.runtime.config, group_key)
                        if (
                            not current.allows("recall")
                            or current.generation != state.generation
                        ):
                            break
                        candidate_query = select(gb_discussions).where(
                            gb_discussions.c.group_key == group_key,
                            gb_discussions.c.id == entry.canonical_id,
                            gb_discussions.c.status == "active",
                            gb_discussions.c.expires_at > func.now(),
                            gb_discussions.c.source_revision == entry.source_revision,
                        )
                        if start is not None:
                            candidate_query = candidate_query.where(
                                gb_discussions.c.ended_at >= start
                            )
                        if end is not None:
                            candidate_query = candidate_query.where(
                                gb_discussions.c.started_at < end
                            )
                        if self_only:
                            candidate_query = candidate_query.where(
                                select(gb_discussion_participants.c.discussion_id)
                                .where(
                                    gb_discussion_participants.c.group_key == group_key,
                                    gb_discussion_participants.c.discussion_id
                                    == gb_discussions.c.id,
                                    gb_discussion_participants.c.sender_id
                                    == requester_id,
                                )
                                .exists()
                            )
                        candidate = conn.execute(candidate_query).mappings().first()
                        if candidate is not None:
                            row = dict(candidate)
                            row["participant_match"] = bool(
                                requester_id
                                and conn.execute(
                                    select(
                                        gb_discussion_participants.c.discussion_id
                                    ).where(
                                        gb_discussion_participants.c.group_key
                                        == group_key,
                                        gb_discussion_participants.c.discussion_id
                                        == row["id"],
                                        gb_discussion_participants.c.sender_id
                                        == requester_id,
                                    )
                                ).first()
                            )
                        else:
                            row = None
                    if row is not None:
                        rankings.append(("semantic", row["id"], row))
            except Exception:
                failure = "dependency_unavailable"
        fused = defaultdict(float)
        canonical = {}
        rank_by_source = {"lexical": 0, "semantic": 0}
        for source, discussion_id, row in rankings:
            rank_by_source[source] += 1
            fused[discussion_id] += 1 / (60 + rank_by_source[source])
            canonical[discussion_id] = row
        ordered = sorted(
            canonical,
            key=lambda key: (
                -fused[key],
                -canonical[key]["ended_at"].timestamp(),
                key,
            ),
        )
        mode = "hybrid" if semantic_ok and failure is None else "live"
        with self.runtime.connection() as conn:
            final_state = policy.effective(conn, self.runtime.config, group_key)
        if not final_state.allows("recall") or final_state.generation != generation:
            return [], {
                "mode": "live",
                "reason": final_state.reasons[0]
                if not final_state.allows("recall") and final_state.reasons
                else "stale_generation",
            }
        return [(canonical[key], fused[key]) for key in ordered[:limit]], {
            "mode": mode,
            "reason": failure,
        }
