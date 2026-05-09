"""GitHub REST client with token auth and basic rate-limit awareness.

We deliberately use httpx directly instead of `gh` CLI: shelling out to gh 2.4.0
loses pagination control and adds startup overhead per call. Auth via GH_TOKEN.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

API = "https://api.github.com"
PER_PAGE = 100


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        self._token = token or os.environ.get("GH_TOKEN")
        if not self._token:
            raise GitHubError("GH_TOKEN env var is required")
        self._client = httpx.AsyncClient(
            base_url=API,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "inference-radar/0.1",
            },
            timeout=timeout,
        )

    async def __aenter__(self) -> GitHub:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        resp = await self._client.get(url, params=params)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = resp.headers.get("X-RateLimit-Reset", "?")
            raise GitHubError(f"rate-limited; resets at {reset}")
        if resp.status_code >= 400:
            raise GitHubError(f"GET {url} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    async def paginate(
        self, url: str, params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        params = {**(params or {}), "per_page": PER_PAGE}
        while url:
            resp = await self._get(url, params=params)
            for item in resp.json():
                yield item
            link = resp.headers.get("Link", "")
            url = _parse_next(link)
            params = None  # subsequent URLs already include per_page

    async def list_issues(
        self, repo_slug: str, since: str | None = None, state: str = "open",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield issues + PRs (GH lumps them in /issues). Caller filters PRs by `pull_request` key.

        `since` is ISO8601; GitHub returns issues updated at or after.
        """
        params: dict[str, Any] = {"state": state, "sort": "updated", "direction": "desc"}
        if since:
            params["since"] = since
        async for item in self.paginate(f"/repos/{repo_slug}/issues", params=params):
            yield item

    async def list_pulls(
        self, repo_slug: str, state: str = "all",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield PRs. Pulls have richer metadata than the /issues endpoint version."""
        params = {"state": state, "sort": "updated", "direction": "desc"}
        async for item in self.paginate(f"/repos/{repo_slug}/pulls", params=params):
            yield item


def _parse_next(link_header: str) -> str:
    """Extract the rel=next URL from a Link header, or '' if missing."""
    if not link_header:
        return ""
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' in section:
            start = section.index("<") + 1
            end = section.index(">")
            return section[start:end]
    return ""
