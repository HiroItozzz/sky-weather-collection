"""プライバシーゾーン、公開への同意、撮影者の削除の管理コマンド（ローカル版と GCP 版）。"""

import uuid
from datetime import UTC, datetime

import pytest
from gcp_fakes import FakeFirestoreClient, FakeStorageClient, FakeTasksClient
from test_gcp import GCP_ENV

from sky_server import admin
from sky_server.admin import create_user, main
from sky_server.backends import build_backend
from sky_server.jobs import ProviderState, WeatherJob
from sky_server.weather.common import raw_key

NOW = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


@pytest.fixture(params=["local", "gcp"])
def backend(request, tmp_path, monkeypatch):
    """コマンドが使うのと同じ設定で作った保存先。GCP 版は偽物のクライアントで動く。"""
    if request.param == "local":
        monkeypatch.delenv("SKY_BACKEND", raising=False)
        monkeypatch.setenv("SKY_DATA_DIR", str(tmp_path))
    else:
        from google.cloud import firestore, storage, tasks_v2

        for name in ("SKY_TASKS_LOCATION", "SKY_TASKS_QUEUE", "SKY_TASK_AUTH"):
            monkeypatch.delenv(name, raising=False)
        for name, value in GCP_ENV.items():
            monkeypatch.setenv(name, value)
        clients = (FakeFirestoreClient(), FakeStorageClient(), FakeTasksClient())
        monkeypatch.setattr(firestore, "Client", lambda **kwargs: clients[0])
        monkeypatch.setattr(storage, "Client", lambda **kwargs: clients[1])
        monkeypatch.setattr(tasks_v2, "CloudTasksClient", lambda **kwargs: clients[2])
    return build_backend()


def new_user(backend, name="alice") -> str:
    user, _ = create_user(backend.repository, name)
    return user.user_id


# プライバシーゾーン


def add_zone(user_id, *extra) -> list[str]:
    return ["add-privacy-zone", "--user-id", user_id, *extra]


def test_ゾーンを追加して一覧と削除ができる(backend, capsys):
    user_id = new_user(backend)
    assert main(add_zone(user_id, "--lat", "35.68", "--lon", "139.76", "--radius-m", "300")) == 0
    first = capsys.readouterr().out.strip().removeprefix("zone_id: ")
    assert str(uuid.UUID(first)) == first
    args = ["--lat", "-33.5", "--lon", "-70.25", "--radius-m", "50000", "--label", "実家 の近く"]
    assert main(add_zone(user_id, *args)) == 0
    second = capsys.readouterr().out.strip().removeprefix("zone_id: ")

    assert main(["list-privacy-zones", "--user-id", user_id]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{first} 35.68 139.76 300.0 -",
        f"{second} -33.5 -70.25 50000.0 実家 の近く",
    ]
    zones = backend.repository.get_user(user_id).privacy_zones
    assert [z.zone_id for z in zones] == [first, second]
    assert zones[1].label == "実家 の近く"

    assert main(["delete-privacy-zone", "--user-id", user_id, "--zone-id", first]) == 0
    assert [z.zone_id for z in backend.repository.get_user(user_id).privacy_zones] == [second]


def test_ゾーンがなければ一覧は何も表示しない(backend, capsys):
    user_id = new_user(backend)
    assert main(["list-privacy-zones", "--user-id", user_id]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("lat", "lon", "radius"),
    [
        ("90.1", "0", "100"),
        ("-90.1", "0", "100"),
        ("0", "180.1", "100"),
        ("0", "-180.1", "100"),
        ("0", "0", "0"),
        ("0", "0", "-1"),
        ("0", "0", "50000.1"),
        ("nan", "0", "100"),
        ("0", "0", "inf"),
        ("abc", "0", "100"),
    ],
)
def test_範囲外の値でゾーンを追加するとエラー(backend, capsys, lat, lon, radius):
    user_id = new_user(backend)
    with pytest.raises(SystemExit) as e:
        main(add_zone(user_id, "--lat", lat, "--lon", lon, "--radius-m", radius))
    assert e.value.code == 2
    assert backend.repository.get_user(user_id).privacy_zones == []
    assert capsys.readouterr().err != ""


def test_ちょうど境目の値は追加できる(backend):
    user_id = new_user(backend)
    for lat, lon, radius in [("90", "180", "50000"), ("-90", "-180", "0.001")]:
        assert main(add_zone(user_id, "--lat", lat, "--lon", lon, "--radius-m", radius)) == 0
    assert len(backend.repository.get_user(user_id).privacy_zones) == 2


