import logging
from datetime import UTC, datetime

import pytest
from conftest import make_metadata
from fastapi import HTTPException
from fastapi.testclient import TestClient
from gcp_fakes import FakeFirestoreClient, FakeStorageClient, FakeTasksClient
from starlette.requests import Request

from sky_server.admin import main
from sky_server.backends import build_backend
from sky_server.config import ConfigError, get_backend_name, get_gcp_settings
from sky_server.gcp.firestore import FirestoreJobRepository, FirestoreObservationRepository
from sky_server.gcp.gcs import GcsBlobStore
from sky_server.gcp.oidc import OidcTaskAuthenticator
from sky_server.gcp.tasks import CloudTasksScheduler
from sky_server.jobs import (
    LocalTaskScheduler,
    WeatherJob,
    ensure_weather_jobs,
)
from sky_server.main import create_app
from sky_server.models import ObservationMetadata, PrivacyZone, User
from sky_server.storage import LocalBlobStore, LocalObservationRepository
from sky_server.task_auth import get_task_authenticator

URL = "https://sky.example.run.app/internal/tasks/fetch-weather"
SERVICE_ACCOUNT = "tasks@proj.iam.gserviceaccount.com"
RUN_AT = datetime(2026, 10, 9, 6, 30, tzinfo=UTC)

GCP_ENV = {
    "SKY_BACKEND": "gcp",
    "SKY_GCP_PROJECT": "proj",
    "SKY_GCS_BUCKET": "bucket",
    "SKY_TASKS_TARGET_URL": URL,
    "SKY_TASKS_SERVICE_ACCOUNT": SERVICE_ACCOUNT,
}


@pytest.fixture
def gcp_env(monkeypatch):
    for name in ("SKY_TASKS_LOCATION", "SKY_TASKS_QUEUE", "SKY_TASK_AUTH"):
        monkeypatch.delenv(name, raising=False)
    for name, value in GCP_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def make_user(token_hash: str = "h1", user_id: str = "u1") -> User:
    return User(user_id=user_id, name="alice", token_hash=token_hash, created_at=RUN_AT)


def make_job(job_id: str = "j1_forecast") -> WeatherJob:
    return WeatherJob(
        job_id=job_id,
        observation_id="j1",
        phase="forecast",
        created_at=RUN_AT,
        run_at=RUN_AT,
        next_attempt_at=RUN_AT,
        providers={},
    )


# Firestore


def test_firestore_撮影者を保存して取得と更新ができる():
    repo = FirestoreObservationRepository(FakeFirestoreClient())
    user = make_user()
    repo.add_user(user)
    assert repo.get_user("u1") == user
    assert repo.get_user("nope") is None
    revoked = user.model_copy(update={"revoked_at": RUN_AT})
    repo.update_user(revoked)
    assert repo.get_user("u1").revoked_at == RUN_AT


def test_firestore_トークンのハッシュで撮影者を探す():
    repo = FirestoreObservationRepository(FakeFirestoreClient())
    repo.add_user(make_user("h1", "u1"))
    repo.add_user(make_user("h2", "u2"))
    assert repo.get_user_by_token_hash("h2").user_id == "u2"
    assert repo.get_user_by_token_hash("h3") is None


def test_firestore_観測は同じIDなら保存せずFalse():
    repo = FirestoreObservationRepository(FakeFirestoreClient())
    assert repo.get_observation("o1") is None
    assert repo.add_observation({"observation_id": "o1", "n": 1}) is True
    assert repo.add_observation({"observation_id": "o1", "n": 2}) is False
    assert repo.get_observation("o1") == {"observation_id": "o1", "n": 1}


def test_firestore_ジョブの作成と取得と更新():
    repo = FirestoreJobRepository(FakeFirestoreClient())
    job = make_job()
    assert repo.get_job(job.job_id) is None
    assert repo.add_job(job) is True
    assert repo.add_job(job.model_copy(update={"attempts": 3})) is False
    assert repo.get_job(job.job_id) == job
    job.attempts = 2
    repo.update_job(job)
    assert repo.get_job(job.job_id).attempts == 2


def test_firestore_コレクションの名前():
    client = FakeFirestoreClient()
    FirestoreObservationRepository(client).add_user(make_user())
    FirestoreObservationRepository(client).add_observation({"observation_id": "o1"})
    FirestoreJobRepository(client).add_job(make_job())
    FirestoreObservationRepository(client).increment_daily_count("u1", "20261009")
    assert set(client.collections) == {"users", "observations", "weather_jobs", "usage"}


