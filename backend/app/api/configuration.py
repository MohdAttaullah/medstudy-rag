"""Authorized configuration administration. No tenant/global scope comes from the client."""

from typing import Any

from fastapi import APIRouter, Query, Request

from app.api.documents import Actor, Service, correlation
from app.schemas.configuration import (
    ApplyRequest,
    PreviewRequest,
    PreviewView,
    RevisionView,
    SettingsView,
)

router = APIRouter(prefix="/api/v1/settings")


@router.get("", response_model=SettingsView)
@router.get("/effective", response_model=SettingsView)
def settings(actor: Actor, service: Service) -> dict[str, Any]:
    return service.configuration.read(actor)


@router.post("/preview", response_model=PreviewView)
def preview(body: PreviewRequest, actor: Actor, service: Service) -> dict[str, Any]:
    return service.configuration.preview(actor, body)


@router.post("/changes", response_model=RevisionView)
def apply(body: ApplyRequest, request: Request, actor: Actor, service: Service) -> dict[str, Any]:
    return service.configuration.apply(actor, body, correlation(request))


@router.get("/history", response_model=list[RevisionView])
def history(
    actor: Actor,
    service: Service,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return service.configuration.history(actor, limit, offset)