def test_見つからない撮影者とゾーンは終了コード1(backend, capsys):
    user_id = new_user(backend)
    unknown = str(uuid.uuid4())
    zone_args = ["--lat", "35", "--lon", "139", "--radius-m", "100"]
    assert main(add_zone(unknown, *zone_args)) == 1
    assert main(add_zone("nope", *zone_args)) == 1
    assert main(["list-privacy-zones", "--user-id", unknown]) == 1
    assert main(["delete-privacy-zone", "--user-id", unknown, "--zone-id", "z"]) == 1
    assert "見つかりません" in capsys.readouterr().err
    assert main(["delete-privacy-zone", "--user-id", user_id, "--zone-id", "nope"]) == 1
    assert "プライバシーゾーンが見つかりません: nope" in capsys.readouterr().err


def test_ゾーンの削除は他のゾーンを残す(backend, capsys):
    user_id = new_user(backend)
    ids = []
    for lat in ("1", "2", "3"):
        main(add_zone(user_id, "--lat", lat, "--lon", "0", "--radius-m", "10"))
        ids.append(capsys.readouterr().out.strip().removeprefix("zone_id: "))
    main(["delete-privacy-zone", "--user-id", user_id, "--zone-id", ids[1]])
    zones = backend.repository.get_user(user_id).privacy_zones
    assert [z.zone_id for z in zones] == [ids[0], ids[2]]


# 公開への同意


def test_同意を変えられる(backend):
    user_id = new_user(backend)
    assert main(["set-consent", "--user-id", user_id, "--public", "yes"]) == 0
    assert backend.repository.get_user(user_id).consent_public is True
    assert main(["set-consent", "--user-id", user_id, "--public", "no"]) == 0
    assert backend.repository.get_user(user_id).consent_public is False


def test_同意の値がyesとno以外ならエラー(backend):
    user_id = new_user(backend)
    with pytest.raises(SystemExit) as e:
        main(["set-consent", "--user-id", user_id, "--public", "maybe"])
    assert e.value.code == 2


def test_見つからない撮影者の同意の変更は終了コード1(backend):
    assert main(["set-consent", "--user-id", str(uuid.uuid4()), "--public", "yes"]) == 1


def test_同意を変えてもゾーンと無効化は残る(backend, capsys):
    user_id = new_user(backend)
    main(add_zone(user_id, "--lat", "1", "--lon", "2", "--radius-m", "3"))
    main(["revoke-user", "--user-id", user_id])
    main(["set-consent", "--user-id", user_id, "--public", "yes"])
    user = backend.repository.get_user(user_id)
    assert len(user.privacy_zones) == 1
    assert user.revoked_at is not None


# 撮影者の削除


def seed_observation(backend, user_id, observation_id, with_jobs=True) -> dict[str, str]:
    """観測1件と、画像・天気の生レスポンス・ジョブを作る。消えるべきものの保存先を返す。"""
    image_key = f"{observation_id[:2]}/{observation_id}.jpg"
    backend.blob_store.put(image_key, b"jpeg")
    backend.repository.add_observation(
        {"observation_id": observation_id, "user_id": user_id, "image_key": image_key}
    )
    keys = {
        raw_key(p, observation_id, ph)
        for p in ("open_meteo", "amedas")
        for ph in ("forecast", "label")
    }
    for key in keys:
        backend.weather_store.put(key, b"raw")
    if with_jobs:
        for phase in ("forecast", "label"):
            job = WeatherJob(
                job_id=f"{observation_id}_{phase}",
                observation_id=observation_id,
                phase=phase,
                created_at=NOW,
                run_at=NOW,
                next_attempt_at=NOW,
                providers={
                    "open_meteo": ProviderState(
                        status="done", blob_key=raw_key("open_meteo", observation_id, phase)
                    )
                },
            )
            backend.job_repository.add_job(job)
    return {"image": image_key, "raw": sorted(keys)}


def remains(backend, observation_id) -> list[str]:
    """観測に関わるデータのうち、残っているものの名前。"""
    found = []
    if backend.repository.get_observation(observation_id):
        found.append("observation")
    for phase in ("forecast", "label"):
        if backend.job_repository.get_job(f"{observation_id}_{phase}"):
            found.append(f"job_{phase}")
        for provider in ("open_meteo", "amedas"):
            if backend.weather_store.get(raw_key(provider, observation_id, phase)):
                found.append(f"raw_{provider}_{phase}")
    image_key = f"{observation_id[:2]}/{observation_id}.jpg"
    if backend.blob_store.get(image_key):
        found.append("image")
    return found


OBS_A1 = "aa000000-0000-4000-8000-000000000001"
OBS_A2 = "aa000000-0000-4000-8000-000000000002"
OBS_B1 = "bb000000-0000-4000-8000-000000000001"


def seed_two_users(backend):
    alice = new_user(backend, "alice")
    bob = new_user(backend, "bob")
    seed_observation(backend, alice, OBS_A1)
    # 2つ目はジョブがなく、生レスポンスだけが残っている
    seed_observation(backend, alice, OBS_A2, with_jobs=False)
    seed_observation(backend, bob, OBS_B1)
    for user_id in (alice, bob):
        backend.repository.increment_daily_count(user_id, "20261009")
        backend.repository.increment_daily_count(user_id, "20261010")
    return alice, bob


