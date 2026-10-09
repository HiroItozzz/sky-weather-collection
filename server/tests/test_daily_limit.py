import json
from datetime import UTC, datetime

import pytest
from conftest import IMAGE, Api, make_metadata
from fastapi.testclient import TestClient

from sky_server.jobs import LocalJobRepository, LocalTaskScheduler
from sky_server.main import create_app
from sky_server.storage import LocalBlobStore, LocalObservationRepository

DAY = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
LIMIT = 3


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(DAY)


@pytest.fixture
def limited_api(data_dir, monkeypatch, clock) -> Api:
    monkeypatch.setenv("SKY_DAILY_UPLOAD_LIMIT", str(LIMIT))
    app = create_app(data_dir, clock=clock)
    return Api(TestClient(app), LocalObservationRepository(data_dir))


@pytest.fixture
def user(limited_api) -> tuple[str, str]:
    return limited_api.new_user()


@pytest.fixture
def token(user) -> str:
    return user[1]


def new_id(n: int) -> str:
    return f"123e4567-e89b-42d3-a456-{n:012d}"


def put_new(api: Api, token: str, n: int):
    image = IMAGE + str(n).encode()
    return api.put(token, make_metadata(image, new_id(n)), image, new_id(n))


def test_ちょうど上限まで201で次は429(limited_api, token):
    for n in range(LIMIT):
        assert put_new(limited_api, token, n).status_code == 201
    res = put_new(limited_api, token, LIMIT)
    assert res.status_code == 429
    assert limited_api.repository.get_observation(new_id(LIMIT)) is None


def test_429にはRetry_Afterで次のUTC0時までの秒数が入る(limited_api, token):
    for n in range(LIMIT):
        put_new(limited_api, token, n)
    res = put_new(limited_api, token, LIMIT)
    # 12:00 から翌日の 0:00 まで 12 時間
    assert res.headers["Retry-After"] == str(12 * 60 * 60)


def test_再送は上限を超えていても200(limited_api, token, user):
    for n in range(LIMIT):
        put_new(limited_api, token, n)
    assert put_new(limited_api, token, LIMIT).status_code == 429
    assert put_new(limited_api, token, 0).status_code == 200
    assert limited_api.repository.get_daily_count(user[0], "20261009") == LIMIT


def test_件数は新規の作成に成功したときだけ増える(limited_api, token, user):
    user_id = user[0]
    repo = limited_api.repository
    assert put_new(limited_api, token, 0).status_code == 201
    assert repo.get_daily_count(user_id, "20261009") == 1
    # 再送（200）
    assert put_new(limited_api, token, 0).status_code == 200
    # 同じ ID で画像が違う（409）
    other = IMAGE + b"other"
    meta = make_metadata(other, new_id(0))
    assert limited_api.put(token, meta, other, new_id(0)).status_code == 409
    # ハッシュの不一致（422）
    meta = make_metadata(IMAGE, new_id(1))
    meta["image_sha256"] = "0" * 64
    assert limited_api.put(token, meta, IMAGE, new_id(1)).status_code == 422
    assert repo.get_daily_count(user_id, "20261009") == 1


def test_上限は撮影者ごとに数える(limited_api, token):
    for n in range(LIMIT):
        put_new(limited_api, token, n)
    _, other_token = limited_api.new_user("other")
    assert put_new(limited_api, other_token, 10).status_code == 201


def test_日付が変わると送れるように戻る(limited_api, token, clock):
    for n in range(LIMIT):
        put_new(limited_api, token, n)
    assert put_new(limited_api, token, LIMIT).status_code == 429
    clock.now = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    assert put_new(limited_api, token, LIMIT).status_code == 201


def test_上限の既定は100(monkeypatch):
    from sky_server.config import get_daily_upload_limit

    monkeypatch.delenv("SKY_DAILY_UPLOAD_LIMIT", raising=False)
    assert get_daily_upload_limit() == 100


def test_ローカルの件数は記録がなければ0でusageに保存される(data_dir):
    repo = LocalObservationRepository(data_dir)
    assert repo.get_daily_count("u1", "20261009") == 0
    repo.increment_daily_count("u1", "20261009")
    repo.increment_daily_count("u1", "20261009")
    assert repo.get_daily_count("u1", "20261009") == 2
    assert repo.get_daily_count("u1", "20261010") == 0
    path = data_dir / "usage" / "u1_20261009.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "user_id": "u1",
        "day": "20261009",
        "count": 2,
    }


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", ""])
def test_上限が1以上の整数でなければConfigError(monkeypatch, value):
    from sky_server.config import ConfigError, get_daily_upload_limit

    monkeypatch.setenv("SKY_DAILY_UPLOAD_LIMIT", value)
    with pytest.raises(ConfigError, match="SKY_DAILY_UPLOAD_LIMIT"):
        get_daily_upload_limit()


def test_上限が1なら使える(monkeypatch):
    from sky_server.config import get_daily_upload_limit

    monkeypatch.setenv("SKY_DAILY_UPLOAD_LIMIT", "1")
    assert get_daily_upload_limit() == 1


def test_上限が不正ならアプリの起動時にConfigError(data_dir, monkeypatch):
    from sky_server.config import ConfigError

    monkeypatch.setenv("SKY_DAILY_UPLOAD_LIMIT", "0")
    with pytest.raises(ConfigError):
        create_app(data_dir)


def test_件数の増加が例外を投げても201でジョブが予約される(data_dir, monkeypatch, clock, caplog):
    class BrokenCountRepository(LocalObservationRepository):
        def increment_daily_count(self, user_id, day):
            raise RuntimeError("firestore down")

    monkeypatch.setenv("SKY_DAILY_UPLOAD_LIMIT", str(LIMIT))
    repository = BrokenCountRepository(data_dir)
    api = Api(TestClient(create_app(data_dir, repository=repository, clock=clock)), repository)
    _, token = api.new_user()
    assert put_new(api, token, 0).status_code == 201
    assert repository.get_observation(new_id(0)) is not None
    assert LocalJobRepository(data_dir).get_job(f"{new_id(0)}_forecast") is not None
    assert "1日の件数の増加に失敗しました" in caplog.text


def test_天気ジョブの予約に失敗したら件数は増やさず503(data_dir, monkeypatch, clock):
    class BrokenScheduler(LocalTaskScheduler):
        def schedule(self, job_id, run_at):
            raise RuntimeError("tasks down")

    monkeypatch.setenv("SKY_DAILY_UPLOAD_LIMIT", str(LIMIT))
    repository = LocalObservationRepository(data_dir)
    app = create_app(
        data_dir,
        repository=repository,
        job_repository=LocalJobRepository(data_dir),
        blob_store=LocalBlobStore(data_dir),
        scheduler=BrokenScheduler(data_dir),
        runner=object(),
        clock=clock,
    )
    api = Api(TestClient(app), repository)
    user_id, token = api.new_user()
    assert put_new(api, token, 0).status_code == 503
    assert repository.get_daily_count(user_id, "20261009") == 0
