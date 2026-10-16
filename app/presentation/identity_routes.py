"""运行时身份发现与验签；没有令牌签发或密钥接收入口。"""
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.presentation.identity import identity_policy, require_signed_identity


def register_identity_routes(api: FastAPI) -> None:
    @api.get("/commerce/identity/config")
    async def identity_config(request: Request):
        return JSONResponse({"mode": identity_policy(request).mode, "default_buyer_id": "pao-coder"},
                            headers={"Cache-Control": "no-store"})

    @api.get("/commerce/identity")
    async def current_identity(request: Request):
        identity = await require_signed_identity(request)
        expires_at = datetime.fromtimestamp(identity.expires_at, timezone.utc).isoformat().replace("+00:00", "Z")
        return JSONResponse({"mode": "hmac", "buyer_id": identity.buyer_id, "expires_at": expires_at},
                            headers={"Cache-Control": "no-store", "Vary": "Authorization"})
