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

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.documents import (
    Actor,
    ConfiguredService,
    Service,
    correlation,
    enforce_rate_limit,
)
from app.core.errors import DomainError
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
from app.services.progress import QueueReporter, StageEvent, reporting

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
) -> AskResponse | Response:
    """Answer an educational question from the indexed corpus, or explain why it was not answered.

    Runs the whole verified pipeline — retrieval, reranking, evidence assembly, the sufficiency
    gate, grounded drafting and claim verification. A substantive answer is returned only behind an
    M8 PASS; every other outcome returns a typed refusal that says which kind it was.

    A caller that asks for `text/event-stream` gets the same answer preceded by stage events, so a
    twelve-second request can show which stage is running rather than appearing frozen. The default
    remains one JSON response: the negotiated form adds a view of the same work and changes nothing
    about it, and every existing client keeps the contract it was written against.
    """
    actor.require("ask:submit")
    enforce_rate_limit(request, actor, "ask")
    run = partial(
        service.ask.ask,
        actor,
        body.question,
        correlation(request),
        conversation_id=body.conversation_id,
        idempotency_key=body.idempotency_key,
        filters=_filters(body),
    )
    if not _wants_events(request, service.settings.ask.stream_progress_stages):
        return await run()
    return StreamingResponse(
        _events(run, correlation(request), request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Progress that arrives in one burst at the end is not progress. This asks an
            # intermediate proxy not to buffer the stream; nginx honours it.
            "X-Accel-Buffering": "no",
        },
    )


def _wants_events(request: Request, enabled: bool) -> bool:
    accept = request.headers.get("accept") or ""
    return enabled and "text/event-stream" in accept.lower()


def _frame(event: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode()


async def _events(
    run: Callable[[], Awaitable[AskResponse]], request_id: UUID, request: Request
) -> AsyncIterator[bytes]:
    """Stage events while the request runs, then the same response the JSON form returns.

    The pipeline runs in its own task so that this generator can forward events as they happen. The
    task owns a copy of this context, which is where the reporter is set, so the orchestrators —
    and the worker thread the synchronous M5/M6 stages run in — report to this queue and to no
    other request's.
    """
    queue: asyncio.Queue[StageEvent | None] = asyncio.Queue()
    progress = QueueReporter(request_id, queue)
    outcome: dict[str, Any] = {}

    async def execute() -> None:
        with reporting(progress):
            try:
                response = await run()
                outcome["result"] = response.model_dump(mode="json")
            except DomainError as exc:
                # The status line is long gone by now, so a refusal that the JSON form would answer
                # with 4xx arrives as a typed error event instead. The code and message are the
                # same ones; the client renders the same failure it renders for an HTTP error.
                outcome["error"] = {
                    "code": exc.code,
                    "message": exc.message,
                    "status": exc.status,
                }
            finally:
                progress.finish()

    task = asyncio.create_task(execute())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _frame("stage", event.model_dump(mode="json"))
        await task
        if "error" in outcome:
            yield _frame("error", {"error": outcome["error"]})
        else:
            yield _frame("result", outcome["result"])
    finally:
        # A reader who navigates away or closes the tab cancels this generator. Cancelling the
        # pipeline with it stops work nobody is waiting for; the turn is recorded only if the
        # request reached persistence first, which is the same outcome as any dropped request.
        if not task.done():
            task.cancel()


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