def test_firestore_1日の件数は記録がなければ0で増やすたびに1ずつ増える():
    client = FakeFirestoreClient()
    repo = FirestoreObservationRepository(client)
    assert repo.get_daily_count("u1", "20261009") == 0
    repo.increment_daily_count("u1", "20261009")
    repo.increment_daily_count("u1", "20261009")
    repo.increment_daily_count("u1", "20261010")
    assert repo.get_daily_count("u1", "20261009") == 2
    assert repo.get_daily_count("u1", "20261010") == 1
    assert repo.get_daily_count("u2", "20261009") == 0
    assert client.collections["usage"]["u1_20261009"] == {
        "user_id": "u1",
        "day": "20261009",
        "count": 2,
    }


def test_firestore_天気ジョブの用意がそのまま動く():
    jobs = FirestoreJobRepository(FakeFirestoreClient())
    tasks = CloudTasksScheduler("p", "us-central1", "q", URL, SERVICE_ACCOUNT, FakeTasksClient())
    meta = ObservationMetadata.model_validate(make_metadata())
    record = meta.with_server_fields("u1", RUN_AT, "key.jpg")
    ensure_weather_jobs(record, jobs, tasks)
    ensure_weather_jobs(record, jobs, tasks)
    job = jobs.get_job(f"{meta.observation_id}_label")
    assert job.enqueued is True


def test_firestore_撮影者の観測と送信数の記録と撮影者を消せる():
    client = FakeFirestoreClient()
    repo = FirestoreObservationRepository(client)
    repo.add_user(make_user("h1", "u1"))
    repo.add_user(make_user("h2", "u2"))
    for observation_id, user_id in [("o1", "u1"), ("o2", "u2"), ("o3", "u1")]:
        repo.add_observation({"observation_id": observation_id, "user_id": user_id})
    repo.increment_daily_count("u1", "20261009")
    repo.increment_daily_count("u1", "20261010")
    repo.increment_daily_count("u2", "20261009")
    assert {r["observation_id"] for r in repo.list_observations_by_user("u1")} == {"o1", "o3"}
    repo.delete_observation("o1")
    repo.delete_observation("nope")
    repo.delete_daily_counts("u1")
    repo.delete_daily_counts("nope")
    repo.delete_user("u1")
    repo.delete_user("nope")
    assert repo.get_observation("o1") is None
    assert repo.get_observation("o3") is not None
    assert repo.get_daily_count("u1", "20261009") == 0
    assert repo.get_daily_count("u2", "20261009") == 1
    assert repo.get_user("u1") is None
    assert repo.get_user("u2") is not None


def test_firestore_ジョブを消せてないジョブを消してもエラーにならない():
    repo = FirestoreJobRepository(FakeFirestoreClient())
    repo.add_job(make_job())
    repo.delete_job("j1_forecast")
    repo.delete_job("j1_forecast")
    assert repo.get_job("j1_forecast") is None


def test_firestore_プライバシーゾーンを保存して読み戻せる():
    repo = FirestoreObservationRepository(FakeFirestoreClient())
    zone = PrivacyZone(zone_id="z1", lat=35.0, lon=139.0, radius_m=300, created_at=RUN_AT)
    user = make_user().model_copy(update={"privacy_zones": [zone]})
    repo.add_user(user)
    assert repo.get_user("u1") == user


# Cloud Storage


def test_gcs_接頭辞がついて保存される():
    client = FakeStorageClient()
    GcsBlobStore("bucket", "images/", client).put("a/b.jpg", b"x")
    GcsBlobStore("bucket", "weather/", client).put("a/b.jpg", b"y")
    assert client.buckets["bucket"] == {"images/a/b.jpg": b"x", "weather/a/b.jpg": b"y"}


def test_gcs_取得とないキーはNone():
    store = GcsBlobStore("bucket", "images/", FakeStorageClient())
    store.put("k", b"data")
    assert store.get("k") == b"data"
    assert store.get("missing") is None


def test_gcs_消せてないキーを消してもエラーにならない():
    client = FakeStorageClient()
    images = GcsBlobStore("bucket", "images/", client)
    weather = GcsBlobStore("bucket", "weather/", client)
    images.put("k", b"x")
    weather.put("k", b"y")
    images.delete("k")
    images.delete("k")
    assert images.get("k") is None
    assert weather.get("k") == b"y"


