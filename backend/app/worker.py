import os
from datetime import UTC, datetime

from celery import Celery

from app import github_api
from app.database import SessionLocal
from app.github_api import GitHubRateLimitError
from app.models import AnalysisResult

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

celery_app = Celery(
    "worker",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND
)

@celery_app.task(name="analyze_repo_task", bind=True)
def analyze_repo_task(self, task_id: str, repo: str):
    db = SessionLocal()

    try:
        # Mark as processing
        result_record = db.query(AnalysisResult).filter(AnalysisResult.id == task_id).first()
        if not result_record:
            return "Task not found"

        result_record.status = "processing"
        db.commit()

        # 1. Fetch commits
        commits = github_api.fetch_commits(repo, per_page=100)

        # 2. Calculate Bus Factor
        bus_factor = github_api.calculate_bus_factor(commits)

        # 3. Calculate Hot Files (limit to 30 latest commits to save API calls)
        hot_files = github_api.calculate_hot_files(repo, commits, limit=30)

        # 4. Fetch PRs and calculate lifetime
        prs = github_api.fetch_pull_requests(repo, state="closed", per_page=100)
        avg_pr_lifetime = github_api.calculate_avg_pr_lifetime(prs)

        # Update record
        result_record.bus_factor = bus_factor
        result_record.hot_files = hot_files
        result_record.avg_pr_lifetime_hours = avg_pr_lifetime
        result_record.status = "completed"
        result_record.completed_at = datetime.now(UTC)
        db.commit()

        return {"status": "success", "task_id": task_id}

    except GitHubRateLimitError as e:
        db.rollback()
        result_record = db.query(AnalysisResult).filter(AnalysisResult.id == task_id).first()
        if result_record:
            result_record.status = "failed"
            result_record.error_message = str(e)
            result_record.completed_at = datetime.now(UTC)
            db.commit()
        # Do not re-raise — the task is marked failed, no Celery retry needed
        return {"status": "failed", "task_id": task_id, "error": str(e)}

    except Exception as e:
        db.rollback()
        result_record = db.query(AnalysisResult).filter(AnalysisResult.id == task_id).first()
        if result_record:
            result_record.status = "failed"
            result_record.error_message = str(e)
            result_record.completed_at = datetime.now(UTC)
            db.commit()
        raise
    finally:
        db.close()
