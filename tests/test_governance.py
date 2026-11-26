from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from http.server import ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
from sqlalchemy import insert, select
from test_agent import Runs, adapter, load
from test_card_lifecycle import drain
from test_core import store as store_fixture
from test_dashboard import ORIGIN, admin, change, identify, login
from test_group_chat import GROUP, ask, direct, owner_state, room, texts

from larkagentd.core.store import (
    governance_flex,
    token_usage,
)
from larkagentd.gateway.access import Access, AccessError
from larkagentd.gateway.dashboard import PREFIX, make_handler
from larkagentd.gateway.governance import Governance
from larkagentd.gateway.hermes import AgentError
from larkagentd.gateway.reporting import Reporting
from larkagentd.lark.ui_text import IDENTITY_UNAVAILABLE, NOT_ALLOWED

store = store_fixture
EMAIL = "person@example.com"
OWNER = "t:u"
FLEX = ["flex", EMAIL]
COUNTS = dict(
    model="model-b",
    input_tokens=70,
    output_tokens=20,
    cache_read_tokens=8,
    cache_write_tokens=2,
    reasoning_tokens=4,
)
RATES = dict(
    input_tokens="2",
    output_tokens="4",
    cache_read_tokens="0.5",
    cache_write_tokens="3",
)


class Clock:
    def __init__(self):
        self.set("2026-11-29T12:00:00")

    def set(self, iso):
        self.current = datetime.fromisoformat(iso + "+00:00")

    def __call__(self, _conn):
        return self.current


@pytest.fixture
def governed(store):
    service, agent, transport = room(store)
    service.access = Access(store, ("admin@example.com",))
    service.governance = Governance(
        service.access,
        seed_limit=500,
        default_model="model-a",
        models=("model-b", "model-c"),
        efforts={"model-b": ("low", "high")},
    )
    clock = Clock()
    service.governance.now = clock
    identify(store, OWNER, EMAIL)
    identify(store, "t:admin", "admin@example.com")
    direct(service, transport, "u", "chat")
    agent.tokens = COUNTS
    return service, agent, transport, clock


def group_turn(svc, transport, user, _chat, text):
    ask(svc, user, text)
    drain(svc, transport)


def edit(service, parts, body=None, *, method="PATCH", actor=None, query=""):
    with service.store.engine.begin() as conn:
        return service.governance.dispatch(
            conn, actor or admin(), method, parts, body or {}, query=query
        )[1]


def usage(service, owner=OWNER):
    with service.store.engine.begin() as conn:
        quota = service.governance.quota(conn, owner)
        return quota, service.governance.usage(conn, quota) if quota else None


def test_flex_tracks_separate_usage_and_can_have_a_lower_limit(governed):
    svc, agent, transport, clock = governed
    result = edit(svc, ["flex", EMAIL.upper()], {"token_limit": 25})
    assert result["flex"] == [
        dict(email=EMAIL, enabled=True, active_limit=25, pending_limit=None, used=0)
    ]
    assert usage(svc)[0]["tier"] == "flex"
    clock.set("2026-12-01T00:00:00")
    assert usage(svc)[0]["limit"] == 25
    group_turn(svc, transport, "u", "chat", "first FlexTier turn")
    group_turn(svc, transport, "u", "chat", "exhausted FlexTier")
    assert len(agent.calls) == 1
    assert any(
        "token limit for this cycle" in (message or "")
        for message in texts(transport, GROUP)
    )
    edit(svc, FLEX, {"enabled": False})
    group_turn(svc, transport, "u", "chat", "FreeTier turn")
    assert usage(svc)[1] == (100, 0)
    edit(svc, FLEX, {"enabled": True})
    assert usage(svc)[1] == (100, 0)
    direct(svc, transport, "u", "chat", "/usage")
    assert any(
        "FlexTier" in (message or "") and "2027-01-01" in message
        for message in texts(transport, "chat")
    )
    change(svc.access, "PATCH", "bot-access", {"paused": True})
    group_turn(svc, transport, "u", "chat", "blocked across tiers")
    assert len(agent.calls) == 2


