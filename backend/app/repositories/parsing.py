"""Tenant-scoped access to parsed material.

Every lookup carries the tenant of the authenticated principal and, where the resource belongs to
a document, re-checks the owning document and version. Parsed material inherits exactly the
security boundary of the original it was derived from; an opaque UUID is not authorization.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models.documents import DocumentVersion
from app.models.parsing import (
    DocumentElement,
    DocumentPage,
    FigureArtifact,
    FormulaArtifact,
    ParseRun,
    TableArtifact,
)
from app.repositories.documents import get_document, get_version


def scoped_version(
    session: Session, tenant_id: UUID, document_id: UUID, version_id: UUID
) -> DocumentVersion:
    get_document(session, tenant_id, document_id)
    version = get_version(session, tenant_id, version_id)
    if version.document_id != document_id:
        raise DomainError("DOCUMENT_VERSION_NOT_FOUND", "Document version not found.", 404)
    return version


def get_parse_run(session: Session, tenant_id: UUID, run_id: UUID) -> ParseRun:
    run = session.scalar(
        select(ParseRun).where(ParseRun.id == run_id, ParseRun.tenant_id == tenant_id)
    )
    if run is None:
        raise DomainError("PARSE_RUN_NOT_FOUND", "Parse run not found.", 404)
    return run


def active_parse_run(session: Session, tenant_id: UUID, version_id: UUID) -> ParseRun | None:
    return session.scalar(
        select(ParseRun).where(
            ParseRun.tenant_id == tenant_id,
            ParseRun.document_version_id == version_id,
            ParseRun.is_active.is_(True),
        )
    )


def latest_parse_run(session: Session, tenant_id: UUID, version_id: UUID) -> ParseRun | None:
    """The run an operator should look at: the active one, else the most recent attempt."""
    return active_parse_run(session, tenant_id, version_id) or session.scalar(
        select(ParseRun)
        .where(ParseRun.tenant_id == tenant_id, ParseRun.document_version_id == version_id)
        .order_by(ParseRun.attempt.desc())
        .limit(1)
    )


def get_page(session: Session, tenant_id: UUID, run: ParseRun, page_id: UUID) -> DocumentPage:
    page = session.scalar(
        select(DocumentPage).where(
            DocumentPage.id == page_id,
            DocumentPage.tenant_id == tenant_id,
            DocumentPage.parse_run_id == run.id,
        )
    )
    if page is None:
        raise DomainError("PARSE_PAGE_NOT_FOUND", "Page not found for this parse run.", 404)
    return page


def get_page_by_number(
    session: Session, tenant_id: UUID, run: ParseRun, page_number: int
) -> DocumentPage:
    page = session.scalar(
        select(DocumentPage).where(
            DocumentPage.page_number == page_number,
            DocumentPage.tenant_id == tenant_id,
            DocumentPage.parse_run_id == run.id,
        )
    )
    if page is None:
        raise DomainError("PARSE_PAGE_NOT_FOUND", "Page not found for this parse run.", 404)
    return page


def get_table(session: Session, tenant_id: UUID, run: ParseRun, table_id: UUID) -> TableArtifact:
    table = session.scalar(
        select(TableArtifact).where(
            TableArtifact.id == table_id,
            TableArtifact.tenant_id == tenant_id,
            TableArtifact.parse_run_id == run.id,
        )
    )
    if table is None:
        raise DomainError("TABLE_ARTIFACT_NOT_FOUND", "Table not found for this parse run.", 404)
    return table


def get_figure(session: Session, tenant_id: UUID, run: ParseRun, figure_id: UUID) -> FigureArtifact:
    figure = session.scalar(
        select(FigureArtifact).where(
            FigureArtifact.id == figure_id,
            FigureArtifact.tenant_id == tenant_id,
            FigureArtifact.parse_run_id == run.id,
        )
    )
    if figure is None:
        raise DomainError("FIGURE_ARTIFACT_NOT_FOUND", "Figure not found for this parse run.", 404)
    return figure


def get_formula(
    session: Session, tenant_id: UUID, run: ParseRun, formula_id: UUID
) -> FormulaArtifact:
    formula = session.scalar(
        select(FormulaArtifact).where(
            FormulaArtifact.id == formula_id,
            FormulaArtifact.tenant_id == tenant_id,
            FormulaArtifact.parse_run_id == run.id,
        )
    )
    if formula is None:
        raise DomainError(
            "FORMULA_ARTIFACT_NOT_FOUND", "Formula not found for this parse run.", 404
        )
    return formula


def get_element(
    session: Session, tenant_id: UUID, run: ParseRun, element_id: UUID
) -> DocumentElement:
    element = session.scalar(
        select(DocumentElement).where(
            DocumentElement.id == element_id,
            DocumentElement.tenant_id == tenant_id,
            DocumentElement.parse_run_id == run.id,
        )
    )
    if element is None:
        raise DomainError("PARSE_ELEMENT_NOT_FOUND", "Element not found for this parse run.", 404)
    return element
