import hashlib
import hmac
import json

# Set CELERY_ENABLED=false before importing app so the sync path is used in tests
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["CELERY_ENABLED"] = "false"
os.environ["CORS_ORIGINS"] = "*"

from app.database import Base, SessionLocal, engine
from app.github_api import GitHubRateLimitError
from app.main import app
from app.models import AnalysisResult

# Setup test DB
Base.metadata.create_all(bind=engine)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Item 4: Restore real assertion that the task was invoked with correct args
# ---------------------------------------------------------------------------

def test_analyze_repo_endpoint():
    with patch("app.main.analyze_repo_task") as mock_task:
        response = client.post("/api/analyze", json={"repo": "test/repo"})
        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] == "pending"
        # Verify the task was actually called with the right task_id and repo
        mock_task.assert_called_once_with(data["task_id"], "test/repo")


def test_get_result_not_found():
    response = client.get("/api/results/nonexistent-id")
    assert response.status_code == 404


def test_get_result_found():
    task_id = str(uuid.uuid4())
    # Insert dummy data directly via db session
    db = SessionLocal()
    db.add(AnalysisResult(
        id=task_id, repo_name="test/repo", status="completed",
        bus_factor=2, avg_pr_lifetime_hours=10.5,
    ))
    db.commit()
    db.close()

    response = client.get(f"/api/results/{task_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == task_id
    assert data["bus_factor"] == 2


# ---------------------------------------------------------------------------
# Item 7: Rate-limit handling unit test
# ---------------------------------------------------------------------------

def test_rate_limit_403_raises_error():
    """Mock a 403 rate-limit response and assert GitHubRateLimitError is raised."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.headers = {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "9999999999",  # far future — triggers "wait too long"
    }
    mock_response.text = "API rate limit exceeded"
    mock_response.json.return_value = {"message": "API rate limit exceeded"}

    with patch("app.github_api.httpx.get", return_value=mock_response):
        with pytest.raises(GitHubRateLimitError) as exc_info:
            from app.github_api import fetch_commits
            fetch_commits("test/repo")
        assert "rate limit" in str(exc_info.value).lower()


def test_rate_limit_worker_sets_failed_status():
    """Verify that when the worker hits a rate limit, the task status is 'failed' with a message."""
    task_id = str(uuid.uuid4())
    db = SessionLocal()
    db.add(AnalysisResult(id=task_id, repo_name="rate-limit/test", status="pending"))
    db.commit()
    db.close()

    with patch("app.github_api.github_get", side_effect=GitHubRateLimitError(message="Rate limit exceeded")):
        from app.worker import analyze_repo_task
        result = analyze_repo_task(task_id, "rate-limit/test")

    assert result["status"] == "failed"

    # Verify DB record
    db = SessionLocal()
    record = db.query(AnalysisResult).filter(AnalysisResult.id == task_id).first()
    assert record.status == "failed"
    assert "rate limit" in record.error_message.lower()
    db.close()


# ---------------------------------------------------------------------------
# Item 8: Webhook endpoint tests
# ---------------------------------------------------------------------------

def test_webhook_no_secret():
    """Webhook should return 403 if GITHUB_WEBHOOK_SECRET is not configured."""
    original = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    os.environ["GITHUB_WEBHOOK_SECRET"] = ""
    # Reload the module-level variable
    import app.main as main_module
    main_module.GITHUB_WEBHOOK_SECRET = ""

    response = client.post("/api/webhooks/github", content=b'{}', headers={"X-GitHub-Event": "push"})
    assert response.status_code == 403

    # Restore
    os.environ["GITHUB_WEBHOOK_SECRET"] = original
    main_module.GITHUB_WEBHOOK_SECRET = original


def test_webhook_invalid_signature():
    """Webhook should reject requests with bad signatures."""
    secret = "test-secret"
    os.environ["GITHUB_WEBHOOK_SECRET"] = secret
    import app.main as main_module
    main_module.GITHUB_WEBHOOK_SECRET = secret

    body = json.dumps({"repository": {"full_name": "test/repo"}}).encode()
    response = client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )
    assert response.status_code == 403

    # Cleanup
    os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
    main_module.GITHUB_WEBHOOK_SECRET = ""


def test_webhook_valid_push():
    """Valid push webhook should trigger re-analysis."""
    secret = "test-secret"
    os.environ["GITHUB_WEBHOOK_SECRET"] = secret
    import app.main as main_module
    main_module.GITHUB_WEBHOOK_SECRET = secret

    body = json.dumps({"repository": {"full_name": "test/webhook-repo"}}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with patch("app.main.analyze_repo_task") as mock_task:
        response = client.post(
            "/api/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": sig,
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "pending"
    mock_task.assert_called_once()

    # Cleanup
    os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
    main_module.GITHUB_WEBHOOK_SECRET = ""


# ---------------------------------------------------------------------------
# Item 11: avg_pr_lifetime returns None with no valid PRs
# ---------------------------------------------------------------------------

def test_avg_pr_lifetime_no_valid_prs():
    """calculate_avg_pr_lifetime should return None, not 0.0, when there are no valid PRs."""
    from app.github_api import calculate_avg_pr_lifetime
    result = calculate_avg_pr_lifetime([])
    assert result is None

    # Also test with PRs that have no closed_at
    result = calculate_avg_pr_lifetime([{"created_at": "2024-01-01T00:00:00Z"}])
    assert result is None
