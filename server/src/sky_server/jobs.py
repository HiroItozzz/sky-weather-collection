"""天気データの取得ジョブ：状態の保存、実行の予約、実行（M4：ローカル版）。"""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from sky_server.config import get_amedas_enabled
from sky_server.storage import BlobStore, ObservationRepository, _create_exclusive, _write_atomic
from sky_server.weather.amedas import fetch_amedas
from sky_server.weather.common import Clock, PermanentError, ceil_hour, create_client, utc_now
from sky_server.weather.open_meteo import fetch_open_meteo

PHASES = ("forecast", "label")
PROVIDER_ORDER = ("open_meteo", "amedas")
PHASE_PROVIDERS = {
    "forecast": ("open_meteo",),
    "label": ("open_meteo", "amedas"),
}

# ラベルは、取得する範囲の終わり（ceil_hour(撮影 + 3時間)）からさらにこれだけ待って取る。
# Open-Meteo の過去の値は最新のモデル実行から来るので、待つほど実況に近くなる
LABEL_RANGE_END = timedelta(hours=3)
LABEL_WAIT = timedelta(hours=3)
MAX_ATTEMPTS = 6
BASE_WAIT = timedelta(minutes=5)
MAX_WAIT = timedelta(hours=2)
LAST_ERROR_LIMIT = 1000

# 取得関数：observation_id, phase, captured_at, lat, lon, clock をキーワード引数で受け取り、
# blob_key を返す
Fetcher = Callable[..., str]


class ProviderState(BaseModel):
    status: Literal["pending", "done", "failed", "disabled"] = "pending"
    blob_key: str | None = None
    completed_at: AwareDatetime | None = None
    error: str | None = None


class WeatherJob(BaseModel):
    job_id: str
    observation_id: str
    phase: Literal["forecast", "label"]
    status: Literal["pending", "done", "failed", "skipped"] = "pending"
    created_at: AwareDatetime
    run_at: AwareDatetime
    next_attempt_at: AwareDatetime | None
    enqueued: bool = False
    attempts: int = 0
    max_attempts: int = MAX_ATTEMPTS
    last_attempt_at: AwareDatetime | None = None
    last_error: str | None = None
    completed_at: AwareDatetime | None = None
    skip_reason: str | None = None
    providers: dict[str, ProviderState]


class JobRepository(ABC):
    """ジョブの状態の保存先。"""

    @abstractmethod
    def add_job(self, job: WeatherJob) -> bool:
        """ジョブを保存する。同じ `job_id` がすでにあれば保存せず False を返す。"""

    @abstractmethod
    def get_job(self, job_id: str) -> WeatherJob | None: ...

    @abstractmethod
    def update_job(self, job: WeatherJob) -> None: ...


class TaskScheduler(ABC):
    """「この時刻にこのジョブを実行してほしい」という予約。"""

    @abstractmethod
    def schedule(self, job_id: str, run_at: datetime) -> None:
        """予約する。失敗したら例外を投げる。"""


class LocalJobRepository(JobRepository):
    """1件1ファイルの JSON で保存する。"""

    def __init__(self, root: Path) -> None:
        self._dir = root / "jobs"
        self._dir.mkdir(parents=True, exist_ok=True)

    def add_job(self, job: WeatherJob) -> bool:
        path = self._dir / f"{job.job_id}.json"
        return _create_exclusive(path, job.model_dump_json(indent=2).encode())

    def get_job(self, job_id: str) -> WeatherJob | None:
        path = self._dir / f"{job_id}.json"
        if not path.exists():
            return None
        return WeatherJob.model_validate_json(path.read_text(encoding="utf-8"))

    def update_job(self, job: WeatherJob) -> None:
        _write_atomic(self._dir / f"{job.job_id}.json", job.model_dump_json(indent=2).encode())