def test_gcs_接頭辞の下だけを消し別の接頭辞や前方一致は残る():
    client = FakeStorageClient()
    images = GcsBlobStore("bucket", "images/", client)
    weather = GcsBlobStore("bucket", "weather/", client)
    for key in ("ab/1.jpg", "ab/2.jpg", "abc/1.jpg", "ab.jpg"):
        images.put(key, b"x")
    weather.put("ab/1.jpg", b"y")
    images.delete_prefix("ab/")
    images.delete_prefix("ab/")
    assert sorted(client.buckets["bucket"]) == [
        "images/ab.jpg",
        "images/abc/1.jpg",
        "weather/ab/1.jpg",
    ]


@pytest.mark.parametrize("prefix", ["", "/", "ab"])
def test_gcs_接頭辞がスラッシュで終わっていなければエラー(prefix):
    store = GcsBlobStore("bucket", "images/", FakeStorageClient())
    with pytest.raises(ValueError):
        store.delete_prefix(prefix)


def test_ローカル_接頭辞の下だけを消し前方一致は残る(tmp_path):
    store = LocalBlobStore(tmp_path)
    for key in ("ab/1.jpg", "ab/2.jpg", "abc/1.jpg", "ab.jpg"):
        store.put(key, b"x")
    store.delete_prefix("ab/")
    store.delete_prefix("ab/")
    assert [store.get(k) for k in ("ab/1.jpg", "ab/2.jpg")] == [None, None]
    assert store.get("abc/1.jpg") == b"x"
    assert store.get("ab.jpg") == b"x"


@pytest.mark.parametrize("prefix", ["", "/", "ab", "../", "../images/"])
def test_ローカル_不正な接頭辞はエラーで何も消さない(tmp_path, prefix):
    store = LocalBlobStore(tmp_path)
    store.put("ab/1.jpg", b"x")
    with pytest.raises(ValueError):
        store.delete_prefix(prefix)
    assert store.get("ab/1.jpg") == b"x"


# Cloud Tasks


def make_scheduler(client: FakeTasksClient) -> CloudTasksScheduler:
    return CloudTasksScheduler("proj", "us-central1", "weather-fetch", URL, SERVICE_ACCOUNT, client)


def test_tasks_作るタスクの中身():
    client = FakeTasksClient()
    make_scheduler(client).schedule("o1_label", RUN_AT)
    queue = "projects/proj/locations/us-central1/queues/weather-fetch"
    name = f"{queue}/tasks/o1_label-{int(RUN_AT.timestamp())}"
    task = client.tasks[name]
    assert task["parent"] == queue
    assert task["schedule_time"] == RUN_AT
    request = task["http_request"]
    assert request["url"] == URL
    assert request["http_method"].name == "POST"
    assert request["headers"] == {"Content-Type": "application/json"}
    assert request["body"] == b'{"job_id": "o1_label"}'
    assert request["oidc_token"] == {"service_account_email": SERVICE_ACCOUNT, "audience": URL}


def test_tasks_同じ予約はAlreadyExistsでも成功とみなす():
    client = FakeTasksClient()
    scheduler = make_scheduler(client)
    scheduler.schedule("o1_label", RUN_AT)
    scheduler.schedule("o1_label", RUN_AT)
    assert client.create_calls == 2
    assert len(client.tasks) == 1


def test_tasks_時刻が違えば別のタスクになる():
    client = FakeTasksClient()
    scheduler = make_scheduler(client)
    scheduler.schedule("o1_label", RUN_AT)
    scheduler.schedule("o1_label", RUN_AT.replace(minute=35))
    assert len(client.tasks) == 2


def test_tasks_AlreadyExists以外の失敗はそのまま投げる():
    class Broken(FakeTasksClient):
        def create_task(self, request):
            raise RuntimeError("通信できない")

    with pytest.raises(RuntimeError):
        make_scheduler(Broken()).schedule("o1_label", RUN_AT)


# OIDC


def make_request(authorization: str | None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "headers": headers})


def good_claims(**overrides) -> dict:
    return {"email": SERVICE_ACCOUNT, "email_verified": True, "aud": URL, **overrides}


def make_auth(verify) -> OidcTaskAuthenticator:
    return OidcTaskAuthenticator(URL, SERVICE_ACCOUNT, verify)


