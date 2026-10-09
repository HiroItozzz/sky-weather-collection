import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import IMAGE, OBSERVATION_ID, Api, make_metadata
from fastapi.testclient import TestClient

from sky_server.jobs import (
    JobRunner,
    LocalJobRepository,
    LocalTaskScheduler,
    ensure_weather_jobs,
    wait_time,
)
from sky_server.main import create_app
from sky_server.models import ObservationMetadata
from sky_server.storage import LocalObservationRepository
from sky_server.weather.common import PermanentError, RetryableError

CAPTURED_AT = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
RECEIVED_AT = CAPTURED_AT + timedelta(minutes=1)
FORECAST_ID = f"{OBSERVATION_ID}_forecast"
LABEL_ID = f"{OBSERVATION_ID}_label"


class FakeFetcher:
    """取得関数の偽物。`outcomes` を先頭から1つずつ使い、尽きたら成功する。"""

    def __init__(self, name: str, outcomes: list[Exception | None] | None = None) -> None:
        self.name = name
        self.outcomes = list(outcomes or [])
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return f"raw/{self.name}/{kwargs['observation_id']}/{kwargs['phase']}.json.gz"


class FailingScheduler(LocalTaskScheduler):
    """`failing` が True の間は予約に失敗する。"""

    def __init__(self, root) -> None:
        super().__init__(root)
        self.failing = False

    def schedule(self, job_id, run_at) -> None:
        if self.failing:
            raise RuntimeError("予約できない")
        super().schedule(job_id, run_at)


def make_record(location: bool = True) -> dict:
    meta = ObservationMetadata.model_validate(
        {**make_metadata(), "captured_at": CAPTURED_AT.isoformat()}
    )
    record = meta.with_server_fields("user", RECEIVED_AT, "key.jpg")
    if not location:
        record["location"] = None
    return record


class Env:
    """ジョブのテストに使う部品をまとめたもの。"""

    def __init__(self, root, amedas_enabled: bool = True) -> None:
        self.observations = LocalObservationRepository(root)
        self.jobs = LocalJobRepository(root)
        self.scheduler = FailingScheduler(root)
        self.open_meteo = FakeFetcher("open_meteo")
        self.amedas = FakeFetcher("amedas")
        self.runner = JobRunner(
            self.observations,
            self.jobs,
            self.scheduler,
            {"open_meteo": self.open_meteo, "amedas": self.amedas},
            amedas_enabled=amedas_enabled,
        )

    def add_observation(self, location: bool = True) -> dict:
        record = make_record(location)
        self.observations.add_observation(record)
        return record

    def ensure(self, record: dict) -> None:
        ensure_weather_jobs(record, self.jobs, self.scheduler, RECEIVED_AT)


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


LABEL_RUN_AT = CAPTURED_AT + timedelta(hours=3, minutes=30)


def test_待ち時間は5分から倍に増えて2時間で止まる():
    assert [wait_time(n) for n in range(1, 8)] == [
        timedelta(minutes=5),
        timedelta(minutes=10),
        timedelta(minutes=20),
        timedelta(minutes=40),
        timedelta(minutes=80),
        timedelta(hours=2),
        timedelta(hours=2),
    ]
    assert wait_time(10_000) == timedelta(hours=2)


def test_2つのジョブが作られて予約される(env):
    env.ensure(env.add_observation())
    forecast = env.jobs.get_job(FORECAST_ID)
    label = env.jobs.get_job(LABEL_ID)
    assert forecast.run_at == RECEIVED_AT
    assert list(forecast.providers) == ["open_meteo"]
    assert label.run_at == LABEL_RUN_AT
    assert list(label.providers) == ["open_meteo", "amedas"]
    assert forecast.enqueued and label.enqueued
    assert forecast.status == label.status == "pending"
    assert forecast.max_attempts == 6
    far_future = LABEL_RUN_AT + timedelta(days=1)
    assert env.scheduler.due(far_future) == [(FORECAST_ID, RECEIVED_AT), (LABEL_ID, LABEL_RUN_AT)]


