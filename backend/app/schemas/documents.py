from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import Authority, SourceType, Status


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=300)
    source_type: SourceType
    authority_level: Authority = Authority.UNREVIEWED
    description: str | None = Field(default=None, max_length=4000)
    specialty: str | None = Field(default=None, max_length=120)
    subject: str | None = Field(default=None, max_length=120)
    publisher: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def assessment_authority(self) -> "DocumentMetadata":
        if self.source_type in {
            SourceType.QUESTION_BANK,
            SourceType.QUESTION_PAPER,
            SourceType.ANSWER_KEY,
        } and self.authority_level in {Authority.REFERENCE, Authority.HIGH}:
            raise ValueError("Assessment sources must use ASSESSMENT or UNREVIEWED authority")
        return self


class VersionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filename: str = Field(min_length=1, max_length=255)
    edition: str | None = Field(default=None, max_length=120)
    publication_year: int | None = Field(default=None, ge=1400, le=2200)


class UploadMetadata(VersionMetadata):
    document: DocumentMetadata


class ORMView(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class VersionView(ORMView):
    id: UUID
    document_id: UUID
    version_number: int
    edition: str | None
    publication_year: int | None
    original_filename: str
    normalized_filename: str
    mime_type: str
    file_size_bytes: int
    sha256: str
    page_count: int | None
    ingestion_status: Status
    searchable: bool
    created_by_user_id: UUID
    created_at: datetime
    archived_at: datetime | None


class DocumentView(DocumentMetadata, ORMView):
    id: UUID
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    latest_version: VersionView | None = None


class StageView(ORMView):
    sequence: int
    id: UUID
    stage: str
    from_status: str | None
    to_status: str
    service_identity: str
    retry_number: int
    error_code: str | None
    error_detail: str | None
    correlation_id: UUID
    created_at: datetime


class JobView(ORMView):
    id: UUID
    document_version_id: UUID
    status: Status
    current_stage: str
    requested_by_user_id: UUID
    configuration_version: str
    config_snapshot: dict[str, object]
    correlation_id: UUID
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    queue_received_at: datetime | None
    retry_count: int
    max_retries: int
    last_error_code: str | None
    last_error_message: str | None
    document_id: UUID | None = None
    document_title: str = ""
    version_number: int = 0
    events: list[StageView] = []


class UploadResult(BaseModel):
    document_id: UUID
    version_id: UUID
    job_id: UUID
    status: Status
    replayed: bool = False


class Page[T](BaseModel):
    items: list[T]
    total: int
    offset: int
    limit: int


class AuditView(ORMView):
    id: UUID
    actor_id: UUID | None
    event_type: str
    resource_id: UUID | None
    correlation_id: UUID
    safe_metadata: dict[str, object]
    created_at: datetime