class LocalTaskScheduler(TaskScheduler):
    """予約を `tasks/{job_id}.json` に書く。実行は管理コマンドが行う。"""

    def __init__(self, root: Path) -> None:
        self._dir = root / "tasks"
        self._dir.mkdir(parents=True, exist_ok=True)

    def schedule(self, job_id: str, run_at: datetime) -> None:
        body = {"job_id": job_id, "run_at": run_at.astimezone(UTC).isoformat()}
        _write_atomic(self._dir / f"{job_id}.json", json.dumps(body).encode())

    def due(self, now: datetime) -> list[tuple[str, datetime]]:
        """期限の来た予約を、`run_at` の古い順に `(job_id, run_at)` で返す。"""
        found = []
        for path in self._dir.glob("*.json"):
            body = json.loads(path.read_text(encoding="utf-8"))
            run_at = datetime.fromisoformat(body["run_at"])
            if run_at <= now:
                found.append((body["job_id"], run_at))
        return sorted(found, key=lambda item: item[1])

    def remove(self, job_id: str, run_at: datetime) -> None:
        """予約の `run_at` が一致するときだけ消す。予約し直されていたら残す。"""
        path = self._dir / f"{job_id}.json"
        if not path.exists():
            return
        body = json.loads(path.read_text(encoding="utf-8"))
        if datetime.fromisoformat(body["run_at"]) == run_at:
            path.unlink(missing_ok=True)


def wait_time(attempts: int) -> timedelta:
    """`attempts` 回目の失敗のあと、次の試行まで待つ時間。5分から倍々に増やし、上限は2時間。"""
    exponent = min(max(attempts - 1, 0), 10)
    return min(BASE_WAIT * 2**exponent, MAX_WAIT)


