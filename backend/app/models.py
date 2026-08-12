from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String

from app.database import Base


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id = Column(String, primary_key=True, index=True)  # Task ID
    repo_name = Column(String, index=True)
    status = Column(String, default="pending")  # pending, processing, completed, failed
    bus_factor = Column(Integer, nullable=True)
    hot_files = Column(JSON, nullable=True)  # [{"file": "path", "changes": 10}]
    avg_pr_lifetime_hours = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