def test_oidc_正しいトークンは通る():
    seen = []

    def verify(token, audience):
        seen.append((token, audience))
        return good_claims()

    make_auth(verify)(make_request("Bearer abc"))
    assert seen == [("abc", URL)]


def raise_invalid(token, audience):
    raise ValueError("Token expired")


@pytest.mark.parametrize(
    ("header", "verify"),
    [
        (None, lambda t, a: good_claims()),
        ("", lambda t, a: good_claims()),
        ("Bearer ", lambda t, a: good_claims()),
        ("Basic abc", lambda t, a: good_claims()),
        ("Bearer abc", raise_invalid),
        ("Bearer abc", lambda t, a: good_claims(email="other@example.com")),
        ("Bearer abc", lambda t, a: {"email_verified": True}),
        ("Bearer abc", lambda t, a: good_claims(email_verified=False)),
        ("Bearer abc", lambda t, a: {"email": SERVICE_ACCOUNT}),
    ],
)
def test_oidc_満たされなければ401(header, verify):
    with pytest.raises(HTTPException) as e:
        make_auth(verify)(make_request(header))
    assert e.value.status_code == 401
    assert e.value.detail == "認証に失敗しました"


def test_oidc_理由はログにだけ出る(caplog):
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as e:
        make_auth(raise_invalid)(make_request("Bearer abc"))
    assert "Token expired" in caplog.text
    assert "Token expired" not in e.value.detail


def test_oidc_JWTの形でないトークンは本物の検証関数でも401(gcp_env):
    # JWT の形でないトークンは、公開鍵を取りに行く前に検証関数が失敗する
    auth = get_task_authenticator_for_oidc(gcp_env)
    with pytest.raises(HTTPException) as e:
        auth(make_request("Bearer not-a-jwt"))
    assert e.value.status_code == 401


def get_task_authenticator_for_oidc(monkeypatch):
    monkeypatch.setenv("SKY_TASK_AUTH", "oidc")
    return get_task_authenticator()


def test_oidc_設定からaudienceとemailを取る(gcp_env):
    auth = get_task_authenticator_for_oidc(gcp_env)
    assert isinstance(auth, OidcTaskAuthenticator)
    assert auth._audience == URL
    assert auth._email == SERVICE_ACCOUNT


def test_oidc_設定が足りなければ起動時にエラー(gcp_env):
    gcp_env.delenv("SKY_TASKS_TARGET_URL")
    with pytest.raises(ConfigError, match="SKY_TASKS_TARGET_URL"):
        get_task_authenticator_for_oidc(gcp_env)


# 設定と build_backend


def test_SKY_BACKENDの既定はlocal(monkeypatch):
    monkeypatch.delenv("SKY_BACKEND", raising=False)
    assert get_backend_name() == "local"


def test_不正なSKY_BACKENDはエラー(monkeypatch):
    monkeypatch.setenv("SKY_BACKEND", "aws")
    with pytest.raises(ConfigError, match="SKY_BACKEND"):
        get_backend_name()
    with pytest.raises(ConfigError):
        build_backend()
    with pytest.raises(ConfigError):
        create_app()


def test_必須の値が足りなければ足りない名前をすべて示す(gcp_env):
    gcp_env.delenv("SKY_GCS_BUCKET")
    gcp_env.setenv("SKY_GCP_PROJECT", "")
    gcp_env.delenv("SKY_TASKS_SERVICE_ACCOUNT")
    with pytest.raises(ConfigError) as e:
        get_gcp_settings()
    message = str(e.value)
    assert "SKY_GCP_PROJECT" in message
    assert "SKY_GCS_BUCKET" in message
    assert "SKY_TASKS_SERVICE_ACCOUNT" in message
    assert "SKY_TASKS_TARGET_URL" not in message


def test_GCPの設定の既定値(gcp_env):
    settings = get_gcp_settings()
    assert settings.tasks_location == "us-central1"
    assert settings.tasks_queue == "weather-fetch"


def test_localならデータ置き場の部品が作られる(tmp_path, monkeypatch):
    monkeypatch.delenv("SKY_BACKEND", raising=False)
    backend = build_backend(tmp_path)
    assert isinstance(backend.repository, LocalObservationRepository)
    assert isinstance(backend.scheduler, LocalTaskScheduler)
    backend.weather_store.put("k", b"w")
    assert (tmp_path / "weather" / "k").read_bytes() == b"w"


