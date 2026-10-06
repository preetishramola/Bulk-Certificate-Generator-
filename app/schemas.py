from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import CertificateStatus, JobStatus
from app.renderer import unsupported_characters

TextField = Annotated[str, Field(min_length=1, max_length=150)]


def _check_renderable(value: str | None) -> str | None:
    if value is not None and (bad := unsupported_characters(value)):
        raise ValueError(f"contains characters the certificate template cannot render: {bad!r}")
    return value


# ---------- Requests ----------


class RecipientIn(BaseModel):
    """A single recipient. Validated individually so one bad row doesn't sink the job."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    grade: str | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("name")
    @classmethod
    def name_has_letters(cls, value: str) -> str:
        if not any(ch.isalpha() for ch in value):
            raise ValueError("must contain at least one letter")
        return value

    _renderable = field_validator("name", "grade")(_check_renderable)


class JobCreate(BaseModel):
    """
    The request envelope. Envelope errors (missing course name, empty list, ...)
    reject the whole request with 422. Recipients are typed as `Any` on purpose:
    each one is validated separately in `app.validation`.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    course_name: TextField
    issuer_name: TextField
    issue_date: date | None = Field(default=None, description="Defaults to today")
    recipients: list[Any] = Field(min_length=1)

    _renderable = field_validator("course_name", "issuer_name")(_check_renderable)


# ---------- Responses ----------


class JobProgress(BaseModel):
    total: int
    pending: int
    generated: int
    failed: int
    invalid: int
    processed: int
    percent_complete: float


class JobOut(BaseModel):
    id: str
    status: JobStatus
    course_name: str
    issuer_name: str
    issue_date: date
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    progress: JobProgress
    links: dict[str, str]


class CertificateOut(BaseModel):
    id: str
    job_id: str
    position: int = Field(description="Index of this recipient in the submitted list")
    status: CertificateStatus
    recipient_name: str | None
    recipient_email: str | None
    grade: str | None
    errors: list[str] | None
    input: Any | None = Field(default=None, description="Raw submitted data (invalid recipients only)")
    generated_at: datetime | None
    download_url: str | None


class CertificatePage(BaseModel):
    items: list[CertificateOut]
    total: int
    limit: int
    offset: int


class InvalidRecipient(BaseModel):
    index: int
    errors: list[str]
