import hashlib
import hmac
import json
import os
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app import database, models, schemas
from app.worker import analyze_repo_task

app = FastAPI(title="GitHub Repository Insights")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

CELERY_ENABLED = os.getenv("CELERY_ENABLED", "true").lower() == "true"
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize tables if alembic is not run (for quickstart fallback)
models.Base.metadata.create_all(bind=database.engine)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _dispatch_task(task_id: str, repo: str):
    """Route the analysis task to Celery or run it synchronously."""
    if CELERY_ENABLED:
        analyze_repo_task.apply_async(args=[task_id, repo], task_id=task_id)
    else:
        analyze_repo_task(task_id, repo)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/analyze", response_model=schemas.AnalysisResponse)
def analyze_repo(request: schemas.AnalyzeRequest, db: Session = Depends(database.get_db)):
    task_id = str(uuid.uuid4())

    # Create pending record
    db_result = models.AnalysisResult(
        id=task_id,
        repo_name=request.repo,
        status="pending"
    )
    db.add(db_result)
    db.commit()

    _dispatch_task(task_id, request.repo)

    return {"task_id": task_id, "status": "pending"}

@app.get("/api/results/{task_id}", response_model=schemas.AnalysisResultDetail)
def get_result(task_id: str, db: Session = Depends(database.get_db)):
    result = db.query(models.AnalysisResult).filter(models.AnalysisResult.id == task_id).first()
    if not result:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/api/webhooks/github")
async def github_webhook(request: Request, db: Session = Depends(database.get_db)):
    """
    Accepts GitHub webhook push events, verifies the HMAC-SHA256 signature,
    and triggers a re-analysis of the pushed repository.
    """
    if not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Webhook secret not configured")

    body = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body)
    event = request.headers.get("X-GitHub-Event", "")

    if event == "push":
        repo = payload.get("repository", {}).get("full_name")
        if repo:
            task_id = str(uuid.uuid4())
            db_result = models.AnalysisResult(
                id=task_id,
                repo_name=repo,
                status="pending"
            )
            db.add(db_result)
            db.commit()

            _dispatch_task(task_id, repo)

            return {"task_id": task_id, "status": "pending"}

    return {"status": "ignored"}