def test_アップロードが遅れたらlabelもすぐ実行する(env):
    record = env.add_observation()
    record["received_at"] = (CAPTURED_AT + timedelta(hours=5)).isoformat()
    env.ensure(record)
    assert env.jobs.get_job(LABEL_ID).run_at == CAPTURED_AT + timedelta(hours=5)


def test_何度呼んでもジョブも予約も増えない(env):
    record = env.add_observation()
    env.ensure(record)
    before = env.jobs.get_job(LABEL_ID)
    env.ensure(record)
    assert env.jobs.get_job(LABEL_ID) == before
    assert len(env.scheduler.due(LABEL_RUN_AT)) == 2


def test_位置がなければskippedで予約しない(env):
    env.ensure(env.add_observation(location=False))
    for job_id in (FORECAST_ID, LABEL_ID):
        job = env.jobs.get_job(job_id)
        assert job.status == "skipped"
        assert job.skip_reason == "no_location"
        assert not job.enqueued
    assert env.scheduler.due(LABEL_RUN_AT) == []


def test_予約に失敗したら例外で次の呼び出しで立ち直る(env):
    record = env.add_observation()
    env.scheduler.failing = True
    with pytest.raises(RuntimeError):
        env.ensure(record)
    assert env.jobs.get_job(FORECAST_ID).enqueued is False
    env.scheduler.failing = False
    env.ensure(record)
    assert env.jobs.get_job(FORECAST_ID).enqueued is True
    assert env.jobs.get_job(LABEL_ID).enqueued is True


def test_forecastの成功(env):
    env.ensure(env.add_observation())
    assert env.runner.run(FORECAST_ID, RECEIVED_AT) == ("ran", None)
    job = env.jobs.get_job(FORECAST_ID)
    assert job.status == "done"
    assert job.attempts == 1
    assert job.last_attempt_at == RECEIVED_AT
    assert job.completed_at == RECEIVED_AT
    assert job.next_attempt_at is None
    state = job.providers["open_meteo"]
    assert state.status == "done"
    assert state.blob_key == f"raw/open_meteo/{OBSERVATION_ID}/forecast.json.gz"
    assert state.completed_at == RECEIVED_AT
    call = env.open_meteo.calls[0]
    assert call["captured_at"] == CAPTURED_AT
    assert (call["lat"], call["lon"]) == (35.68, 139.76)
    assert call["phase"] == "forecast"
    assert env.amedas.calls == []


def test_labelは2つのプロバイダーを取得する(env):
    env.ensure(env.add_observation())
    env.runner.run(LABEL_ID, LABEL_RUN_AT)
    job = env.jobs.get_job(LABEL_ID)
    assert job.status == "done"
    assert job.providers["amedas"].blob_key == f"raw/amedas/{OBSERVATION_ID}/label.json.gz"
    assert len(env.open_meteo.calls) == len(env.amedas.calls) == 1


def test_ジョブがなければ無視する(env):
    assert env.runner.run(FORECAST_ID, RECEIVED_AT) == ("ignored", "job_not_found")


def test_二重の配達は無視して取り直さない(env):
    env.ensure(env.add_observation())
    env.runner.run(FORECAST_ID, RECEIVED_AT)
    assert env.runner.run(FORECAST_ID, RECEIVED_AT) == ("ignored", "already_finished")
    assert len(env.open_meteo.calls) == 1
    assert env.jobs.get_job(FORECAST_ID).attempts == 1


def test_余裕の2分より前なら実行しない(env):
    env.ensure(env.add_observation())
    too_early = LABEL_RUN_AT - timedelta(minutes=2) - timedelta(seconds=1)
    assert env.runner.run(LABEL_ID, too_early) == ("ignored", "not_due")
    assert env.open_meteo.calls == []
    assert env.jobs.get_job(LABEL_ID).attempts == 0


def test_予定の2分前までなら早く届いても実行する(env):
    env.ensure(env.add_observation())
    assert env.runner.run(LABEL_ID, LABEL_RUN_AT - timedelta(minutes=2)) == ("ran", None)
    assert len(env.open_meteo.calls) == 1
    assert env.jobs.get_job(LABEL_ID).status == "done"


