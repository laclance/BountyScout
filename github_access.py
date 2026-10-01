"""Shared GitHub HTTP access and per-scan cache primitives.

The fork-specific scanner uses this module for GitHub JSON transport and keyed cache
fills. It deliberately does not own scanner policy, ranking, or long-lived state.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from threading import Lock
from typing import Any, TypeVar

import scout_bounties as bounty

T = TypeVar("T")


class KeyedLockPool:
    """Provide stable per-key locks while allowing unrelated cache fills in parallel."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[str, Lock] = {}

    def lock_for(self, key: str) -> Lock:
        """Return one shared lock for a cache key."""
        with self._guard:
            return self._locks.setdefault(key, Lock())


def cached_value(
    cache: MutableMapping[str, T],
    key: str,
    loader: Callable[[], T],
    locks: KeyedLockPool,
    *,
    namespace: str,
) -> T:
    """Load a missing cache value once, serialized only against the same key."""
    if key in cache:
        return cache[key]

    with locks.lock_for(f"{namespace}:{key}"):
        if key not in cache:
            cache[key] = loader()
        return cache[key]


def github_get(
    url: str,
    token: str | None = None,
    timeout: int = 20,
    *,
    log_errors: bool = True,
) -> Any:
    """Fetch JSON from GitHub with the scanner's standard API headers."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OSSOpportunityScout",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        if log_errors:
            print(f"GitHub API Error for {url}: {exc}")
        return None


def repo_metadata(repo: str, token: str | None) -> dict[str, Any]:
    """Fetch lightweight repository metadata used for filtering and ranking."""
    data = github_get(f"https://api.github.com/repos/{repo}", token)
    return data if isinstance(data, dict) else {}


def issue_comments(
    item: Mapping[str, Any],
    token: str | None,
) -> list[dict[str, Any]]:
    """Fetch all currently exposed issue comments used by strategic verification."""
    repo, number = bounty.issue_repo_and_number(item)
    if not repo or not number or not int(item.get("comments") or 0):
        return []

    comments = github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100",
        token,
    )
    return comments if isinstance(comments, list) else []


def contribution_guide(
    repo: str,
    token: str | None,
    getter: Callable[[str, str | None], Any] | None = None,
) -> str | None:
    """Return the first contribution guide found at common repository paths."""
    load = getter or (lambda url, auth: github_get(url, auth, timeout=10, log_errors=False))
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"):
        data = load(
            f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}",
            token,
        )
        if isinstance(data, dict) and data.get("html_url"):
            return str(data["html_url"])
    return None


def issue_from_github_url(url: str, token: str | None) -> dict[str, Any] | None:
    """Fetch a GitHub issue object from its canonical issue URL."""
    match = bounty.issue_repo_and_number({"html_url": str(url)})
    repo, number = match
    if not repo or not number:
        return None
    item = github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        token,
    )
    return item if isinstance(item, dict) else None
