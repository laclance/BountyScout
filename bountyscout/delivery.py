"""Outbound notification and report delivery transports.

Owns Telegram, Discord, and GitHub report HTTP delivery.
It does not render reports, rank candidates, or manage seen-state.
"""

from __future__ import annotations

import json
import urllib.request


def send_telegram_notification(token: str, chat_id: str, message: str) -> bool:
    """Send a notification message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("Telegram notification sent successfully.")
            return True
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")
        return False


def send_discord_notification(webhook_url: str, message: str) -> bool:
    """Send a notification message via Discord Webhook."""
    payload = {"content": message}
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("Discord notification sent successfully.")
            return True
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")
        return False


def create_github_issue(repo_fullname: str, token: str, title: str, body: str) -> bool:
    """Create a native GitHub scan report and immediately close it as not planned."""
    url = f"https://api.github.com/repos/{repo_fullname}/issues"
    payload = {
        "title": title,
        "body": body,
        "labels": ["bounty-alert"],
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "MyPersonalBountyScout",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            created = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"Failed to create GitHub Issue notification: {e}")
        return False

    issue_url = created.get("url") if isinstance(created, dict) else None
    if not isinstance(issue_url, str) or not issue_url:
        print("Failed to auto-close GitHub Issue notification: created issue URL missing.")
        return False

    close_payload = {
        "state": "closed",
        "state_reason": "not_planned",
    }
    close_req = urllib.request.Request(
        issue_url,
        data=json.dumps(close_payload).encode("utf-8"),
        headers=headers,
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(close_req, timeout=15):
            print("GitHub Issue notification created and auto-closed successfully.")
            return True
    except Exception as e:
        print(f"Failed to auto-close GitHub Issue notification: {e}")
        return False
