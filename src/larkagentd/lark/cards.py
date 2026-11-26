"""Compact Card 1.0 layouts, shared/updateable through the message PATCH API."""

import json

from .identity_display import entity_display, principal_display

# Reserve room for the card shell under Lark's 30 KB message-card limit.
OPTION_BYTES = 24000


def choice_pages(choices):
    pages, current, size = [], [], 2
    for item in sorted(choices, key=lambda x: (x["label"].casefold(), x["id"])):
        option = {
            "text": {"tag": "plain_text", "content": item["label"][:80]},
            "value": item["id"],
        }
        cost = len(json.dumps(option, ensure_ascii=False).encode("utf-8")) + 2
        if current and size + cost > OPTION_BYTES:
            pages.append(current)
            current, size = [], 2
        current.append(item)
        size += cost
    return pages + [current] if current else pages or [[]]


def card(state, note=""):
    version = state.get("card_version", "")
    elements = []

    def text(value):
        return {"tag": "plain_text", "content": str(value)}

    def paragraph(value):
        elements.append({"tag": "div", "text": text(value)})

    def hint(value):
        elements.append({"tag": "note", "elements": [text(value)]})

    def button(label, action, primary=False, **extra):
        return {
            "tag": "button",
            "text": text(label),
            "type": "primary" if primary else "default",
            "value": dict(action=action, version=version, **extra),
        }

    def connection_summary():
        # One full-width field group keeps long principals readable without large gaps.
        elements.append(
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": False,
                        "text": text("Entity: " + entity_display(state)),
                    },
                    {
                        "is_short": False,
                        "text": text("RAM: " + principal_display(identity)),
                    },
                ],
            }
        )

    def actions(items):
        elements.append({"tag": "action", "actions": items})

    stage = state.get("stage", "")
    identity = state.get("identity")
    title, color = "Connected account", "blue"
    if state.get("card_closed"):
        title = state.get("closed_title", "Setup closed")
        color = "green" if title == "Connected" else "grey"
        if title == "Connected" and identity:
            connection_summary()
        paragraph(
            note or state.get("card_note") or "Open setup to manage your connection."
        )
        if state.get("setup_reopen_token"):
            actions(
                [
                    button(
                        "Open setup",
                        "open_setup",
                        True,
                        reopen_token=state["setup_reopen_token"],
                    )
                ]
            )
        hint("Or type /setup.")
    elif state.get("auth_job"):
        title = "Updating connection"
        paragraph("Checking your connection...")
        hint("Your chat is saved.")
        actions([button("Cancel", "cancel")])
    elif stage == "loading_accounts":
        title, color = "Signed in", "green"
        paragraph("Loading your entities...")
        if state.get("auth_retry_notice"):
            hint("Connection is slow. Retrying...")
        actions([button("Cancel", "cancel")])
    elif stage == "device":
        title = "Sign in to Connected account"
        hint("Step 1 of 3 • Sign in")
        paragraph("Open the sign-in page, then enter this code:")
        paragraph(state["user_code"])
        actions(
            [
                {
                    "tag": "button",
                    "text": text("Open sign-in"),
                    "type": "primary",
                    "url": state["verification_uri"],
                },
                button("Cancel", "cancel"),
            ]
        )
        hint(
            "Connection is slow. Retrying..."
            if state.get("auth_retry_notice")
            else "This card updates after sign-in."
        )
    elif stage in ("accounts", "roles"):
        account = stage == "accounts"
        title = "Choose an entity" if account else "Choose a role"
        hint("Step 2 of 3 • Entity" if account else "Step 3 of 3 • Role")
        if not account:
            paragraph(
                "Entity: "
                + entity_display(
                    {
                        "account": state.get("pending_account"),
                        "account_label": state.get("pending_account_label"),
                    }
                )
            )
        choices = state.get("choices", [])
        pages = choice_pages(choices)
        page = max(0, min(state.get("page", 0), len(pages) - 1))
        shown = pages[page]
        if shown:
            actions(
                [
                    {
                        "tag": "select_static",
                        "placeholder": text(
                            "Select an entity" if account else "Select a role"
                        ),
                        "options": [
                            {"text": text(x["label"][:80]), "value": x["id"]}
                            for x in shown
                        ],
                        "value": {
                            "action": "account" if account else "role",
                            "version": version,
                        },
                    }
                ]
            )
            hint(
                "Choose an entity to continue."
                if account
                else "Choose a role to connect."
            )
        else:
            paragraph(
                "No entities available. Try another login or ask your administrator."
                if account
                else "No roles available. Choose another entity or ask your administrator."
            )
        nav = []
        if account and not shown:
            nav.append(button("Sign in again", "connect", True))
        if page:
            nav.append(button("Previous", "page", page=page - 1))
        if page + 1 < len(pages):
            nav.append(button("Next", "page", page=page + 1))
        if len(pages) > 1:
            hint(f"Page {page + 1} of {len(pages)} • {len(choices)} options")
        if not account:
            nav.append(button("Back", "switch"))
        actions(nav + [button("Cancel", "cancel")])
    elif identity:
        title, color = "Connection settings", "blue"
        connection_summary()
        hint("Temporary credentials renew while your sign-in is valid.")
        if note:
            paragraph(note)
        elements.append({"tag": "hr"})
        actions(
            [
                button("Change entity / role", "switch", True),
                button("Done", "close"),
            ]
        )
        disconnect = button("Disconnect", "disconnect")
        disconnect["confirm"] = {
            "title": text("Disconnect from Connected account?"),
            "text": text(
                "Your chat stays saved. You'll need to sign in to connect again."
            ),
        }
        actions([button("Sign in again", "connect"), disconnect])
    else:
        title = (
            "Hi! Let's get started" if state.get("welcome") else "Connect your account"
        )
        if state.get("welcome"):
            paragraph("I'm your group assistant. Connect your account to get started.")
            paragraph("Sign in, then choose an entity and role.")
        else:
            paragraph(note or "Connect your account to get started.")
        actions([button("Connect", "connect", True), button("Not now", "close")])
        hint(
            "Sign in with your account provider, not in this chat. Return with /setup."
        )
    return {
        "config": {"wide_screen_mode": False, "update_multi": True},
        "header": {"title": text(title), "template": color},
        "elements": elements,
    }


def json_card(state, note=""):
    return json.dumps(card(state, note), ensure_ascii=False)
