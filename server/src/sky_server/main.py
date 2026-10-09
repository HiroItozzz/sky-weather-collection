"""アップロード API（M1：ローカル版）。"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sky_server.auth import hash_token
from sky_server.config import get_data_dir
from sky_server.models import UUID_PATTERN, ObservationMetadata, User
from sky_server.storage import (
    BlobStore,
    LocalBlobStore,
    LocalObservationRepository,
    ObservationRepository,
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024

ObservationId = Annotated[str, PathParam(pattern=UUID_PATTERN)]


def create_app(
    data_dir: Path | None = None,
    repository: ObservationRepository | None = None,
    blob_store: BlobStore | None = None,
) -> FastAPI:
    if data_dir is None:
        data_dir = get_data_dir()
    repository = repository or LocalObservationRepository(data_dir)
    blob_store = blob_store or LocalBlobStore(data_dir)

    app = FastAPI(title="sky-weather-collection")

    def current_user(request: Request) -> User:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        unauthorized = HTTPException(
            status_code=401,
            detail="認証に失敗しました",
            headers={"WWW-Authenticate": "Bearer"},
        )
        if scheme.lower() != "bearer" or not token.strip():
            raise unauthorized
        user = repository.get_user_by_token_hash(hash_token(token.strip()))
        if user is None or user.revoked_at is not None:
            raise unauthorized
        return user

    CurrentUser = Annotated[User, Depends(current_user)]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.put("/v1/observations/{observation_id}")
    async def put_observation(
        observation_id: ObservationId,
        user: CurrentUser,
        metadata: Annotated[str, Form()],
        image: Annotated[UploadFile, File()],
    ) -> JSONResponse:
        observation_id = observation_id.lower()

        if image.content_type != "image/jpeg":
            raise HTTPException(422, "image は image/jpeg で送ってください")
        data = await image.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "画像が大きすぎます（上限は10MBです）")

        try:
            meta = ObservationMetadata.model_validate_json(metadata)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
        if meta.observation_id != observation_id:
            raise HTTPException(422, "metadata の observation_id がパスの ID と一致しません")
        if hashlib.sha256(data).hexdigest() != meta.image_sha256:
            raise HTTPException(422, "画像の SHA-256 が image_sha256 と一致しません")

        existing = repository.get_observation(observation_id)
        if existing is not None:
            return _duplicate_response(existing, user, meta)

        # キーにハッシュを含めて、同じ ID で別の画像が同時に来ても上書きし合わないようにする
        image_key = _image_key(observation_id, meta.image_sha256)
        blob_store.put(image_key, data)
        record = meta.with_server_fields(user.user_id, datetime.now(UTC), image_key)
        if not repository.add_observation(record):
            # 同時に同じ ID が登録された場合
            existing = repository.get_observation(observation_id)
            if existing is None:
                raise HTTPException(500, "保存に失敗しました")
            return _duplicate_response(existing, user, meta)
        return JSONResponse({"observation_id": observation_id, "status": "created"}, 201)

    @app.get("/v1/observations/{observation_id}")
    def get_observation(observation_id: ObservationId, user: CurrentUser) -> dict:
        observation_id = observation_id.lower()
        record = repository.get_observation(observation_id)
        if record is None or record["user_id"] != user.user_id:
            raise HTTPException(404, "観測が見つかりません")
        return {"observation_id": observation_id, "received_at": record["received_at"]}

    return app


def _image_key(observation_id: str, image_sha256: str) -> str:
    return f"{observation_id}/{image_sha256}.jpg"


def _duplicate_response(existing: dict, user: User, meta: ObservationMetadata) -> JSONResponse:
    if existing["user_id"] != user.user_id or existing["image_sha256"] != meta.image_sha256:
        raise HTTPException(409, "同じ ID で内容の違う観測がすでにあります")
    return JSONResponse({"observation_id": meta.observation_id, "status": "exists"}, 200)