def test_ちょうど期限なら実行する(env):
    env.ensure(env.add_observation())
    assert env.runner.run(LABEL_ID, LABEL_RUN_AT) == ("ran", None)
    assert len(env.open_meteo.calls) == 1


def test_期限前でも予約が済んでいなければ予約し直す(env):
    record = env.add_observation()
    env.scheduler.failing = True
    with pytest.raises(RuntimeError):
        env.ensure(record)
    env.scheduler.failing = False
    assert env.runner.run(LABEL_ID, CAPTURED_AT) == ("ignored", "not_due")
    assert env.jobs.get_job(LABEL_ID).enqueued is True
    assert env.scheduler.due(LABEL_RUN_AT)[-1] == (LABEL_ID, LABEL_RUN_AT)


def test_観測がなければfailed(env):
    env.ensure(env.add_observation())
    (env.observations._observations / f"{OBSERVATION_ID}.json").unlink()
    assert env.runner.run(FORECAST_ID, RECEIVED_AT) == ("ran", "observation_not_found")
    job = env.jobs.get_job(FORECAST_ID)
    assert job.status == "failed"
    assert job.last_error == "observation_not_found"
    assert env.open_meteo.calls == []


def test_実行時に位置がなければskipped(env):
    env.ensure(env.add_observation())
    path = env.observations._observations / f"{OBSERVATION_ID}.json"
    record = json.loads(path.read_text())
    record["location"] = None
    path.write_text(json.dumps(record))
    assert env.runner.run(FORECAST_ID, RECEIVED_AT) == ("ran", "no_location")
    job = env.jobs.get_job(FORECAST_ID)
    assert (job.status, job.skip_reason) == ("skipped", "no_location")


def test_再試行できない失敗はfailedになる(env):
    env.open_meteo.outcomes = [PermanentError("HTTP 400: bad")]
    env.ensure(env.add_observation())
    env.runner.run(FORECAST_ID, RECEIVED_AT)
    job = env.jobs.get_job(FORECAST_ID)
    assert job.status == "failed"
    assert job.providers["open_meteo"].status == "failed"
    assert "HTTP 400: bad" in job.last_error
    assert job.completed_at == RECEIVED_AT


def test_再試行できる失敗は待ち時間をあけて予約し直す(env):
    env.open_meteo.outcomes = [RetryableError("HTTP 503")]
    env.ensure(env.add_observation())
    env.runner.run(FORECAST_ID, RECEIVED_AT)
    job = env.jobs.get_job(FORECAST_ID)
    assert job.status == "pending"
    assert job.attempts == 1
    assert job.next_attempt_at == RECEIVED_AT + timedelta(minutes=5)
    assert job.enqueued is True
    assert "HTTP 503" in job.last_error
    assert job.providers["open_meteo"].status == "pending"
    assert job.providers["open_meteo"].error
    assert env.scheduler.due(RECEIVED_AT) == []
    assert env.scheduler.due(job.next_attempt_at) == [(FORECAST_ID, job.next_attempt_at)]


def test_想定外の例外も再試行に回して回数を数える(env):
    env.open_meteo.outcomes = [KeyError("lat")]
    env.ensure(env.add_observation())
    assert env.runner.run(FORECAST_ID, RECEIVED_AT) == ("ran", None)
    job = env.jobs.get_job(FORECAST_ID)
    assert job.status == "pending"
    assert job.attempts == 1
    assert job.next_attempt_at == RECEIVED_AT + timedelta(minutes=5)
    assert "KeyError" in job.last_error


def test_再試行の予約に失敗したら例外を投げ次の配達で予約し直す(env):
    env.open_meteo.outcomes = [RetryableError("HTTP 503")]
    env.ensure(env.add_observation())
    env.scheduler.failing = True
    with pytest.raises(RuntimeError):
        env.runner.run(FORECAST_ID, RECEIVED_AT)
    job = env.jobs.get_job(FORECAST_ID)
    assert job.enqueued is False
    assert job.next_attempt_at == RECEIVED_AT + timedelta(minutes=5)
    env.scheduler.failing = False
    assert env.runner.run(FORECAST_ID, RECEIVED_AT) == ("ignored", "not_due")
    assert env.jobs.get_job(FORECAST_ID).enqueued is True


