"""アップロード API（M1：ローカル版）。"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from sky_server.auth import hash_token
from sky_server.backends import build_backend
from sky_server.jobs import (
    JobRepository,
    JobRunner,
    TaskScheduler,
    default_fetchers,
    ensure_weather_jobs,
)
from sky_server.models import UUID_PATTERN, ObservationMetadata, User
from sky_server.storage import BlobStore, ObservationRepository
from sky_server.task_auth import TaskAuthenticator, get_task_authenticator

MAX_IMAGE_BYTES = 10 * 1024 * 1024

ObservationId = Annotated[str, PathParam(pattern=UUID_PATTERN)]

JOB_ID_PATTERN = UUID_PATTERN[:-1] + r"_(forecast|label)$"


class FetchWeatherRequest(BaseModel):
    job_id: str = Field(pattern=JOB_ID_PATTERN)


def create_app(
    data_dir: Path | None = None,
    repository: ObservationRepository | None = None,
    blob_store: BlobStore | None = None,
    job_repository: JobRepository | None = None,
    scheduler: TaskScheduler | None = None,
    runner: JobRunner | None = None,
    task_auth: TaskAuthenticator | None = None,
) -> FastAPI:
    # 引数で渡されなかったものは、設定（SKY_BACKEND）に合わせて作る。全部渡されたら作らない
    if None in (repository, blob_store, job_repository, scheduler) or runner is None:
        backend = build_backend(data_dir)
        repository = repository or backend.repository
        blob_store = blob_store or backend.blob_store
        job_repository = job_repository or backend.job_repository
        scheduler = scheduler or backend.scheduler
        if runner is None:
            fetchers = default_fetchers(backend.weather_store)
            runner = JobRunner(repository, job_repository, scheduler, fetchers)
    # SKY_TASK_AUTH の値が正しくなければ、ここで起動時にエラーになる
    task_auth = task_auth or get_task_authenticator()

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

    def authenticate_task(request: Request) -> None:
        task_auth(request)

    def reserve_weather_jobs(record: dict) -> None:
        """天気ジョブを用意する。予約に失敗したら 503 にして、再送でやり直してもらう。"""
        try:
            ensure_weather_jobs(record, job_repository, scheduler)
        except Exception as e:
            raise HTTPException(
                503, "天気データの取得の予約に失敗しました。しばらくしてからもう一度送ってください"
            ) from e

    def exists_response(existing: dict, user: User, meta: ObservationMetadata) -> JSONResponse:
        response = _duplicate_response(existing, user, meta)
        reserve_weather_jobs(existing)
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # 保存先（Firestore、Cloud Tasks など）は同期のクライアントなので、async にせず
    # スレッドプールで動かして、イベントループを止めないようにする
    @app.put("/v1/observations/{observation_id}")
    def put_observation(
        observation_id: ObservationId,
        user: CurrentUser,
        metadata: Annotated[str, Form()],
        image: Annotated[UploadFile, File()],
    ) -> JSONResponse:
        observation_id = observation_id.lower()

        if image.content_type != "image/jpeg":
            raise HTTPException(422, "image は image/jpeg で送ってください")
        data = image.file.read(MAX_IMAGE_BYTES + 1)
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
            return exists_response(existing, user, meta)

        # キーにハッシュを含めて、同じ ID で別の画像が同時に来ても上書きし合わないようにする
        image_key = _image_key(observation_id, meta.image_sha256)
        blob_store.put(image_key, data)
        record = meta.with_server_fields(user.user_id, datetime.now(UTC), image_key)
        if not repository.add_observation(record):
            # 同時に同じ ID が登録された場合
            existing = repository.get_observation(observation_id)
            if existing is None:
                raise HTTPException(500, "保存に失敗しました")
            return exists_response(existing, user, meta)
        reserve_weather_jobs(record)
        return JSONResponse({"observation_id": observation_id, "status": "created"}, 201)

    @app.get("/v1/observations/{observation_id}")
    def get_observation(observation_id: ObservationId, user: CurrentUser) -> dict:
        observation_id = observation_id.lower()
        record = repository.get_observation(observation_id)
        if record is None or record["user_id"] != user.user_id:
            raise HTTPException(404, "観測が見つかりません")
        return {"observation_id": observation_id, "received_at": record["received_at"]}

    @app.post("/internal/tasks/fetch-weather", dependencies=[Depends(authenticate_task)])
    def fetch_weather(body: FetchWeatherRequest) -> dict:
        job_id = body.job_id.lower()
        result, reason = runner.run(job_id, datetime.now(UTC))
        job = job_repository.get_job(job_id)
        return {
            "job_id": job_id,
            "result": result,
            "reason": reason,
            "status": job.status if job else None,
        }

    return app


def _image_key(observation_id: str, image_sha256: str) -> str:
    return f"{observation_id}/{image_sha256}.jpg"


def _duplicate_response(existing: dict, user: User, meta: ObservationMetadata) -> JSONResponse:
    if existing["user_id"] != user.user_id or existing["image_sha256"] != meta.image_sha256:
        raise HTTPException(409, "同じ ID で内容の違う観測がすでにあります")
    return JSONResponse({"observation_id": meta.observation_id, "status": "exists"}, 200)
