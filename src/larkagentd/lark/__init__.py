"""The Lark side of the Gateway: everything that talks to Lark or shapes what the person sees.

The transport (`adapter`: send, edit, react, download, the WebSocket), parsing what arrives
(`inbound`, `attachments`), how answers and progress look (`render`, `progress`, `card`, `cards`,
`card_delivery`, `identity_display`, `usage_text`) and the fixed wording (`ui_text`).

`gateway/` (the daemon, the service flows, the Hermes client) uses this package; nothing here may
import from `gateway/`. The agent sees the same rules from `ui_text` through `gateway/hermes.py`.
"""
