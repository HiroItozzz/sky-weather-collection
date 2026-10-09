"""アップロードの本文を読む前の検査（ASGI ミドルウェア）。

FastAPI は依存（認証）を解決する前に multipart の本文をすべて読むので、
`PUT /v1/observations/{id}` に限って、大きさ → 認証 → 本文の数え上げ、の順に検査する。
"""

import json
import re

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sky_server.auth import hash_token
from sky_server.storage import ObservationRepository

# 画像の上限 10MB に、メタデータと multipart の区切りの分として 1MB を足した値
MAX_UPLOAD_BYTES = 11 * 1024 * 1024

UPLOAD_PATH = re.compile(r"^/v1/observations/[^/]+$")


class UploadGuard:
    def __init__(self, app: ASGIApp, repository: ObservationRepository) -> None:
        self.app = app
        self.repository = repository

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "PUT"
            or not UPLOAD_PATH.match(scope["path"])
        ):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in reversed(scope["headers"])}

        # 1. 大きさ（資源を使わない検査なので先に行う）
        length = headers.get(b"content-length")
        if length is not None:
            text = length.decode("latin-1").strip()
            if not (text.isascii() and text.isdigit()):
                await _respond(send, 400, "Content-Length が正しくありません")
                return
            if int(text) > MAX_UPLOAD_BYTES:
                await _respond(send, 413, "リクエストが大きすぎます")
                return
        # Content-Length がないときも受け付ける。認証を先に通し、本文は手順 3 で数える

        # 2. 認証
        user = await self._authenticate(headers.get(b"authorization", b"").decode("latin-1"))
        if user is None:
            await _respond(send, 401, "認証に失敗しました", {"WWW-Authenticate": "Bearer"})
            return
        # state はリクエストをまたいで共有されることがあるので、書き換えずに新しい dict にする
        scope["state"] = {**scope.get("state", {}), "user": user}

        # 3. 本文を数える。Content-Length を偽ったリクエストは、超えた時点で読むのをやめる
        received = 0
        too_large = False
        responded = False

        async def counting_receive() -> Message:
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_UPLOAD_BYTES:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            # 打ち切ったあとは、アプリ側の応答（読み取りの失敗など）を捨てて 413 に置き換える
            nonlocal responded
            if too_large:
                if not responded:
                    responded = True
                    await _respond(send, 413, "リクエストが大きすぎます")
                return
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)
        if too_large and not responded:
            await _respond(send, 413, "リクエストが大きすぎます")

    async def _authenticate(self, header: str):
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        user = await run_in_threadpool(
            self.repository.get_user_by_token_hash, hash_token(token.strip())
        )
        if user is None or user.revoked_at is not None:
            return None
        return user


async def _respond(
    send: Send, status: int, detail: str, headers: dict[str, str] | None = None
) -> None:
    """FastAPI の `HTTPException` と同じ形（`{"detail": "..."}`）で返す。"""
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": body})