def patch_clients(monkeypatch) -> tuple[FakeFirestoreClient, FakeStorageClient, FakeTasksClient]:
    from google.cloud import firestore, storage, tasks_v2

    clients = (FakeFirestoreClient(), FakeStorageClient(), FakeTasksClient())
    monkeypatch.setattr(firestore, "Client", lambda **kwargs: clients[0])
    monkeypatch.setattr(storage, "Client", lambda **kwargs: clients[1])
    monkeypatch.setattr(tasks_v2, "CloudTasksClient", lambda **kwargs: clients[2])
    return clients


def test_gcpなら5つの部品が偽物のクライアントで作られる(gcp_env):
    firestore_client, storage_client, tasks_client = patch_clients(gcp_env)
    backend = build_backend()
    assert isinstance(backend.repository, FirestoreObservationRepository)
    assert isinstance(backend.job_repository, FirestoreJobRepository)
    assert isinstance(backend.blob_store, GcsBlobStore)
    assert isinstance(backend.scheduler, CloudTasksScheduler)
    backend.blob_store.put("k", b"img")
    backend.weather_store.put("k", b"w")
    assert storage_client.buckets["bucket"] == {"images/k": b"img", "weather/k": b"w"}
    backend.scheduler.schedule("o1_label", RUN_AT)
    assert any("queues/weather-fetch/tasks/" in name for name in tasks_client.tasks)
    backend.repository.add_observation({"observation_id": "o1"})
    assert "observations" in firestore_client.collections


def test_gcpでもcreate_appが引数を優先して残りを作る(gcp_env, tmp_path):
    firestore_client, _, _ = patch_clients(gcp_env)
    local = LocalObservationRepository(tmp_path)
    app = create_app(repository=local)
    assert app is not None
    # 渡した repository は使われ、渡さなかった job_repository は Firestore に作られる
    assert local.get_observation("o1") is None
    assert firestore_client.collections == {}


def test_管理コマンドはgcpでrun_due_jobsをエラーで終える(gcp_env, capsys):
    assert main(["run-due-jobs"]) == 1
    assert "gcp" in capsys.readouterr().err


def test_管理コマンドは設定が足りなければエラーで終える(gcp_env, capsys):
    gcp_env.delenv("SKY_GCS_BUCKET")
    assert main(["create-user", "--name", "bob"]) == 1
    assert "SKY_GCS_BUCKET" in capsys.readouterr().err


def test_管理コマンドはgcpでFirestoreに撮影者を作る(gcp_env, capsys):
    firestore_client, _, _ = patch_clients(gcp_env)
    assert main(["create-user", "--name", "bob"]) == 0
    assert len(firestore_client.collections["users"]) == 1
    user_id = next(iter(firestore_client.collections["users"]))
    assert main(["revoke-user", "--user-id", user_id]) == 0
    assert firestore_client.collections["users"][user_id]["revoked_at"] is not None


def test_localのcreate_appは従来どおり動く(tmp_path, monkeypatch):
    monkeypatch.delenv("SKY_BACKEND", raising=False)
    create_app(tmp_path)
    assert LocalBlobStore(tmp_path).get("none") is None


# gcp での小さな修正


def test_gcpでSKY_TASK_AUTHがnoneなら起動時にエラー(gcp_env):
    patch_clients(gcp_env)
    gcp_env.setenv("SKY_TASK_AUTH", "none")
    with pytest.raises(ConfigError, match="SKY_TASK_AUTH"):
        get_task_authenticator()
    with pytest.raises(ConfigError, match="SKY_TASK_AUTH"):
        create_app()


def test_localならSKY_TASK_AUTHがnoneでも起動できる(tmp_path, monkeypatch):
    monkeypatch.delenv("SKY_BACKEND", raising=False)
    monkeypatch.setenv("SKY_TASK_AUTH", "none")
    create_app(tmp_path)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_gcpでは仕様の画面を公開しない(gcp_env, path):
    patch_clients(gcp_env)
    gcp_env.setenv("SKY_TASK_AUTH", "oidc")
    assert TestClient(create_app()).get(path).status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_localでは仕様の画面を公開する(tmp_path, monkeypatch, path):
    monkeypatch.delenv("SKY_BACKEND", raising=False)
    assert TestClient(create_app(tmp_path)).get(path).status_code == 200
