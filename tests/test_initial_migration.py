from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from test_core import store as store_fixture
from test_dashboard import change

from larkagentd.core.store import gateway_policies, metadata
from larkagentd.gateway.access import Access
from larkagentd.knowledge.library import sources
from larkagentd.knowledge.models import GroupBrainConfig
from larkagentd.knowledge.policy import effective

store = store_fixture
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def configuration(store, monkeypatch):
    with store.engine.begin() as conn:
        schema = conn.scalar(text("SELECT current_schema()"))
    metadata.drop_all(store.engine)
    monkeypatch.setenv(
        "DATABASE_URL",
        store.engine.url.update_query_dict(
            {"options": "-csearch_path=" + schema}
        ).render_as_string(hide_password=False),
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_head_matches_the_model_in_a_fresh_database(store, configuration):
    command.upgrade(configuration, "head")
    command.check(configuration)
    with store.engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        assert conn.scalar(select(gateway_policies.c.scope)) is None
    assert set(inspect(store.engine).get_table_names()) == set(metadata.tables) | {
        "alembic_version"
    }
    assert "learning_mode" not in {
        column["name"]
        for column in inspect(store.engine).get_columns("group_knowledge_settings")
    }
    command.downgrade(configuration, "base")
    assert set(inspect(store.engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(configuration, "head")


def test_group_setup_and_workspace_sources_have_distinct_scope(store, configuration):
    command.upgrade(configuration, "head")
    config = GroupBrainConfig(enabled=True)
    access = Access(store, ("admin@example.com",), group_brain=config)
    change(access, "POST", "groups", {"group_id": "oc_demo"})
    change(
        access,
        "POST",
        "knowledge/sources",
        {"name": "Workspace guide", "markdown": "Pilot checklist."},
    )
    change(
        access,
        "POST",
        "groups/oc_demo/knowledge/sources",
        {"name": "Group guide", "markdown": "Pilot schedule."},
    )
    with store.engine.connect() as conn:
        assert effective(conn, config, "group:oc_demo").allows("recall")
        assert conn.execute(
            select(sources.c.scope, sources.c.group_scope).order_by(sources.c.scope)
        ).all() == [("group:oc_demo", "group:oc_demo"), ("workspace", None)]
    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(insert(gateway_policies).values(scope="dm", mode="all_users"))
    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(
            insert(sources).values(
                id="missing", scope="group:missing", name="Missing", markdown="Invalid"
            )
        )


def test_rerunning_alembic_preserves_existing_sources(store, configuration):
    command.upgrade(configuration, "head")
    with store.engine.begin() as conn:
        conn.execute(
            sources.insert(),
            [
                dict(
                    scope="workspace",
                    name="Guide",
                    id="legacy",
                    markdown="Office hours",
                    server=None,
                    uri=None,
                ),
                dict(
                    scope="workspace",
                    name="Feed",
                    id="remote",
                    markdown="",
                    server="news",
                    uri="data:news",
                ),
            ],
        )
    command.upgrade(configuration, "head")
    with store.engine.begin() as conn:
        assert conn.execute(
            select(
                sources.c.id, sources.c.markdown, sources.c.server, sources.c.uri
            ).order_by(sources.c.id)
        ).all() == [
            ("legacy", "Office hours", None, None),
            ("remote", "", "news", "data:news"),
        ]