def test_only_cadence_waits_for_reset_and_promotes_atomically_after_idle(governed):
    svc, _, _, clock = governed
    edit(svc, FLEX, {"token_limit": 30})
    data = edit(svc, ["quota"], {"free_limit": 200, "cadence": "weekly"})
    assert data["quota"]["free_limit"] == 200
    assert data["quota"]["cadence"] == "monthly"
    assert data["quota"]["pending"] == {
        "cadence": "weekly",
        "free_limit": 200,
        "effective_on": "2026-12-01",
    }
    edit(svc, FLEX, {"token_limit": 0})
    assert usage(svc)[0]["limit"] == 0
    assert usage(svc)[0]["resets_on"] == "2026-12-01"
    clock.set("2026-12-01T00:00:00")
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: usage(svc)[0], range(4)))
    assert all(row["limit"] == 0 for row in results)
    assert {row["cycle_start"] for row in results} == {"2026-12-01"}
    assert {row["resets_on"] for row in results} == {"2026-12-07"}
    clock.set("2026-12-07T00:00:00")
    assert usage(svc)[0]["cycle_start"] == "2026-12-07"
    edit(svc, FLEX, {"enabled": False})
    assert usage(svc)[0]["limit"] == 200
    edit(svc, ["quota"], {"cadence": "monthly"})
    assert usage(svc)[0]["resets_on"] == "2026-12-14"
    clock.set("2026-12-14T00:00:00")
    assert usage(svc)[0]["resets_on"] == "2027-01-01"


def test_lowered_personal_free_limit_applies_to_usage_already_recorded(governed):
    svc, agent, transport, clock = governed
    group_turn(svc, transport, "u", "chat", "request before lower limit")
    data = edit(svc, ["quota"], {"free_limit": 80})
    assert data["quota"]["free_limit"] == 80 and not data["quota"]["pending"]
    group_turn(svc, transport, "u", "chat", "refused immediately")
    assert len(agent.calls) == 1 and usage(svc)[1] == (100, 0)
    clock.set("2026-12-01T00:00:00")
    quota, counted = usage(svc)
    assert quota["limit"] == 80 and counted == (0, 0)


def test_seed_reuses_only_first_month_legacy_usage(governed):
    svc, _, _, clock = governed
    with svc.store.engine.begin() as conn:
        conn.execute(
            insert(token_usage).values(
                session_id="legacy",
                job_id="job",
                requester=OWNER,
                principal=OWNER,
                chat_type="p2p",
                month=date(2026, 11, 1),
                input_tokens=450,
            )
        )
    assert usage(svc)[1] == (450, 0)
    svc.governance.seed_limit = 999
    assert usage(svc)[0]["limit"] == 500
    clock.set("2026-12-01T00:00:00")
    assert usage(svc)[1] == (0, 0)


def test_waiting_group_turn_rechecks_new_zero_flex_limit(governed):
    svc, agent, transport, _ = governed
    ask(svc, "u", "queued before the limit edit")
    edit(svc, FLEX, {"token_limit": 0})
    drain(svc, transport)
    assert not agent.calls
    assert any("token limit" in (message or "") for message in texts(transport, GROUP))
    assert not owner_state(svc.store, OWNER).get("busy")


def test_flex_identity_failure_prevents_free_tier_fallback(governed):
    svc, agent, transport, clock = governed
    edit(svc, FLEX, {"token_limit": 0})
    clock.set("2026-12-01T00:00:00")
    svc.receive(
        "t",
        "unverified",
        GROUP,
        "test-event",
        "test-message",
        "hello",
        transport=transport,
        group=True,
        thread="example-thread",
    )
    drain(svc, transport)
    assert not agent.calls
    assert IDENTITY_UNAVAILABLE in texts(transport, GROUP)
    transport.email_of = lambda _: "other@example.com"
    svc.receive(
        "t",
        "verified",
        GROUP,
        "test-event-2",
        "test-message-2",
        "hello",
        transport=transport,
        group=True,
        thread="example-thread",
    )
    drain(svc, transport)
    assert len(agent.calls) == 1


