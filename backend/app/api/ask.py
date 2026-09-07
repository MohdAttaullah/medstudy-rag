"""The public Ask API.

`POST /api/v1/ask` is the first endpoint in this system that may return a medical answer, and it
returns one only when M8 verified every material claim. The M5–M8 diagnostic endpoints are
unchanged: they still pin `answering_enabled` false and still return exactly what they always did.
Nothing was retrofitted into an answering endpoint.

Citation and source inspection deliberately adds no new document routes. The M2 parse API already
streams page previews, elements, tables, figures and formulas under `document:read` with tenant
scope resolved server-side, so a citation links into those rather than handing the browser an
object-store URL of its own.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.documents import (
    Actor,
    ConfiguredService,
    Service,
    correlation,
    enforce_rate_limit,
)
from app.repositories.conversations import ConversationRepository
from app.retrieval.model import RetrievalFilters
from app.schemas.ask import (
    AskRequest,
    AskResponse,
    ConversationSummaryView,
    ConversationView,
)
from app.schemas.documents import Page
from app.services.ask import conversation_summary

router = APIRouter(prefix="/api/v1")


def _filters(body: AskRequest) -> RetrievalFilters | None:
    if body.filters is None:
        return None
    return RetrievalFilters(
        document_ids=tuple(body.filters.document_ids),
        document_version_ids=tuple(body.filters.document_version_ids),
        source_types=tuple(body.filters.source_types),
        authority_levels=tuple(body.filters.authority_levels),
        chunk_types=tuple(body.filters.chunk_types),
    )


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest, request: Request, actor: Actor, service: ConfiguredService
) -> AskResponse:
    """Answer an educational question from the indexed corpus, or explain why it was not answered.

    Runs the whole verified pipeline — retrieval, reranking, evidence assembly, the sufficiency
    gate, grounded drafting and claim verification. A substantive answer is returned only behind an
    M8 PASS; every other outcome returns a typed refusal that says which kind it was.
    """
    actor.require("ask:submit")
    enforce_rate_limit(request, actor, "ask")
    return await service.ask.ask(
        actor,
        body.question,
        correlation(request),
        conversation_id=body.conversation_id,
        idempotency_key=body.idempotency_key,
        filters=_filters(body),
    )


@router.get("/conversations", response_model=Page[ConversationSummaryView])
def conversations(
    actor: Actor,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    actor.require("conversation:read")
    with service.sessions() as session:
        repository = ConversationRepository(session, actor.tenant_id, actor.user_id)
        rows = repository.list_conversations(limit, offset)
        return {
            "items": [conversation_summary(*row) for row in rows],
            "total": repository.count(),
            "limit": limit,
            "offset": offset,
        }


@router.get("/conversations/{conversation_id}", response_model=ConversationView)
def conversation(conversation_id: UUID, actor: Actor, service: Service) -> dict[str, Any]:
    """Read one conversation. Ownership comes from the authenticated principal, not the URL.

    The repository filters on the caller's tenant, so another tenant's id is simply not found —
    the same response as an id that never existed, which tells a prober nothing.
    """
    actor.require("conversation:read")
    with service.sessions() as session:
        repository = ConversationRepository(session, actor.tenant_id, actor.user_id)
        row = repository.get(conversation_id)
        turns = repository.turns(conversation_id)
        return {
            "conversation_id": row.id,
            "title": row.title,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "turns": [
                service.ask.conversation_turn(turn, repository.citations(turn.id)) for turn in turns
            ],
        }


Dependencies = [Depends(correlation)]
