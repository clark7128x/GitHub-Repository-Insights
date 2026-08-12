# System Architecture

## Context Diagram

```mermaid
C4Context
    title System Context for GitHub Repository Insights

    Person(user, "User", "A developer or engineering manager analyzing a repo")
    
    System_Boundary(insights, "GitHub Repository Insights") {
        Container(frontend, "React Dashboard", "React, Vite", "Provides the UI for submitting repositories and viewing metrics")
        Container(api, "FastAPI Backend", "Python, FastAPI", "Handles API requests, triggers background analysis, and serves results")
        Container(worker, "Celery Worker", "Python, Celery", "Processes analysis jobs asynchronously")
        ContainerDb(db, "PostgreSQL Database", "PostgreSQL", "Stores job status and calculated repository metrics")
        ContainerDb(redis, "Redis Broker/Cache", "Redis", "Message broker for Celery, task state, and GitHub API response cache")
    }
    
    System_Ext(github, "GitHub API", "Provides commit history, pull request data, and repository metadata")

    Rel(user, frontend, "Views and submits repos via", "HTTPS")
    Rel(frontend, api, "Makes API calls to", "JSON/HTTPS")
    Rel(api, db, "Reads/writes analysis state", "SQL/TCP")
    Rel(api, redis, "Queues analysis jobs", "TCP")
    Rel(redis, worker, "Delivers jobs to", "TCP")
    Rel(worker, github, "Fetches repo data from", "HTTPS")
    Rel(worker, redis, "Reads/writes cached responses", "TCP")
    Rel(worker, db, "Saves computed metrics to", "SQL/TCP")
```

## Sequence Diagram: Analysis Flow

```mermaid
sequenceDiagram
    actor User
    participant React as React UI
    participant API as FastAPI Backend
    participant Redis as Redis Broker
    participant Celery as Celery Worker
    participant DB as PostgreSQL
    participant GitHub as GitHub API

    User->>React: Submit "owner/repo"
    React->>API: POST /api/analyze {"repo": "owner/repo"}
    API->>DB: Create row {status: "pending"}
    API->>Redis: Queue analyze_repo_task
    API-->>React: Return {task_id, status: "pending"}
    
    Note over React,API: React begins polling (max 60 attempts)
    loop Polling (2s interval, max 60)
        React->>API: GET /api/results/{task_id}
        API->>DB: Query task status
        DB-->>API: {status: "pending"}
        API-->>React: {status: "pending"}
    end

    Redis->>Celery: Deliver analyze_repo_task
    Celery->>DB: Update status to "processing"
    
    Celery->>Redis: Check cache for commits
    alt Cache miss
        Celery->>GitHub: GET /repos/owner/repo/commits
        GitHub-->>Celery: Return 100 commits
        Celery->>Redis: Cache response (TTL 15m)
    else Cache hit
        Redis-->>Celery: Return cached commits
    end
    
    loop Hot Files (Limited to 30)
        Celery->>GitHub: GET /repos/owner/repo/commits/{ref}
        Note over Celery,GitHub: Checks X-RateLimit-Remaining on each response
        GitHub-->>Celery: Return changed files
    end
    
    Celery->>GitHub: GET /repos/owner/repo/pulls?state=closed
    GitHub-->>Celery: Return 100 PRs
    
    Celery->>Celery: Compute Bus Factor & Avg PR Lifetime
    Celery->>DB: Update status to "completed" & save metrics
    
    React->>API: GET /api/results/{task_id}
    API->>DB: Query task status
    DB-->>API: {status: "completed", metrics: ...}
    API-->>React: Return analysis results
    React-->>User: Display metrics
```

## Calculation Logic Explained

- **Bus Factor**: The application parses the author login/name from the most recent 100 commits. It tallies the number of commits per distinct author, sorts them in descending order of contribution, and determines the minimum number of authors required to account for exactly 50% or more of the total commits. If 10 developers made 100 commits, but 1 developer made 55 of them, the bus factor is 1.
- **Hot Files**: The application inspects the diff/files output for the latest 30 commits. It counts how many distinct times each file path was modified across those commits. The paths are sorted by modification frequency, and the top 10 are returned. This indicates the most volatile code areas.
- **Average Pull Request Lifetime**: The application fetches closed and merged pull requests. It parses the `created_at` timestamp and the `closed_at` (or `merged_at`) timestamp, converting the difference to hours. It then averages these lifespans across all fetched PRs to produce a single metric representing how long PRs sit open on average. Returns `null` when no valid (closed/merged) PRs are found.

## Caching Strategy

GitHub API responses are cached in Redis with a configurable TTL (default: 15 minutes, controlled by `CACHE_TTL_SECONDS`). Every request to the GitHub API goes through a central `github_get()` helper that:

1. Checks Redis for a cached response keyed by the URL + query parameters (SHA-256 hash).
2. On a cache hit, returns the cached JSON immediately — no HTTP request is made.
3. On a cache miss, makes the HTTP request and stores the response in Redis with the configured TTL.

If Redis is unavailable (e.g. local development without Docker), caching degrades gracefully — requests go directly to the GitHub API.

## Rate Limit Handling

The `github_get()` helper inspects `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers on every GitHub API response:

- **Proactive detection**: If `X-RateLimit-Remaining` reaches 0 or a 403/429 is returned with a rate-limit message, the handler reads the reset timestamp.
- **Backoff**: If the wait time is within the configured maximum (`RATE_LIMIT_MAX_WAIT`, default: 120 seconds), the request sleeps until reset and retries automatically.
- **Failure**: If the wait would exceed the maximum, a `GitHubRateLimitError` is raised. The Celery worker catches this and marks the analysis task as `"failed"` with a descriptive error message — never as `"completed"` with incomplete data.
- **No silent swallowing**: The previous bare `except: continue` in `calculate_hot_files()` has been replaced with explicit error propagation. Rate-limit errors are never silently ignored.

## Webhook Integration

A `POST /api/webhooks/github` endpoint accepts GitHub webhook push events:

1. Verifies the `X-Hub-Signature-256` HMAC-SHA256 signature against the `GITHUB_WEBHOOK_SECRET` environment variable.
2. On valid `push` events, extracts the repository full name and triggers a new analysis.
3. Returns 403 if the secret is not configured or the signature is invalid.

## Execution Model

The backend supports two execution modes controlled by the `CELERY_ENABLED` environment variable:

- **`CELERY_ENABLED=true`** (default, used in Docker): Analysis tasks are dispatched to a Celery worker via Redis. This is the production path.
- **`CELERY_ENABLED=false`** (local development): Analysis tasks run synchronously in the FastAPI request thread. This eliminates the dependency on Redis/Celery for quick local iteration with SQLite.