def test_disabled_blocks_queued_and_new_work_but_keeps_dashboard_recovery(governed):
    svc, agent, transport, _ = governed
    ask(svc, "u", "queued")
    change(
        svc.access,
        "PATCH",
        "bot-access",
        {"paused": True},
        actor={"role": "manager", "owner": "t:manager"},
    )
    drain(svc, transport)
    group_turn(svc, transport, "admin", "admin-chat", "please answer")
    direct(svc, transport, "admin", "admin-chat", "/dashboard login")
    direct(svc, transport, "u", "chat", "/dashboard login")
    assert not agent.calls
    assert NOT_ALLOWED in texts(transport, GROUP)
    assert any(
        (message or "").startswith("Dashboard login token (")
        for message in texts(transport, "admin-chat")
    )
    assert "Dashboard access requires an admin or manager role." in texts(
        transport, "chat"
    )
    change(svc.access, "PATCH", "bot-access", {"paused": False})
    group_turn(svc, transport, "admin", "admin-chat", "allowed again")
    assert len(agent.calls) == 1
    with pytest.raises(AccessError, match="invalid_mode"):
        change(svc.access, "PATCH", "groups/grp", {"mode": "disabled"})


def test_running_retry_keeps_its_cycle_tier_model_and_rate(governed):
    svc, agent, transport, clock = governed
    edit(svc, ["model"], {"model": "model-b", "effort": "high"})
    edit(svc, ["prices", "model-b"], RATES, method="PUT")
    edit(svc, FLEX, {"token_limit": 500})
    clock.set("2026-12-31T23:59:59")
    agent.script = [AgentError("unknown_execution"), "finished after reset"]

    def change_during_run(session):
        if session.endswith(":1"):
            clock.set("2027-01-01T00:00:00")
            edit(svc, FLEX, {"enabled": False})
            edit(svc, ["model"], {"model": "model-c", "effort": None})
            edit(
                svc, ["prices", "model-b"], dict(RATES, input_tokens="10"), method="PUT"
            )
            change(svc.access, "PATCH", "bot-access", {"paused": True})

    agent.hook = change_during_run
    ask(svc, "u", "before midnight")
    drain(svc, transport)
    assert agent.requests[0]["model"] == "model-b"
    assert agent.requests[0]["reasoning_effort"] == "high"
    assert agent.calls[0][1] == agent.calls[1][1]
    assert any(
        "finished after reset" in (message or "") for message in texts(transport, GROUP)
    )
    with svc.store.engine.begin() as conn:
        rows = conn.execute(select(token_usage)).mappings().all()
    assert len(rows) == 2
    assert all(r["quota_tier"] == "flex" and r["quota_tracker"] == EMAIL for r in rows)
    assert {r["quota_cycle_start"] for r in rows} == {date(2026, 12, 1)}
    assert {r["estimated_cost_usd"] for r in rows} == {Decimal("0.00023")}
    assert usage(svc)[1] == (0, 0)
    report = Reporting(svc.store)
    summary = report.get("summary", "all_time=true")
    assert summary["monetary_costs_available"]
    assert summary["statistics"]["priced_sessions"] == 2
    assert Decimal(summary["statistics"]["estimated_cost_usd"]) == Decimal("0.00046")
    with report.export("dataset=llm&all_time=true") as (stream, _, count, _):
        assert count == 2 and b",flex,0.00023000000000" in stream.read()


def test_latest_model_applies_in_group_and_dm_cannot_run_model(governed):
    svc, agent, transport, _ = governed
    edit(svc, ["model"], {"model": "model-b", "effort": "low"})
    group_turn(svc, transport, "u", "chat", "/model model-c")
    direct(svc, transport, "u", "chat", "hello in DM")
    ask(svc, "u", "hello in group")
    drain(svc, transport)
    assert [(r["model"], r["reasoning_effort"]) for r in agent.requests] == [
        ("model-b", "low")
    ]
    assert any(
        "dashboard controls the model" in (message or "")
        for message in texts(transport, GROUP)
    )
    ask(svc, "u", "/usage")
    drain(svc, transport)
    assert not any(
        "FreeTier" in (message or "") or "Resets" in (message or "")
        for message in texts(transport, GROUP)
    )


