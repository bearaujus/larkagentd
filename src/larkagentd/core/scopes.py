"""Which tools a registered group may use.

A scope only narrows: a tool must pass the registry and be enabled for the group. The pieces that
the Gateway, the dashboard and the tool service share live here so they cannot drift.
"""

import re

# Always on in every group and in no scope list. The Group Brain tools are here because a group's
# Group Brain switch, not its tool list, decides whether they answer.
BASELINE = frozenset(
    (
        "list_skills",
        "read_skill",
        "run_confirmed_action",
        "group_search_discussions",
        "group_get_discussion",
        "group_knowledge_search",
        "group_knowledge_sources",
        "group_memory_propose",
        "group_memory_correct",
    )
)
DEFAULT_GROUP_TOOLS = frozenset(
    (
        "lark_thread",
        "lark_read",
        "lark_image",
        "lark_file_read",
        "lark_file_create",
        "lark_file_edit",
        "request_web_search",
        "search_group_knowledge",
    )
)


def is_scopable(name):
    return name not in BASELINE


def group_chat_scope(chat_id):
    """The policy scope of a group chat id, or None when the id could not be one."""
    if not isinstance(chat_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,256}", chat_id
    ):
        return None
    return "group:" + chat_id
