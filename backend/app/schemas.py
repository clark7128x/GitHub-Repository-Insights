from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AnalyzeRequest(BaseModel):
    repo: str  # e.g. "fastapi/fastapi"

class AnalysisResponse(BaseModel):
    task_id: str
    status: str

class AnalysisResultDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    repo_name: str
    status: str
    bus_factor: int | None = None
    hot_files: list[dict[str, Any]] | None = None
    avg_pr_lifetime_hours: float | None = None
    created_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
