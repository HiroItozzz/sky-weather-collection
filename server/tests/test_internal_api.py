import pytest
from conftest import OBSERVATION_ID, Api
from fastapi.testclient import TestClient

from sky_server.main import create_app
from sky_server.storage import LocalObservationRepository

FORECAST_ID = f"{OBSERVATION_ID}_forecast"
URL = "/internal/tasks/fetch-weather"


def make_client(data_dir, monkeypatch, auth: str | None, **kwargs) -> TestClient:
    if auth is None:
        monkeypatch.delenv("SKY_TASK_AUTH", raising=False)
    else:
        monkeypatch.setenv("SKY_TASK_AUTH", auth)
    return TestClient(create_app(data_dir, **kwargs), raise_server_exceptions=False)


def test_既定では認証に失敗して401(data_dir, monkeypatch):
    client = make_client(data_dir, monkeypatch, None)
    assert client.post(URL, json={"job_id": FORECAST_ID}).status_code == 401


def test_認証が通らなければ本文が不正でも401(data_dir, monkeypatch):
    client = make_client(data_dir, monkeypatch, None)
    assert client.post(URL, json={"job_id": "bad"}).status_code == 401


def test_noneなら200でジョブがなければ無視される(data_dir, monkeypatch):
    client = make_client(data_dir, monkeypatch, "none")
    res = client.post(URL, json={"job_id": FORECAST_ID})
    assert res.status_code == 200
    assert res.json() == {
        "job_id": FORECAST_ID,
        "result": "ignored",
        "reason": "job_not_found",
        "status": None,
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"job_id": "bad"},
        {"job_id": OBSERVATION_ID},
        {"job_id": f"{OBSERVATION_ID}_other"},
        {"job_id": 1},
    ],
)
def test_本文の形式が違えば422(data_dir, monkeypatch, body):
    client = make_client(data_dir, monkeypatch, "none")
    assert client.post(URL, json=body).status_code == 422


def test_期限が来たジョブを実行して状態を返す(data_dir, monkeypatch):
    from sky_server.jobs import (
        JobRunner,
        LocalJobRepository,
        LocalTaskScheduler,
    )

    observations = LocalObservationRepository(data_dir)
    jobs = LocalJobRepository(data_dir)
    scheduler = LocalTaskScheduler(data_dir)
    runner = JobRunner(
        observations,
        jobs,
        scheduler,
        {"open_meteo": lambda **kw: "raw/open_meteo/x.json.gz", "amedas": lambda **kw: "y"},
    )
    client = make_client(
        data_dir,
        monkeypatch,
        "none",
        repository=observations,
        job_repository=jobs,
        scheduler=scheduler,
        runner=runner,
    )
    api = Api(client, observations)
    assert api.put(api.new_user()[1]).status_code == 201
    res = client.post(URL, json={"job_id": FORECAST_ID})
    assert res.json() == {"job_id": FORECAST_ID, "result": "ran", "reason": None, "status": "done"}
    again = client.post(URL, json={"job_id": FORECAST_ID})
    assert again.json()["reason"] == "already_finished"


def test_再試行の予約に失敗したら500(data_dir, monkeypatch):
    from sky_server.jobs import JobRunner, LocalJobRepository, LocalTaskScheduler
    from sky_server.weather.common import RetryableError

    class BrokenAfterPut(LocalTaskScheduler):
        broken = False

        def schedule(self, job_id, run_at):
            if self.broken:
                raise RuntimeError("予約できない")
            super().schedule(job_id, run_at)

    def unavailable(**kwargs):
        raise RetryableError("HTTP 503")

    observations = LocalObservationRepository(data_dir)
    jobs = LocalJobRepository(data_dir)
    scheduler = BrokenAfterPut(data_dir)
    fetchers = {"open_meteo": unavailable, "amedas": unavailable}
    runner = JobRunner(observations, jobs, scheduler, fetchers)
    client = make_client(
        data_dir,
        monkeypatch,
        "none",
        repository=observations,
        job_repository=jobs,
        scheduler=scheduler,
        runner=runner,
    )
    api = Api(client, observations)
    api.put(api.new_user()[1])
    scheduler.broken = True
    assert client.post(URL, json={"job_id": FORECAST_ID}).status_code == 500
    assert jobs.get_job(FORECAST_ID).enqueued is False


def test_認証を引数で渡せる(data_dir, monkeypatch):
    from fastapi import HTTPException

    def only_secret(request):
        if request.headers.get("X-Secret") != "ok":
            raise HTTPException(401, "だめ")

    client = make_client(data_dir, monkeypatch, "none", task_auth=only_secret)
    assert client.post(URL, json={"job_id": FORECAST_ID}).status_code == 401
    res = client.post(URL, json={"job_id": FORECAST_ID}, headers={"X-Secret": "ok"})
    assert res.status_code == 200


def test_SKY_TASK_AUTHが不明な値なら起動時にエラー(data_dir, monkeypatch):
    monkeypatch.setenv("SKY_TASK_AUTH", "bogus")
    with pytest.raises(ValueError, match="SKY_TASK_AUTH"):
        create_app(data_dir)
