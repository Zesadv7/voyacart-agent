"""持久会话候选 HTTP：签名归属、幂等写入与独立目录核验。"""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.ag_ui_journal import JournalConflict, JournalForbidden, JournalNotFound
from app.infrastructure.candidate_workspace import CandidateWorkspaceError
from app.presentation.identity import require_buyer, require_session


class CandidateMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: StrictInt = Field(ge=0)


class CandidateSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["run"]
    run_id: str = Field(min_length=1, max_length=128)
    product_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)


class AddCandidate(CandidateMutation):
    source: CandidateSource


class CompareCandidates(CandidateMutation):
    candidate_ids: list[str] = Field(max_length=3)


def register_candidate_workspace_routes(api, get_service):
    def service():
        value = get_service()
        if value is None:
            raise HTTPException(503, detail={"code": "CANDIDATE_STORE_UNAVAILABLE", "message": "候选存储尚未就绪，请稍后重试。"})
        return value

    async def owned(request, buyer_id, session_id):
        buyer = await require_buyer(request, buyer_id)
        await require_session(request, buyer, session_id, create=False)
        return buyer

    async def invoke(operation):
        try:
            return await operation()
        except CandidateWorkspaceError as error:
            detail = {"code": error.code, "message": str(error)}
            if error.current_revision is not None:
                detail["current_revision"] = error.current_revision
            raise HTTPException(error.status, detail=detail) from error
        except (JournalForbidden, JournalNotFound, JournalConflict) as error:
            status = 403 if isinstance(error, JournalForbidden) else 404 if isinstance(error, JournalNotFound) else 409
            raise HTTPException(status, detail={"code": "CANDIDATE_SOURCE_ACCESS", "message": str(error)}) from error
        except (sqlite3.DatabaseError, SQLAlchemyError) as error:
            raise HTTPException(503, detail={"code": "CANDIDATE_STORE_UNAVAILABLE", "message": "候选暂时无法读写，原记录仍保留，请稍后核对。"}) from error
        except ValueError as error:
            raise HTTPException(422, detail={"code": "INVALID_CANDIDATE_REQUEST", "message": "候选参数无效，请核对规格、目的地或币种。"}) from error

    prefix = "/commerce/ag-ui/sessions/{session_id}/candidates"

    @api.get(prefix)
    async def get_workspace(session_id: str, request: Request, buyer_id: str = Query(min_length=1, max_length=128)):
        buyer = await owned(request, buyer_id, session_id)
        return await invoke(lambda: service().store.list(buyer, session_id))

    @api.post(prefix)
    async def add_candidate(session_id: str, body: AddCandidate, request: Request,
                            buyer_id: str = Query(min_length=1, max_length=128)):
        buyer = await owned(request, buyer_id, session_id)
        return await invoke(lambda: service().store.add(buyer, session_id, body.request_id,
                            body.expected_revision, body.source.model_dump()))

    @api.put(prefix + "/comparison")
    async def compare_candidates(session_id: str, body: CompareCandidates, request: Request,
                                 buyer_id: str = Query(min_length=1, max_length=128)):
        buyer = await owned(request, buyer_id, session_id)
        return await invoke(lambda: service().store.compare(buyer, session_id, body.request_id,
                            body.expected_revision, body.candidate_ids))

    @api.delete(prefix + "/{candidate_id}")
    async def remove_candidate(session_id: str, candidate_id: str, body: CandidateMutation, request: Request,
                               buyer_id: str = Query(min_length=1, max_length=128)):
        buyer = await owned(request, buyer_id, session_id)
        return await invoke(lambda: service().store.remove(buyer, session_id, body.request_id,
                            body.expected_revision, candidate_id))

    @api.get(prefix + "/{candidate_id}/verification")
    async def verify_candidate(session_id: str, candidate_id: str, request: Request,
                               buyer_id: str = Query(min_length=1, max_length=128),
                               target_currency: str = Query("CNY", pattern=r"^[A-Z]{3}$"),
                               ship_to: str | None = Query(None, pattern=r"^[A-Z]{2}$")):
        buyer = await owned(request, buyer_id, session_id)
        return await invoke(lambda: service().verify(buyer, session_id, candidate_id,
                            target_currency=target_currency, ship_to=ship_to))
