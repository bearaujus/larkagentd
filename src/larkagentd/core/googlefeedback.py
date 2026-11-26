"""Google reading failures expressed as user actions, independent of transport/auth."""

import json
import re


def valid_email(value):
    if isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.gserviceaccount\.com", value
    ):
        return value
    return None


def configured_email(raw):
    """Extract only the public sharing identity; never retain/return other JSON fields."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("type") == "service_account":
            return valid_email(data.get("client_email"))
    except (ValueError, TypeError, RecursionError):
        pass
    return None


GROUP_DISABLED_MESSAGE = (
    "I can't open Google files in this group yet. Send me the link in a direct message, "
    "or ask a bot administrator to enable Google Docs and Sheets for this group. "
)


def group_disabled_message(email=None):
    email = valid_email(email)
    if email:
        return GROUP_DISABLED_MESSAGE + (
            f"My Google service account is {email}; I haven't checked its access to this file yet."
        )
    return (
        GROUP_DISABLED_MESSAGE + "I haven't checked this file's sharing settings yet."
    )


def refusal(code, email=None):
    email = valid_email(email)
    result = {"ok": False, "error": code, "retryable": False}
    if code == "not_configured":
        result["guidance"] = (
            "Ask the bot administrator to set GOOGLE_SERVICE_ACCOUNT_JSON in the bot's .env "
            "to the service account JSON key, then restart the bot. Never paste the key into chat. "
            "Then share the Google file with that key's client_email as Viewer."
        )
    elif code in ("forbidden", "not_found") and email:
        result["service_account_email"] = email
        result["guidance"] = (
            (
                "Google denied access. "
                if code == "forbidden"
                else "Google could not find this file or it is not shared with the bot. "
            )
            + f"Ask the file owner to open Share -> add {email} -> Viewer -> Share, then resend the link. "
            "This is the service account configured in GOOGLE_SERVICE_ACCOUNT_JSON. "
            "If sharing is blocked by your organization, ask your Workspace administrator to allow it. "
            "Account setup does not grant Google access."
        )
    elif code == "api_disabled":
        result["guidance"] = (
            "Ask the bot administrator to enable the Google Sheets API and Google Docs API in the service account's Google Cloud project, then retry."
        )
    elif code == "unauthorized":
        result["guidance"] = (
            "Ask the bot administrator to check or replace GOOGLE_SERVICE_ACCOUNT_JSON and restart the bot. The service account key may be invalid or revoked. Never paste the key into chat."
        )
    elif code in ("rate_limited", "google_unavailable"):
        result.update(
            retryable=True,
            guidance="Google could not complete this request. Try again later; this does not establish a document sharing problem.",
        )
    messages = {
        "not_enabled_for_group": group_disabled_message(email),
        "tool_not_allowed": "Google document reading isn't available for your account right now. Ask a bot administrator to enable it; I haven't checked this file's sharing settings yet.",
        "tool_unavailable": "Google document reading isn't available right now. Ask a bot administrator to check it; I haven't checked this file's sharing settings yet.",
        "not_configured": "Google document reading hasn't been set up for this bot yet. Ask a bot administrator to finish the connection, then send me the link again.",
        "unauthorized": "I couldn't connect to Google with the bot's account. Ask a bot administrator to check the connection, then send me the link again.",
        "api_disabled": "The bot's Google connection needs one more setup step. Ask a bot administrator to enable Google Docs and Sheets access, then send me the link again.",
        "rate_limited": "Google is limiting requests right now. Please try sending the link again in a little while.",
        "google_unavailable": "I couldn't reach Google to open the file. Please try sending the link again in a little while.",
        "link_not_from_user": "Please include the Google file link in your current message so I can open the right file.",
        "bad_link": "Please send the full Google Docs or Sheets link copied from the file's Share dialog.",
        "bad_range": "I couldn't read that part of the spreadsheet. Please check the tab name and ask for a smaller range, up to 100 rows and 30 columns.",
        "result_too_large": "This file is too large for me to read in one request. For a spreadsheet, ask for a smaller range; for a document, paste the relevant section here.",
    }
    if code in ("forbidden", "not_found") and email:
        opening = (
            "Google hasn't allowed me to open this file. "
            if code == "forbidden"
            else "I couldn't open this file. The link may be incorrect, or the file may not be shared with the bot. "
        )
        result["user_message"] = opening + (
            f"Ask the owner to open Share, add {email} as a Viewer, and save. "
            "Then send me the link again."
        )
    elif code in messages:
        result["user_message"] = messages[code]
    if code == "not_enabled_for_group" and email:
        result["service_account_email"] = email
    result["response_guidance"] = (
        "Use user_message as the basis of a brief reply in the user's language. "
        "Include the configured account email when present, including for a group restriction, but do not show tool names, error codes, "
        "configuration variable names, a check table or a diagnostic report. "
        "guidance contains administrator troubleshooting details; explain them only when the user asks for technical setup help. "
        "A group-policy refusal happens before any Google request: it does not verify the service account or file sharing. "
        "Never promise that enabling a group setting guarantees the file can be read."
    )
    return result