def test_待ち時間の上限は2時間(env):
    env.open_meteo.outcomes = [RetryableError("x")] * 5
    env.ensure(env.add_observation())
    now = RECEIVED_AT
    waits = []
    for _ in range(5):
        env.runner.run(FORECAST_ID, now)
        next_at = env.jobs.get_job(FORECAST_ID).next_attempt_at
        waits.append(next_at - now)
        now = next_at
    assert waits == [timedelta(minutes=m) for m in (5, 10, 20, 40, 80)]
    # max_attempts を増やして、6回目以降の待ち時間も見る
    job = env.jobs.get_job(FORECAST_ID)
    job.max_attempts = 10
    env.jobs.update_job(job)
    env.open_meteo.outcomes = [RetryableError("x")] * 2
    env.runner.run(FORECAST_ID, now)
    assert env.jobs.get_job(FORECAST_ID).next_attempt_at - now == timedelta(hours=2)


def test_max_attemptsちょうどでfailedになる(env):
    env.open_meteo.outcomes = [RetryableError("HTTP 503")] * 6
    env.ensure(env.add_observation())
    now = RECEIVED_AT
    for n in range(1, 7):
        env.runner.run(FORECAST_ID, now)
        job = env.jobs.get_job(FORECAST_ID)
        if n < 6:
            assert job.status == "pending"
            now = job.next_attempt_at
    assert job.attempts == 6
    assert job.status == "failed"
    assert job.providers["open_meteo"].status == "failed"
    assert job.providers["open_meteo"].error.endswith("max_attempts")
    assert job.completed_at == now
    assert job.next_attempt_at is None
    assert env.runner.run(FORECAST_ID, now) == ("ignored", "already_finished")


def test_一部のプロバイダーだけ成功したら再試行では取り直さない(env):
    env.amedas.outcomes = [RetryableError("HTTP 503")]
    env.ensure(env.add_observation())
    env.runner.run(LABEL_ID, LABEL_RUN_AT)
    job = env.jobs.get_job(LABEL_ID)
    assert job.status == "pending"
    assert job.providers["open_meteo"].status == "done"
    assert job.providers["amedas"].status == "pending"
    assert job.last_error.startswith("amedas")
    env.runner.run(LABEL_ID, job.next_attempt_at)
    job = env.jobs.get_job(LABEL_ID)
    assert job.status == "done"
    assert job.attempts == 2
    assert len(env.open_meteo.calls) == 1
    assert len(env.amedas.calls) == 2


def test_片方が失敗してももう片方は取得する(env):
    env.open_meteo.outcomes = [RetryableError("HTTP 503")]
    env.ensure(env.add_observation())
    env.runner.run(LABEL_ID, LABEL_RUN_AT)
    job = env.jobs.get_job(LABEL_ID)
    assert job.providers["open_meteo"].status == "pending"
    assert job.providers["amedas"].status == "done"


def test_アメダスを無効にするとdisabledになる(tmp_path):
    env = Env(tmp_path, amedas_enabled=False)
    env.ensure(env.add_observation())
    env.runner.run(LABEL_ID, LABEL_RUN_AT)
    job = env.jobs.get_job(LABEL_ID)
    assert job.status == "done"
    assert job.providers["amedas"].status == "disabled"
    assert env.amedas.calls == []


def test_last_errorは1000文字まで(env):
    env.open_meteo.outcomes = [RetryableError("あ" * 5000)]
    env.ensure(env.add_observation())
    env.runner.run(FORECAST_ID, RECEIVED_AT)
    assert len(env.jobs.get_job(FORECAST_ID).last_error) == 1000