def test_yesがなければ件数だけ表示して何も消さない(backend, capsys):
    alice, bob = seed_two_users(backend)
    assert main(["delete-user", "--user-id", alice]) == 1
    err = capsys.readouterr().err
    assert "観測 2 件" in err
    assert "--yes" in err
    assert backend.repository.get_user(alice) is not None
    assert len(remains(backend, OBS_A1)) == 8
    assert len(remains(backend, OBS_A2)) == 6
    assert backend.repository.get_daily_count(alice, "20261009") == 1


def test_yesで撮影者のデータがすべて消え他の撮影者は残る(backend, capsys):
    alice, bob = seed_two_users(backend)
    before_bob = remains(backend, OBS_B1)
    assert main(["delete-user", "--user-id", alice, "--yes"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == f"撮影者 {alice} を消しました（消した観測: 2 件）"

    assert backend.repository.get_user(alice) is None
    assert remains(backend, OBS_A1) == []
    assert remains(backend, OBS_A2) == []
    assert backend.repository.get_daily_count(alice, "20261009") == 0
    assert backend.repository.get_daily_count(alice, "20261010") == 0

    assert backend.repository.get_user(bob) is not None
    assert remains(backend, OBS_B1) == before_bob
    assert len(before_bob) == 8
    assert backend.repository.get_daily_count(bob, "20261009") == 1
    assert backend.repository.get_daily_count(bob, "20261010") == 1


def test_ジョブのblob_keyが標準の場所でなくても消える(backend):
    alice = new_user(backend)
    seed_observation(backend, alice, OBS_A1)
    job = backend.job_repository.get_job(f"{OBS_A1}_label")
    job.providers["open_meteo"].blob_key = "raw/other/place.json.gz"
    backend.job_repository.update_job(job)
    backend.weather_store.put("raw/other/place.json.gz", b"raw")
    assert main(["delete-user", "--user-id", alice, "--yes"]) == 0
    assert backend.weather_store.get("raw/other/place.json.gz") is None


def test_観測のない撮影者も消せる(backend, capsys):
    alice = new_user(backend)
    assert main(["delete-user", "--user-id", alice, "--yes"]) == 0
    assert "消した観測: 0 件" in capsys.readouterr().out
    assert backend.repository.get_user(alice) is None


def test_見つからない撮影者の削除は終了コード1(backend, capsys):
    new_user(backend)
    assert main(["delete-user", "--user-id", str(uuid.uuid4()), "--yes"]) == 1
    assert main(["delete-user", "--user-id", "nope", "--yes"]) == 1
    assert "見つかりません" in capsys.readouterr().err


def test_途中で失敗してももう一度実行すれば残りを消せる(backend, monkeypatch, capsys):
    alice, bob = seed_two_users(backend)
    store_class = type(backend.weather_store)
    original = store_class.delete
    calls = []

    def flaky(self, key):
        calls.append(key)
        if len(calls) == 10:
            raise ConnectionError("通信できません")
        original(self, key)

    monkeypatch.setattr(store_class, "delete", flaky)
    assert main(["delete-user", "--user-id", alice, "--yes"]) == 1
    assert "ConnectionError" in capsys.readouterr().err
    # 撮影者は最後に消すので、失敗したあとも残っている
    assert backend.repository.get_user(alice) is not None
    assert len(remains(backend, OBS_A1) + remains(backend, OBS_A2)) > 0

    monkeypatch.setattr(store_class, "delete", original)
    assert main(["delete-user", "--user-id", alice, "--yes"]) == 0
    assert backend.repository.get_user(alice) is None
    assert remains(backend, OBS_A1) == remains(backend, OBS_A2) == []
    assert backend.repository.get_daily_count(alice, "20261009") == 0
    assert len(remains(backend, OBS_B1)) == 8


def test_消す順番は観測の記録と撮影者が最後(backend, monkeypatch):
    alice, _ = seed_two_users(backend)
    order = []
    for obj, name in [
        (type(backend.repository), "delete_observation"),
        (type(backend.repository), "delete_daily_counts"),
        (type(backend.repository), "delete_user"),
        (type(backend.job_repository), "delete_job"),
    ]:
        monkeypatch.setattr(obj, name, _recording(getattr(obj, name), name, order))
    admin.delete_user_data(
        alice, backend.repository, backend.blob_store, backend.weather_store, backend.job_repository
    )
    assert order[-2:] == ["delete_daily_counts", "delete_user"]
    # 観測ごとに、ジョブを消してから観測の記録を消す
    assert order[:6] == ["delete_job", "delete_job", "delete_observation"] * 2


def _recording(original, name, order):
    def wrapper(self, *args):
        order.append(name)
        return original(self, *args)

    return wrapper
