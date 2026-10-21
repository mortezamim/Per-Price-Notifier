#!/usr/bin/env python3
"""List Telegram groups seen by bot without printing messages or user data."""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("error: TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1
    request = Request(
        f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=100",
        headers={"Accept": "application/json", "User-Agent": "Per-Price-Notifier/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"error: Telegram getUpdates failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        print("error: Telegram rejected getUpdates", file=sys.stderr)
        return 1
    groups: set[tuple[str, int]] = set()
    for update in payload.get("result", []):
        if not isinstance(update, dict):
            continue
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            event = update.get(key)
            if not isinstance(event, dict):
                continue
            chat = event.get("chat")
            if not isinstance(chat, dict) and key == "my_chat_member":
                chat = event.get("chat")
            if (
                isinstance(chat, dict)
                and chat.get("type") in {"group", "supergroup"}
                and isinstance(chat.get("id"), int)
            ):
                groups.add((str(chat.get("title") or "(untitled group)"), chat["id"]))
    if not groups:
        print("No group updates found. Send /start@Per_Price_Notifier_bot in the group, then retry.")
        return 2
    for title, chat_id in sorted(groups):
        print(f"Group: {title} | Chat ID: {chat_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