def test_estimates_distinguish_partial_prices_unknown_model_and_missing_usage(governed):
    svc, agent, transport, clock = governed
    edit(svc, ["prices", "model-b"], dict(RATES, cache_read_tokens=None), method="PUT")
    group_turn(svc, transport, "u", "chat", "unpriced cache read")
    agent.tokens = dict(COUNTS, cache_read_tokens=0)
    group_turn(svc, transport, "u", "chat", "priced")
    agent.tokens = dict(COUNTS, model="")
    group_turn(svc, transport, "u", "chat", "unknown model")
    agent.tokens = None
    group_turn(svc, transport, "u", "chat", "missing usage")
    stats = Reporting(svc.store).get("summary", "all_time=true")["statistics"]
    assert (
        stats["priced_sessions"],
        stats["unpriced_sessions"],
        stats["unavailable_sessions"],
    ) == (1, 2, 1)
    assert Decimal(stats["estimated_cost_usd"]) == Decimal("0.000226")
    assert usage(svc)[1] == (292, 1)
    assert agent.requests[0]["model"] == "model-a"


def test_governance_http_enforces_roles_and_origin(governed):
    svc, _, _, _ = governed
    identify(svc.store, "t:manager", "manager@example.com")
    change(svc.access, "PUT", "managers/manager@example.com")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(svc.access, ORIGIN, governance=svc.governance)
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = PREFIX + "/governance"
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}",
            headers={"Origin": ORIGIN, "Host": ORIGIN.split("://", 1)[1]},
        ) as client:
            assert client.get(url).status_code == 401
            assert client.get(url + "/history").status_code == 401
            login(svc.access, client, "t:manager")
            assert client.get(url).status_code == 200
            assert client.get(url + "/history").json() == {
                "items": [],
                "next_before": None,
            }
            assert client.patch(url + "/history").status_code == 405
            pools = url + "/pools/free"
            adjustment = {"limit": 500}
            assert client.patch(pools, json=adjustment).status_code == 403
            assert (
                client.patch(
                    url + "/model", json={"model": "model-b", "effort": "low"}
                ).status_code
                == 403
            )
            assert (
                client.patch(PREFIX + "/bot-access", json={"paused": True}).status_code
                == 200
            )
            login(svc.access, client)
            assert (
                client.patch(
                    pools,
                    json=adjustment,
                    headers={"Origin": "http://other.example.com"},
                ).status_code
                == 403
            )
            assert (
                client.patch(pools, json=dict(adjustment, limit=-1)).status_code == 400
            )
            assert (
                client.patch(pools, json=adjustment).json()["pools"]["free"]["limit"]
                == 500
            )
            model = {"model": "model-b", "effort": "high"}
            assert (
                client.patch(
                    url + "/model",
                    json=model,
                    headers={"Origin": "http://other.example.com"},
                ).status_code
                == 403
            )
            assert client.patch(url + "/model", json=model).json()["effort"] == "high"
            audit = client.get(url + "/history").json()["items"]
            assert len(audit) == 1
            assert audit[0]["actor_email"] == "admin@example.com"
            assert audit[0]["resource"] == "shared_pool"
            assert audit[0]["after"] == {"limit": 500}
            assert client.get(url + "/history?before=0").status_code == 400
            assert (
                client.patch(url + "/model", json=dict(model, effort="max")).status_code
                == 400
            )
            assert (
                client.patch(
                    url + "/quota", json={"free_limit": True, "cadence": "weekly"}
                ).status_code
                == 400
            )
            assert (
                client.put(
                    url + "/prices/model-b", json=dict(RATES, input_tokens="-2")
                ).status_code
                == 400
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_free_pool_counts_across_users_and_rechecks_queued_work(governed):
    svc, agent, transport, _ = governed
    direct(svc, transport, "v", "chat-v")
    changed = edit(svc, ["pools", "free"], {"limit": 100})
    assert changed["pools"]["free"]["limit"] == 100
    ask(svc, "u", "first queued request")
    ask(svc, "v", "queued while the pool has room")
    drain(svc, transport)
    assert len(agent.calls) == 1
    assert any(
        "shared token pool" in (message or "") for message in texts(transport, GROUP)
    )
    changed = edit(svc, ["pools", "free"], {"limit": 150})
    assert changed["pools"]["free"]["used"] == 100
    group_turn(svc, transport, "v", "chat-v", "try after increase")
    assert len(agent.calls) == 2
    assert usage(svc, "t:v")[1] == (100, 0)
    changed = edit(svc, ["pools", "free"], {"limit": 50})
    assert changed["pools"]["free"]["used"] == 200
    assert changed["pools"]["flex"]["used"] == 0
    group_turn(svc, transport, "u", "chat", "try after decrease")
    assert len(agent.calls) == 2


def test_flex_pool_and_personal_limits_both_apply(governed):
    svc, agent, transport, clock = governed
    identify(svc.store, "t:v", "second@example.com")
    identify(svc.store, "t:w", "third@example.com")
    edit(svc, FLEX, {"token_limit": 60})
    edit(svc, ["flex", "second@example.com"], {"token_limit": 200})
    edit(svc, ["pools", "flex"], {"limit": 150})
    clock.set("2026-12-01T00:00:00")
    group_turn(svc, transport, "u", "chat", "FlexTier first user")
    group_turn(svc, transport, "u", "chat", "personal cap reached")
    assert len(agent.calls) == 1
    assert any(
        "your token limit" in (message or "") for message in texts(transport, GROUP)
    )
    group_turn(svc, transport, "v", "chat-v", "FlexTier second user")
    group_turn(svc, transport, "v", "chat-v", "pool cap reached")
    assert len(agent.calls) == 2
    assert any(
        "shared token pool" in (message or "") for message in texts(transport, GROUP)
    )
    group_turn(svc, transport, "w", "chat-w", "FreeTier still available")
    assert len(agent.calls) == 3
    edit(svc, ["flex", "second@example.com"], {"enabled": False})
    group_turn(svc, transport, "v", "chat-v", "FreeTier after switching")
    pools = edit(svc, [], method="GET")["pools"]
    assert (pools["free"]["used"], pools["flex"]["used"]) == (200, 200)
    edit(svc, ["flex", "second@example.com"], {"enabled": True})
    assert usage(svc, "t:v")[1] == (100, 0)


def test_shared_limits_apply_now_and_remain_after_reset(governed):
    svc, _, _, clock = governed
    edit(svc, ["pools", "free"], {"limit": 900})
    edit(svc, ["pools", "flex"], {"limit": 0})
    data = edit(svc, [], method="GET")
    assert data["pools"]["free"]["limit"] == 900
    assert data["pools"]["flex"]["limit"] == 0
    clock.set("2026-12-01T00:00:00")
    data = edit(svc, [], method="GET")
    assert data["pools"]["free"]["limit"] == 900
    assert data["pools"]["flex"]["limit"] == 0
    edit(svc, ["pools", "flex"], {"limit": None})
    assert edit(svc, [], method="GET")["pools"]["flex"]["limit"] is None
    clock.set("2027-01-01T00:00:00")
    assert edit(svc, [], method="GET")["pools"]["flex"]["limit"] is None


def test_existing_scheduled_caps_stay_scheduled_until_explicitly_saved(governed):
    svc, _, _, clock = governed
    with svc.store.engine.begin() as conn:
        policy, _ = svc.governance.policy(conn)
        policy["pending"] = dict(
            cadence="monthly", free_limit=250, effective_on="2026-12-01"
        )
        policy["pools"] = {
            "free": {
                "limit": 1000,
                "adjustment": {"limit": 900, "cycle_start": "2026-11-01"},
                "pending": {"limit": 1200, "effective_on": "2026-12-01"},
            }
        }
        svc.governance.save(conn, policy)
        conn.execute(
            insert(governance_flex).values(email=EMAIL, enabled=True, pending_limit=20)
        )
    data = edit(svc, ["quota"], {"cadence": "weekly"})
    assert data["quota"]["free_limit"] == 500
    assert data["quota"]["pending"]["free_limit"] == 250
    assert data["pools"]["free"]["limit"] == 900 and data["pools"]["free"]["adjusted"]
    assert data["flex"][0]["pending_limit"] == 20
    edit(svc, ["quota"], {"free_limit": 250})
    edit(svc, ["pools", "free"], {"limit": 1200})
    assert usage(svc)[0]["limit"] == 250
    assert usage(svc)[0]["pool_limit"] == 1200
    clock.set("2026-12-01T00:00:00")
    assert usage(svc)[0]["limit"] == 20
    assert usage(svc)[0]["resets_on"] == "2026-12-07"
    assert edit(svc, [], method="GET")["pools"]["free"]["limit"] == 1200


def test_audit_records_actor_and_changes_and_pages_without_duplicates(governed):
    svc, _, _, _ = governed
    second = {
        "owner": "t:other-admin",
        "email": "other-admin@example.com",
        "role": "admin",
    }
    edit(svc, ["quota"], {"free_limit": 70, "cadence": "weekly"})
    edit(svc, FLEX, {"token_limit": 40}, actor=second)
    edit(svc, FLEX, {"enabled": False}, actor=second)
    for limit in range(27):
        edit(svc, ["pools", "flex"], {"limit": limit})
    edit(svc, ["pools", "flex"], {"limit": 26})  # no-op
    with pytest.raises(AccessError):
        edit(svc, ["pools", "flex"], {"limit": -1})
    recent = edit(svc, ["history"], method="GET")
    older = edit(
        svc, ["history"], method="GET", query="before=" + recent["next_before"]
    )
    rows = recent["items"] + older["items"]
    assert len(recent["items"]) == 25
    assert len(rows) == len({row["id"] for row in rows}) == 31
    assert [int(row["id"]) for row in rows] == sorted(
        (int(row["id"]) for row in rows), reverse=True
    )
    assert older["next_before"] is None
    assert rows[0]["before"] == {"limit": 25} and rows[0]["after"] == {"limit": 26}
    assert rows[0]["occurred_at"].endswith("+00:00")
    assert rows[-4]["actor_email"] == "other-admin@example.com"
    assert rows[-4]["target"] == EMAIL
    assert rows[-4]["before"]["enabled"] is True
    assert rows[-4]["after"]["enabled"] is False
    assert rows[-2]["after"]["pending_cadence"] == "weekly"
    assert rows[-1]["before"] == {"free_limit": 500}
    assert rows[-1]["after"] == {"free_limit": 70}


def test_audit_and_policy_save_are_atomic(governed, monkeypatch):
    svc, _, _, _ = governed
    usage(svc)

    def fail(*_args):
        raise RuntimeError("synthetic audit write failure")

    monkeypatch.setattr(svc.governance, "record_change", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        edit(svc, ["quota"], {"free_limit": 95})
    assert usage(svc)[0]["limit"] == 500


def test_new_pools_include_current_legacy_user_usage_and_exclude_system(governed):
    svc, _, _, _ = governed
    with svc.store.engine.begin() as conn:
        for key, kind, count in [("user", "p2p", 80), ("system", "system", 900)]:
            conn.execute(
                insert(token_usage).values(
                    session_id=key,
                    job_id=key,
                    requester=key,
                    principal=key,
                    chat_type=kind,
                    input_tokens=count,
                    month=date(2026, 11, 1),
                )
            )
    pools = edit(svc, [], method="GET")["pools"]
    assert pools["free"]["limit"] is None and pools["flex"]["limit"] is None
    assert pools["free"]["used"] == 80 and pools["flex"]["used"] == 0


def test_configured_reasoning_options_and_native_wire_shape(monkeypatch):
    config = load(
        monkeypatch,
        HERMES_MODEL="model-b",
        MODEL_REASONING_EFFORTS='{"model-b":["high","low"]}',
    )
    assert config.model_reasoning_efforts == {"model-b": ("low", "high")}
    with pytest.raises(ValueError, match="MODEL_REASONING_EFFORTS"):
        load(
            monkeypatch,
            HERMES_MODEL="model-b",
            MODEL_REASONING_EFFORTS='{"model-b":["unknown"]}',
        )
    hermes = adapter(Runs())
    request = dict(history=[], message=dict(content="question"), model="model-b")
    assert "model_options" not in hermes.build(request)
    body = hermes.build(dict(request, reasoning_effort="high"))
    assert body["model"] == "model-b"
    assert body["model_options"] == {"reasoning_effort": "high"}