def _parse_time(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _new_job(record: dict, phase: str, now: datetime) -> WeatherJob:
    observation_id = record["observation_id"]
    received_at = _parse_time(record["received_at"])
    if phase == "forecast":
        run_at = received_at
    else:
        range_end = ceil_hour(_parse_time(record["captured_at"]) + LABEL_RANGE_END)
        run_at = max(range_end + LABEL_WAIT, received_at)
    job = WeatherJob(
        job_id=f"{observation_id}_{phase}",
        observation_id=observation_id,
        phase=phase,
        created_at=now,
        run_at=run_at,
        next_attempt_at=run_at,
        providers={name: ProviderState() for name in PHASE_PROVIDERS[phase]},
    )
    if record.get("location") is None:
        _finish(job, "skipped", now, skip_reason="no_location")
    return job


def _enqueue(job: WeatherJob, jobs: JobRepository, scheduler: TaskScheduler) -> None:
    """予約して、成功したら `enqueued` を true にして保存する。失敗したら例外をそのまま投げる。"""
    scheduler.schedule(job.job_id, job.next_attempt_at)
    job.enqueued = True
    jobs.update_job(job)


def ensure_weather_jobs(
    record: dict,
    jobs: JobRepository,
    scheduler: TaskScheduler,
    now: datetime | None = None,
) -> None:
    """観測の2つのジョブを用意し、予約できていないものを予約する。何度呼んでも結果は同じ。

    予約に失敗したら例外を投げる（呼び出し側が 503 にする）。
    """
    now = now or datetime.now(UTC)
    # 先に2つとも作ってから予約する。片方の予約に失敗しても、もう片方のジョブは残るようにする
    ensured = []
    for phase in PHASES:
        job = _new_job(record, phase, now)
        if not jobs.add_job(job):
            job = jobs.get_job(job.job_id)
        ensured.append(job)
    for job in ensured:
        if job.status == "pending" and not job.enqueued:
            _enqueue(job, jobs, scheduler)


def default_fetchers(blob_store: BlobStore) -> dict[str, Fetcher]:
    """実際の HTTP で取得する関数の対応表を作る。"""

    def open_meteo(**kwargs) -> str:
        with create_client() as client:
            return fetch_open_meteo(client, blob_store, **kwargs)

    def amedas(**kwargs) -> str:
        with create_client() as client:
            return fetch_amedas(client, blob_store, **kwargs)

    return {"open_meteo": open_meteo, "amedas": amedas}


class JobRunner:
    """ジョブを1回実行して、状態を更新し、必要なら予約し直す。"""

    def __init__(
        self,
        observations: ObservationRepository,
        jobs: JobRepository,
        scheduler: TaskScheduler,
        fetchers: dict[str, Fetcher],
        amedas_enabled: bool | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._observations = observations
        self._jobs = jobs
        self._scheduler = scheduler
        self._fetchers = fetchers
        self._amedas_enabled = get_amedas_enabled() if amedas_enabled is None else amedas_enabled
        # 取得関数に渡す時計。封筒の取得時刻やラベルの範囲の確認は、ジョブの now ではなくこれで取る
        self._clock = clock

    def run(self, job_id: str, now: datetime | None = None) -> tuple[str, str | None]:
        """戻り値は `("ran" | "ignored", 理由 | None)`。"""
        now = now or datetime.now(UTC)
        job = self._jobs.get_job(job_id)
        if job is None:
            return "ignored", "job_not_found"
        if job.status != "pending":
            return "ignored", "already_finished"
        if now < job.next_attempt_at:
            if not job.enqueued:
                _enqueue(job, self._jobs, self._scheduler)
            return "ignored", "not_due"

        record = self._observations.get_observation(job.observation_id)
        if record is None:
            job.last_error = "observation_not_found"
            _finish(job, "failed", now)
            self._jobs.update_job(job)
            return "ran", "observation_not_found"
        location = record.get("location")
        if location is None:
            _finish(job, "skipped", now, skip_reason="no_location")
            self._jobs.update_job(job)
            return "ran", "no_location"

        job.attempts += 1
        job.last_attempt_at = now
        captured_at = _parse_time(record["captured_at"])
        for name in PROVIDER_ORDER:
            state = job.providers.get(name)
            if state is None or state.status != "pending":
                continue
            if name == "amedas" and not self._amedas_enabled:
                state.status = "disabled"
                continue
            try:
                state.blob_key = self._fetchers[name](
                    observation_id=job.observation_id,
                    phase=job.phase,
                    captured_at=captured_at,
                    lat=location["lat"],
                    lon=location["lon"],
                    clock=self._clock,
                )
            except PermanentError as e:
                state.status = "failed"
                state.error = _describe(e)
                job.last_error = _limit(f"{name}: {state.error}")
            except Exception as e:
                # RetryableError と、想定外の例外（応答の形が違うなど）。想定外のものも
                # 再試行に回し、待ち時間と max_attempts が効くようにする
                state.error = _describe(e)
                job.last_error = _limit(f"{name}: {state.error}")
            else:
                state.status = "done"
                state.completed_at = now
                state.error = None

        pending = [state for state in job.providers.values() if state.status == "pending"]
        if not pending:
            failed = any(state.status == "failed" for state in job.providers.values())
            _finish(job, "failed" if failed else "done", now)
            self._jobs.update_job(job)
        elif job.attempts >= job.max_attempts:
            for state in pending:
                state.status = "failed"
                state.error = f"{state.error}; max_attempts" if state.error else "max_attempts"
            _finish(job, "failed", now)
            self._jobs.update_job(job)
        else:
            job.next_attempt_at = now + wait_time(job.attempts)
            job.enqueued = False
            self._jobs.update_job(job)
            _enqueue(job, self._jobs, self._scheduler)
        return "ran", None


def _finish(
    job: WeatherJob,
    status: Literal["done", "failed", "skipped"],
    now: datetime,
    skip_reason: str | None = None,
) -> None:
    """ジョブを完了にする。完了したジョブは `completed_at` があり、`next_attempt_at` は null。"""
    job.status = status
    job.completed_at = now
    job.next_attempt_at = None
    job.skip_reason = skip_reason


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _limit(text: str) -> str:
    return text[:LAST_ERROR_LIMIT]