def test_予約は古い順に返りrun_atが一致するときだけ消える(tmp_path):
    scheduler = LocalTaskScheduler(tmp_path)
    t0 = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
    scheduler.schedule("b", t0 + timedelta(minutes=1))
    scheduler.schedule("a", t0 + timedelta(minutes=2))
    scheduler.schedule("c", t0 + timedelta(minutes=3))
    assert scheduler.due(t0 + timedelta(minutes=2)) == [
        ("b", t0 + timedelta(minutes=1)),
        ("a", t0 + timedelta(minutes=2)),
    ]
    # 同じジョブを予約し直すと上書きされる
    scheduler.schedule("b", t0 + timedelta(minutes=10))
    scheduler.remove("b", t0 + timedelta(minutes=1))
    assert scheduler.due(t0 + timedelta(minutes=10))[-1] == ("b", t0 + timedelta(minutes=10))
    scheduler.remove("b", t0 + timedelta(minutes=10))
    assert [job_id for job_id, _ in scheduler.due(t0 + timedelta(hours=1))] == ["a", "c"]


def test_add_jobは同じIDなら保存せずFalse(env):
    env.ensure(env.add_observation())
    job = env.jobs.get_job(FORECAST_ID)
    assert env.jobs.add_job(job) is False
    assert env.jobs.get_job("nothing") is None


@pytest.fixture
def put_env(tmp_path):
    """予約に失敗させられる状態で、PUT まで通して試す。"""
    env = Env(tmp_path / "data")
    app = create_app(
        tmp_path / "data",
        repository=env.observations,
        job_repository=env.jobs,
        scheduler=env.scheduler,
        runner=env.runner,
    )
    api = Api(TestClient(app), env.observations)
    return env, api, api.new_user()[1]


def test_PUTの201で2つのジョブが予約される(put_env):
    env, api, token = put_env
    assert api.put(token).status_code == 201
    assert env.jobs.get_job(FORECAST_ID).enqueued
    assert env.jobs.get_job(LABEL_ID).enqueued
    assert len(env.scheduler.due(datetime.now(UTC) + timedelta(days=1))) == 2


def test_PUTの再送でジョブは増えない(put_env):
    env, api, token = put_env
    api.put(token)
    before = env.jobs.get_job(LABEL_ID)
    assert api.put(token).status_code == 200
    assert env.jobs.get_job(LABEL_ID) == before
    assert len(env.scheduler.due(datetime.now(UTC) + timedelta(days=1))) == 2


def test_PUTで位置がなければskippedのジョブができる(put_env):
    env, api, token = put_env
    metadata = make_metadata()
    metadata["location"] = None
    assert api.put(token, metadata).status_code == 201
    assert env.jobs.get_job(FORECAST_ID).skip_reason == "no_location"
    assert env.scheduler.due(datetime.now(UTC) + timedelta(days=1)) == []


def test_予約に失敗したら503で再送で立ち直る(put_env):
    env, api, token = put_env
    env.scheduler.failing = True
    res = api.put(token)
    assert res.status_code == 503
    # 観測は保存済み
    assert api.repository.get_observation(OBSERVATION_ID) is not None
    assert env.jobs.get_job(FORECAST_ID).enqueued is False
    env.scheduler.failing = False
    assert api.put(token).status_code == 200
    assert env.jobs.get_job(FORECAST_ID).enqueued
    assert env.jobs.get_job(LABEL_ID).enqueued


def test_再送でも予約に失敗したら503(put_env):
    env, api, token = put_env
    api.put(token)
    job = env.jobs.get_job(LABEL_ID)
    job.enqueued = False
    env.jobs.update_job(job)
    env.scheduler.failing = True
    assert api.put(token).status_code == 503


def test_409と422のときはジョブを作らない(put_env):
    env, api, token = put_env
    api.put(token)
    other = api.new_user("other")[1]
    env.jobs._dir.joinpath(f"{FORECAST_ID}.json").unlink()
    env.jobs._dir.joinpath(f"{LABEL_ID}.json").unlink()
    assert api.put(other).status_code == 409
    assert api.put(token, image=IMAGE + b"x").status_code == 409
    bad = make_metadata()
    bad["schema_version"] = 2
    assert api.put(token, bad).status_code == 422
    assert env.jobs.get_job(FORECAST_ID) is None
    assert env.jobs.get_job(LABEL_ID) is None
