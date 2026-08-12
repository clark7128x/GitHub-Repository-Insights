import hashlib
import json
import logging
import os
import time
from datetime import UTC
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API_URL = "https://api.github.com"
REDIS_URL = os.getenv("REDIS_URL", "")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # 15 minutes default
GITHUB_REQUEST_TIMEOUT = float(os.getenv("GITHUB_REQUEST_TIMEOUT", "30.0"))

# Rate-limit backoff settings
RATE_LIMIT_MAX_WAIT = int(os.getenv("RATE_LIMIT_MAX_WAIT", "120"))  # seconds


class GitHubRateLimitError(Exception):
    """Raised when the GitHub API rate limit is exceeded."""

    def __init__(self, reset_at: int | None = None, message: str = ""):
        self.reset_at = reset_at
        super().__init__(message or "GitHub API rate limit exceeded")


# ---------------------------------------------------------------------------
# Redis cache helpers
# ---------------------------------------------------------------------------

_redis_client = None


def get_redis():
    """Lazily connect to Redis. Returns None if REDIS_URL is not set."""
    global _redis_client
    if _redis_client is None and REDIS_URL:
        try:
            import redis

            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception:
            logger.warning("Redis unavailable — caching disabled")
            _redis_client = None
    return _redis_client


def _cache_key(url: str, params: dict | None = None) -> str:
    raw = url + json.dumps(params or {}, sort_keys=True)
    return "ghcache:" + hashlib.sha256(raw.encode()).hexdigest()


def _get_cached(url: str, params: dict | None = None):
    r = get_redis()
    if r:
        try:
            cached = r.get(_cache_key(url, params))
            if cached:
                return json.loads(cached)
        except Exception:
            pass
    return None


def _set_cached(url: str, data, params: dict | None = None):
    r = get_redis()
    if r:
        try:
            r.setex(_cache_key(url, params), CACHE_TTL, json.dumps(data))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Core HTTP helper with caching, timeouts, and rate-limit handling
# ---------------------------------------------------------------------------


def get_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


def _check_rate_limit(response: httpx.Response) -> None:
    """Inspect rate-limit headers and raise or sleep as appropriate."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset_ts = response.headers.get("X-RateLimit-Reset")

    if response.status_code in (403, 429):
        # Determine whether this is a rate-limit 403 (vs. a permissions 403)
        is_rate_limit = (
            response.status_code == 429
            or (remaining is not None and int(remaining) == 0)
            or "rate limit" in response.text.lower()
        )
        if is_rate_limit:
            reset_at = int(reset_ts) if reset_ts else None
            if reset_at:
                wait = max(0, reset_at - int(time.time()))
                if wait <= RATE_LIMIT_MAX_WAIT:
                    logger.warning(
                        "Rate limit hit — waiting %d seconds for reset", wait
                    )
                    time.sleep(wait + 1)
                    return  # caller should retry
                else:
                    raise GitHubRateLimitError(
                        reset_at=reset_at,
                        message=(
                            f"GitHub API rate limit exceeded. "
                            f"Resets in {wait}s (max wait {RATE_LIMIT_MAX_WAIT}s). "
                            f"Set GITHUB_TOKEN to raise the limit to 5,000/hr."
                        ),
                    )
            raise GitHubRateLimitError(
                message=(
                    "GitHub API rate limit exceeded (no reset time available). "
                    "Set GITHUB_TOKEN to raise the limit to 5,000/hr."
                ),
            )


def github_get(url: str, params: dict | None = None, use_cache: bool = True):
    """
    Central helper: cache lookup → HTTP GET → rate-limit check → cache store.
    Retries once after a rate-limit wait.
    """
    if use_cache:
        cached = _get_cached(url, params)
        if cached is not None:
            return cached

    max_retries = 2
    for attempt in range(max_retries):
        response = httpx.get(
            url,
            headers=get_headers(),
            params=params,
            follow_redirects=True,
            timeout=httpx.Timeout(GITHUB_REQUEST_TIMEOUT),
        )

        # _check_rate_limit may sleep (retry), or raise GitHubRateLimitError.
        _check_rate_limit(response)

        if response.status_code in (403, 429) and attempt < max_retries - 1:
            # _check_rate_limit slept — retry the request
            continue

        response.raise_for_status()

        data = response.json()
        if use_cache:
            _set_cached(url, data, params)
        return data

    # Should not reach here, but just in case
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# GitHub API functions
# ---------------------------------------------------------------------------


def fetch_commits(repo: str, per_page: int = 100):
    url = f"{GITHUB_API_URL}/repos/{repo}/commits"
    params = {"per_page": per_page}
    return github_get(url, params=params)


def fetch_commit_details(repo: str, ref: str):
    url = f"{GITHUB_API_URL}/repos/{repo}/commits/{ref}"
    return github_get(url)


def fetch_pull_requests(repo: str, state: str = "closed", per_page: int = 100):
    url = f"{GITHUB_API_URL}/repos/{repo}/pulls"
    params = {"state": state, "per_page": per_page}
    return github_get(url, params=params)


# ---------------------------------------------------------------------------
# Metric calculations
# ---------------------------------------------------------------------------


def calculate_bus_factor(commits: list[dict[str, Any]]) -> int:
    """Calculate the minimum number of developers responsible for 50% of the commits."""
    author_counts: dict[str, int] = {}
    total_commits = len(commits)

    if total_commits == 0:
        return 0

    for commit in commits:
        author = "unknown"
        if commit.get("author") and commit["author"].get("login"):
            author = commit["author"]["login"]
        elif commit.get("commit") and commit["commit"].get("author"):
            author = commit["commit"]["author"].get("name", "unknown")

        author_counts[author] = author_counts.get(author, 0) + 1

    sorted_authors = sorted(author_counts.values(), reverse=True)

    cumulative = 0
    bus_factor = 0
    target = total_commits * 0.5

    for count in sorted_authors:
        cumulative += count
        bus_factor += 1
        if cumulative >= target:
            break

    return bus_factor


def calculate_hot_files(
    repo: str, commits: list[dict[str, Any]], limit: int = 30
) -> list[dict[str, Any]]:
    """
    Fetch details for the latest `limit` commits and return the most
    frequently modified files.  Raises GitHubRateLimitError instead of
    silently swallowing rate-limit errors.
    """
    file_counts: dict[str, int] = {}
    commits_to_check = commits[:limit]

    for commit in commits_to_check:
        # GitHubRateLimitError propagates — no bare except
        details = fetch_commit_details(repo, commit["sha"])
        for file_info in details.get("files", []):
            filename = file_info.get("filename")
            if filename:
                file_counts[filename] = file_counts.get(filename, 0) + 1

    sorted_files = sorted(file_counts.items(), key=lambda x: x[1], reverse=True)
    return [{"file": k, "changes": v} for k, v in sorted_files[:10]]


def calculate_avg_pr_lifetime(prs: list[dict[str, Any]]) -> float | None:
    """Calculate average PR lifetime in hours. Returns None if no valid PRs."""
    from datetime import datetime

    total_hours = 0.0
    valid_prs = 0

    for pr in prs:
        created_at_str = pr.get("created_at")
        closed_at_str = pr.get("closed_at") or pr.get("merged_at")

        if created_at_str and closed_at_str:
            created_at = datetime.strptime(created_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            closed_at = datetime.strptime(closed_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            delta = closed_at - created_at
            total_hours += delta.total_seconds() / 3600.0
            valid_prs += 1

    if valid_prs == 0:
        return None

    return round(total_hours / valid_prs, 2)
