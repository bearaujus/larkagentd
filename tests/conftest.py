import os

import pytest

# Everything the Gateway, the tool service and the dashboard read from the environment. A
# developer's shell usually has the real values (storage keys, repository tokens, Lark and
# model credentials). A test that reaches them is not a test of the code: it is slow, it
# depends on who runs it, and it can contact real services with real credentials. A test
# that needs a setting sets it itself.
SETTINGS = (
    "AGENT_",
    "GROUP_BRAIN_",
    "LARKAGENTD_",
    "KNOWLEDGE_",
    "BRAVE_SEARCH_",
    "CREDENTIAL_",
    "DASHBOARD_",
    "GITLAB_",
    "GOOGLE_",
    "HERMES_",
    "LARK_",
    "MCP_",
    "TOOLKIT_",
    "TOOL_SERVICE_",
)
EXACT = (
    "AVAILABLE_MODELS",
    "DATABASE_URL",
    "USER_TOKEN_QUOTA",
    "HEALTH_PORT",
    "HISTORY_MESSAGES",
    "LOG_FORMAT",
    "LOG_LEVEL",
    "LOG_STATUS_INTERVAL",
    "MESSAGE_BATCH_SECONDS",
    "TOKEN_SUMMARY",
)


@pytest.fixture(autouse=True)
def no_real_settings(monkeypatch):
    for name in list(os.environ):
        if name.startswith(SETTINGS) or name in EXACT:
            monkeypatch.delenv(name)


class AllowRegistry:
    """An explicit unit-test policy. Production requires a real ToolRegistry."""

    def decide(self, owner, name):
        from larkagentd.toolservice.registry import Decision

        return Decision(True, "ok")
