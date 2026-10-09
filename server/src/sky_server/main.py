"""アップロード API（M1：ローカル版）。"""

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from sky_server.auth import hash_token
from sky_server.backends import build_backend
from sky_server.config import get_backend_name, get_daily_upload_limit
from sky_server.jobs import (
    JobRepository,
    JobRunner,
    Summarizer,
    TaskScheduler,
    default_fetchers,
    ensure_weather_jobs,
)
from sky_server.models import UUID_PATTERN, ObservationMetadata, User
from sky_server.storage import BlobStore, ObservationRepository
from sky_server.task_auth import TaskAuthenticator, get_task_authenticator
from sky_server.upload_guard import UploadGuard

MAX_IMAGE_BYTES = 10 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8\xff"

ObservationId = Annotated[str, PathParam(pattern=UUID_PATTERN)]

JOB_ID_PATTERN = UUID_PATTERN[:-1] + r"_(forecast|label)$"

CAPTURED_AT_UTC_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
UNKNOWN_ANSWER = {"result": "unknown", "source": None}
CATEGORIES = ("clear", "cloudy", "rain", "unknown")


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
    clock: Callable[[], datetime] | None = None,
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
            runner = JobRunner(
                repository,
                job_repository,
                scheduler,
                fetchers,
                summarizer=Summarizer(backend.weather_store),
            )
    # SKY_TASK_AUTH の値が正しくなければ、ここで起動時にエラーになる
    task_auth = task_auth or get_task_authenticator()

    clock = clock or (lambda: datetime.now(UTC))
    daily_limit = get_daily_upload_limit()

    # 本番（gcp）では API の仕様の画面を公開しない
    if get_backend_name() == "gcp":
        app = FastAPI(
            title="sky-weather-collection", docs_url=None, redoc_url=None, openapi_url=None
        )
    else:
        app = FastAPI(title="sky-weather-collection")
    app.add_middleware(UploadGuard, repository=repository)

    def current_user(request: Request) -> User:
        # アップロードは UploadGuard が照合済み。同じリクエストで2回問い合わせない
        guarded = getattr(request.state, "user", None)
        if guarded is not None:
            return guarded
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
        if not data.startswith(JPEG_MAGIC):
            raise HTTPException(422, "image が JPEG の形式ではありません")

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

        # 再送ではない新規の観測だけが、1日の上限の対象になる
        now = clock()
        day = now.strftime("%Y%m%d")
        if repository.get_daily_count(user.user_id, day) >= daily_limit:
            next_midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
            retry_after = max(1, math.ceil((next_midnight - now).total_seconds()))
            raise HTTPException(
                429,
                "1日に送れる観測の数の上限を超えました。明日の0時（UTC）以降にもう一度送ってください",
                headers={"Retry-After": str(retry_after)},
            )

        # キーにハッシュを含めて、同じ ID で別の画像が同時に来ても上書きし合わないようにする
        image_key = _image_key(observation_id, meta.image_sha256)
        blob_store.put(image_key, data)
        record = meta.with_server_fields(user.user_id, now, image_key)
        if not repository.add_observation(record):
            # 同時に同じ ID が登録された場合
            existing = repository.get_observation(observation_id)
            if existing is None:
                raise HTTPException(500, "保存に失敗しました")
            return exists_response(existing, user, meta)
        repository.increment_daily_count(user.user_id, day)
        reserve_weather_jobs(record)
        return JSONResponse({"observation_id": observation_id, "status": "created"}, 201)

    @app.get("/v1/observations/{observation_id}")
    def get_observation(observation_id: ObservationId, user: CurrentUser) -> dict:
        observation_id = observation_id.lower()
        record = repository.get_observation(observation_id)
        if record is None or record["user_id"] != user.user_id:
            raise HTTPException(404, "観測が見つかりません")
        return _observation_view(record)

    @app.get("/v1/me/observations")
    def list_my_observations(
        user: CurrentUser,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: str | None = None,
    ) -> dict:
        cursor = None if before is None else _decode_cursor(before)
        # 次のページがあるかを知るために、1件多く読む
        records = repository.list_observations_page(user.user_id, limit + 1, cursor)
        page = records[:limit]
        next_before = _encode_cursor(page[-1]) if len(records) > limit else None
        return {"observations": [_observation_view(r) for r in page], "next_before": next_before}

    @app.get("/v1/me/stats")
    def my_stats(user: CurrentUser) -> dict:
        return _stats(repository.list_observations_by_user(user.user_id))

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


def _observation_view(record: dict) -> dict:
    """観測の表示の形（仕様 4.1節）にする。"""
    from sky_server.summary import is_correct

    answer = record.get("answer") or UNKNOWN_ANSWER
    return {
        "observation_id": record["observation_id"],
        "received_at": record["received_at"],
        "captured_at": record["captured_at"],
        "user_guess": record.get("user_guess"),
        "weather_at_capture": record.get("weather_at_capture"),
        "answer": answer,
        "correct": is_correct(record.get("user_guess"), answer),
    }


def _stats(records: list[dict]) -> dict:
    """観測の数え上げ（仕様 4.4節）。"""
    from sky_server.summary import is_correct

    by_category = dict.fromkeys(CATEGORIES, 0)
    answered = guesses = correct = 0
    for record in records:
        weather = record.get("weather_at_capture")
        category = weather["category"] if weather else "unknown"
        by_category[category if category in by_category else "unknown"] += 1
        answer = record.get("answer") or UNKNOWN_ANSWER
        if answer["result"] == "unknown":
            continue
        answered += 1
        if record.get("user_guess") is not None:
            guesses += 1
            correct += is_correct(record["user_guess"], answer) is True
    return {
        "observations_total": len(records),
        "answered_total": answered,
        "guesses_total": guesses,
        "guesses_correct": correct,
        "by_category": by_category,
    }


def _encode_cursor(record: dict) -> str:
    text = json.dumps([record["captured_at_utc"], record["observation_id"]])
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """カーソルを `(captured_at_utc, observation_id)` に戻す。読めなければ 422。"""
    invalid = HTTPException(422, "before が正しいカーソルではありません")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (binascii.Error, ValueError) as e:
        raise invalid from e
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) for item in value)
        or not re.fullmatch(CAPTURED_AT_UTC_PATTERN, value[0])
        or not re.fullmatch(UUID_PATTERN, value[1])
    ):
        raise invalid
    return value[0], value[1]
