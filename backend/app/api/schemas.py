"""API request/response contracts (decoupled from the internal domain models)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.db.repository import SessionDetail, SessionSummary
from app.domain.sections import Section


class CreateSessionRequest(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    website: str | None = Field(default=None, max_length=2048)
    objective: str = Field(min_length=1, max_length=2000)

    @field_validator("company", "objective")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("website", mode="before")
    @classmethod
    def _empty_website_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str


class ResumeResponse(BaseModel):
    session_id: str
    status: str
    #: False when the run was already finished (resume was a no-op).
    resumed: bool


class SourceOut(BaseModel):
    id: str
    url: str
    title: str
    snippet: str


class ReportItemOut(BaseModel):
    text: str
    source_ids: list[str]


class ReportSectionOut(BaseModel):
    section: Section
    items: list[ReportItemOut]


class ReportOut(BaseModel):
    version: int
    quality_score: float | None
    sections: list[ReportSectionOut]
    unknowns: list[str]
    sources: list[SourceOut]
    created_at: datetime | None


class SessionSummaryOut(BaseModel):
    id: str
    company: str
    company_key: str
    website: str | None
    objective: str
    status: str
    degraded: bool
    created_at: datetime
    updated_at: datetime
    report_version: int | None

    @classmethod
    def from_summary(cls, s: SessionSummary) -> "SessionSummaryOut":
        return cls(
            id=s.id,
            company=s.company,
            company_key=s.company_key,
            website=s.website,
            objective=s.objective,
            status=s.status,
            degraded=s.degraded,
            created_at=s.created_at,
            updated_at=s.updated_at,
            report_version=s.report_version,
        )


class SessionHistoryResponse(BaseModel):
    sessions: list[SessionSummaryOut]


class SessionDetailResponse(BaseModel):
    id: str
    company: str
    website: str | None
    objective: str
    company_key: str
    status: str
    degraded: bool
    created_at: datetime
    updated_at: datetime
    report: ReportOut | None

    @classmethod
    def from_detail(cls, detail: SessionDetail) -> "SessionDetailResponse":
        report: ReportOut | None = None
        if detail.report is not None:
            r = detail.report
            report = ReportOut(
                version=detail.report_version or 1,
                quality_score=r.quality_score,
                sections=[
                    ReportSectionOut(
                        section=s.section,
                        items=[
                            ReportItemOut(text=i.text, source_ids=i.source_ids)
                            for i in s.items
                        ],
                    )
                    for s in r.sections
                ],
                unknowns=r.unknowns,
                sources=[
                    SourceOut(id=s.id, url=s.url, title=s.title, snippet=s.snippet)
                    for s in r.sources
                ],
                created_at=detail.report_created_at,
            )
        return cls(
            id=detail.id,
            company=detail.company,
            website=detail.website,
            objective=detail.objective,
            company_key=detail.company_key,
            status=detail.status,
            degraded=detail.degraded,
            created_at=detail.created_at,
            updated_at=detail.updated_at,
            report=report,
        )
