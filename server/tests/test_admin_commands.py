"""プライバシーゾーン、公開への同意、撮影者の削除の管理コマンド（ローカル版と GCP 版）。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import RAIN_ANSWER, WEATHER_AT_CAPTURE
from gcp_fakes import FakeFirestoreClient, FakeStorageClient, FakeTasksClient
from test_gcp import GCP_ENV

from sky_server import admin
from sky_server.admin import create_user, main
from sky_server.backends import build_backend
from sky_server.jobs import ProviderState, Summarizer, WeatherJob
from sky_server.weather.common import raw_key, save_envelope

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
        f"{first}\t35.68\t139.76\t300.0\t-",
        f"{second}\t-33.5\t-70.25\t50000.0\t実家 の近く",
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
    image_key = f"{observation_id}/{'0' * 64}.jpg"
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
    if backend.blob_store.get(f"{observation_id}/{'0' * 64}.jpg"):
        found.append("image")
    if backend.blob_store.get(f"{observation_id}/{'1' * 64}.jpg"):
        found.append("image_other")
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


def delete_user(alice, at=NOW) -> int:
    return main(["delete-user", "--user-id", alice, "--yes"], clock=lambda: at)


def test_1回目は観測のデータを消して撮影者は残し無効にする(backend, capsys):
    alice, bob = seed_two_users(backend)
    assert delete_user(alice) == 0
    out = capsys.readouterr().out
    assert "もう一度実行してください" in out
    assert "あと 10 分" in out

    user = backend.repository.get_user(alice)
    assert user.revoked_at == NOW
    assert user.deletion_started_at == NOW
    assert sorted(user.deletion_observation_ids) == [OBS_A1, OBS_A2]
    assert remains(backend, OBS_A1) == []
    assert remains(backend, OBS_A2) == []
    assert backend.repository.get_daily_count(alice, "20261009") == 1
    assert backend.repository.get_user(bob).revoked_at is None


def test_10分たっていなければ撮影者を残して待ち時間を表示する(backend, capsys):
    alice, _ = seed_two_users(backend)
    delete_user(alice)
    capsys.readouterr()
    assert delete_user(alice, NOW + timedelta(minutes=9, seconds=30)) == 0
    assert "あと 1 分" in capsys.readouterr().out
    user = backend.repository.get_user(alice)
    assert user is not None
    assert user.deletion_started_at == NOW
    assert backend.repository.get_daily_count(alice, "20261009") == 1


def test_10分ちょうどで撮影者とusageが消え他の撮影者は残る(backend, capsys):
    alice, bob = seed_two_users(backend)
    before_bob = remains(backend, OBS_B1)
    delete_user(alice)
    capsys.readouterr()
    assert delete_user(alice, NOW + timedelta(minutes=10)) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == f"撮影者 {alice} を消しました（消した観測: 2 件）"

    assert backend.repository.get_user(alice) is None
    assert backend.repository.get_daily_count(alice, "20261009") == 0
    assert backend.repository.get_daily_count(alice, "20261010") == 0
    assert backend.repository.get_user(bob) is not None
    assert remains(backend, OBS_B1) == before_bob
    assert len(before_bob) == 8
    assert backend.repository.get_daily_count(bob, "20261009") == 1
    assert backend.repository.get_daily_count(bob, "20261010") == 1


def test_1回目のあとに書き戻されたジョブと生レスポンスも2回目で消える(backend):
    alice, _ = seed_two_users(backend)
    delete_user(alice)
    # 実行中のジョブが、観測が消えたあとに書き戻した
    seed_observation(backend, alice, OBS_A1)
    backend.repository.delete_observation(OBS_A1)
    assert len(remains(backend, OBS_A1)) == 7

    assert delete_user(alice, NOW + timedelta(minutes=10)) == 0
    assert remains(backend, OBS_A1) == []
    assert backend.repository.get_user(alice) is None


def test_別のIDの前方一致では消えない(backend):
    alice = new_user(backend)
    other = OBS_A1[:-1] + "9"
    seed_observation(backend, alice, OBS_A1)
    bob = new_user(backend, "bob")
    seed_observation(backend, bob, OBS_A1 + "0")
    seed_observation(backend, bob, other)
    # 同じ ID で別の画像が先に保存されていた（409 になった）
    backend.blob_store.put(f"{OBS_A1}/{'1' * 64}.jpg", b"jpeg2")
    delete_user(alice)
    assert remains(backend, OBS_A1) == []
    assert len(remains(backend, OBS_A1 + "0")) == 8
    assert len(remains(backend, other)) == 8


def test_ジョブのblob_keyが標準の場所でなくても消える(backend):
    alice = new_user(backend)
    seed_observation(backend, alice, OBS_A1)
    job = backend.job_repository.get_job(f"{OBS_A1}_label")
    job.providers["open_meteo"].blob_key = "raw/other/place.json.gz"
    backend.job_repository.update_job(job)
    backend.weather_store.put("raw/other/place.json.gz", b"raw")
    assert delete_user(alice) == 0
    assert backend.weather_store.get("raw/other/place.json.gz") is None


def test_観測のない撮影者も10分後に消せる(backend, capsys):
    alice = new_user(backend)
    assert delete_user(alice) == 0
    assert backend.repository.get_user(alice) is not None
    assert delete_user(alice, NOW + timedelta(minutes=10)) == 0
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
    original = store_class.delete_prefix
    calls = []

    def flaky(self, prefix):
        calls.append(prefix)
        if len(calls) == 3:
            raise ConnectionError("通信できません")
        original(self, prefix)

    monkeypatch.setattr(store_class, "delete_prefix", flaky)
    assert delete_user(alice) == 1
    assert "ConnectionError" in capsys.readouterr().err
    # 撮影者は最後に消すので、失敗したあとも残っている
    user = backend.repository.get_user(alice)
    assert user is not None
    assert sorted(user.deletion_observation_ids) == [OBS_A1, OBS_A2]
    assert len(remains(backend, OBS_A1) + remains(backend, OBS_A2)) > 0

    monkeypatch.setattr(store_class, "delete_prefix", original)
    assert delete_user(alice, NOW + timedelta(minutes=10)) == 0
    assert backend.repository.get_user(alice) is None
    assert remains(backend, OBS_A1) == remains(backend, OBS_A2) == []
    assert backend.repository.get_daily_count(alice, "20261009") == 0
    assert len(remains(backend, OBS_B1)) == 8


def test_観測の記録が先に消えていても記録したIDで続きを消せる(backend):
    alice, _ = seed_two_users(backend)
    delete_user(alice)
    # 2回目の前に、観測の記録だけが消えて生レスポンスが残った状態
    backend.weather_store.put(raw_key("amedas", OBS_A1, "label"), b"raw")
    assert delete_user(alice, NOW + timedelta(minutes=11)) == 0
    assert remains(backend, OBS_A1) == []


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
        alice,
        backend.repository,
        backend.blob_store,
        backend.weather_store,
        backend.job_repository,
        lambda: NOW,
    )
    order.clear()
    admin.delete_user_data(
        alice,
        backend.repository,
        backend.blob_store,
        backend.weather_store,
        backend.job_repository,
        lambda: NOW + timedelta(minutes=10),
    )
    assert order[-2:] == ["delete_daily_counts", "delete_user"]
    # 観測ごとに、ジョブを消してから観測の記録を消す
    assert order[:6] == ["delete_job", "delete_job", "delete_observation"] * 2


def _recording(original, name, order):
    def wrapper(self, *args):
        order.append(name)
        return original(self, *args)

    return wrapper


# 要約の作り直し

REBUILD_A = "cc000000-0000-4000-8000-000000000001"
REBUILD_B = "cc000000-0000-4000-8000-000000000002"


def seed_for_rebuild(backend, observation_id: str, captured_at: str, **statuses: str) -> None:
    """観測と、`statuses`（phase -> ジョブの状態）のジョブを作る。封筒はジョブごとに保存する。"""
    user_id = new_user(backend, f"user-{observation_id[-1]}")
    backend.repository.add_observation(
        {
            "observation_id": observation_id,
            "user_id": user_id,
            "captured_at": captured_at,
            "received_at": "2026-10-09T03:05:00+00:00",
        }
    )
    for phase, status in statuses.items():
        providers = {}
        for provider in ("open_meteo", "amedas") if phase == "label" else ("open_meteo",):
            key = raw_key(provider, observation_id, phase)
            save_envelope(backend.weather_store, key, {"provider": provider, "phase": phase})
            providers[provider] = ProviderState(status="done", blob_key=key)
        backend.job_repository.add_job(
            WeatherJob(
                job_id=f"{observation_id}_{phase}",
                observation_id=observation_id,
                phase=phase,
                status=status,
                created_at=NOW,
                run_at=NOW,
                next_attempt_at=None if status != "pending" else NOW,
                providers=providers,
            )
        )


def test_作り直すと撮影時刻のUTCが補われる(backend, fake_summary, capsys):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T12:00:00.123+09:00")
    seed_for_rebuild(backend, REBUILD_B, "2026-10-09T03:00:00Z")
    assert main(["rebuild-summaries"]) == 0
    assert backend.repository.get_observation(REBUILD_A)["captured_at_utc"] == (
        "2026-10-09T03:00:00.123Z"
    )
    assert backend.repository.get_observation(REBUILD_B)["captured_at_utc"] == (
        "2026-10-09T03:00:00.000Z"
    )
    assert "処理した観測: 2 件、失敗: 0 件" in capsys.readouterr().out


def test_作り直しはあるcaptured_at_utcを書き換えない(backend, fake_summary):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T12:00:00+09:00")
    backend.repository.update_observation_fields(
        REBUILD_A, {"captured_at_utc": "2026-10-09T03:00:00.999Z"}
    )
    assert main(["rebuild-summaries"]) == 0
    assert backend.repository.get_observation(REBUILD_A)["captured_at_utc"] == (
        "2026-10-09T03:00:00.999Z"
    )


def test_作り直すと完了したジョブの要約が計算し直される(backend, fake_summary):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", forecast="done", label="failed")
    assert main(["rebuild-summaries"]) == 0
    forecast = backend.job_repository.get_job(f"{REBUILD_A}_forecast")
    label = backend.job_repository.get_job(f"{REBUILD_A}_label")
    assert forecast.summary == {"weather_at_capture": WEATHER_AT_CAPTURE}
    assert label.summary == {"answer": RAIN_ANSWER}
    record = backend.repository.get_observation(REBUILD_A)
    assert record["weather_at_capture"] == WEATHER_AT_CAPTURE
    assert record["answer"] == RAIN_ANSWER
    # 保存した封筒から計算している
    assert ("weather_at_capture", {"provider": "open_meteo", "phase": "forecast"}) == (
        fake_summary.calls[0][0],
        fake_summary.calls[0][1],
    )


def test_作り直しは同じ結果になり何度実行しても変わらない(backend, fake_summary):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", forecast="done", label="done")
    assert main(["rebuild-summaries"]) == 0
    first = backend.repository.get_observation(REBUILD_A)
    fake_summary.weather = {**WEATHER_AT_CAPTURE, "temperature_c": 25.0}
    assert main(["rebuild-summaries"]) == 0
    # 計算の結果が変われば上書きされる
    second = backend.repository.get_observation(REBUILD_A)
    assert second["weather_at_capture"]["temperature_c"] == 25.0
    assert second["answer"] == first["answer"]
    assert backend.job_repository.get_job(f"{REBUILD_A}_forecast").summary == {
        "weather_at_capture": second["weather_at_capture"]
    }


def test_作り直しは完了していないジョブとskippedは計算しない(backend, fake_summary):
    seed_for_rebuild(
        backend, REBUILD_A, "2026-10-09T03:00:00Z", forecast="pending", label="skipped"
    )
    assert main(["rebuild-summaries"]) == 0
    assert fake_summary.calls == []
    record = backend.repository.get_observation(REBUILD_A)
    assert "weather_at_capture" not in record
    assert "answer" not in record
    assert backend.job_repository.get_job(f"{REBUILD_A}_forecast").summary is None


def test_ジョブがない観測も作り直しで止まらない(backend, fake_summary, capsys):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z")
    assert main(["rebuild-summaries"]) == 0
    assert "処理した観測: 1 件、失敗: 0 件" in capsys.readouterr().out


def test_観測がなければ作り直しは0件で終わる(backend, fake_summary, capsys):
    assert main(["rebuild-summaries"]) == 0
    assert "処理した観測: 0 件、失敗: 0 件" in capsys.readouterr().out


def test_計算に失敗した観測があっても続けて数えて終了コード1(backend, fake_summary, capsys):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", label="done")
    seed_for_rebuild(backend, REBUILD_B, "2026-10-09T04:00:00Z", forecast="done", label="done")
    original = fake_summary.answer

    def answer(open_meteo, amedas, captured_at):
        if captured_at.hour == 3:
            raise RuntimeError("計算できない")
        return original(open_meteo, amedas, captured_at)

    fake_summary.answer = answer
    assert main(["rebuild-summaries"]) == 1
    captured = capsys.readouterr()
    assert "処理した観測: 2 件、失敗: 1 件" in captured.out
    assert REBUILD_A in captured.err
    # 失敗した観測には要約が書かれない。もう一方は最後まで作り直されている
    assert backend.job_repository.get_job(f"{REBUILD_A}_label").summary is None
    assert backend.job_repository.get_job(f"{REBUILD_A}_label").status == "done"
    assert backend.job_repository.get_job(f"{REBUILD_B}_label").summary == {"answer": RAIN_ANSWER}
    assert backend.repository.get_observation(REBUILD_B)["weather_at_capture"] == WEATHER_AT_CAPTURE


def test_撮影時刻が読めない観測は失敗に数えて続ける(backend, fake_summary, capsys):
    seed_for_rebuild(backend, REBUILD_A, "壊れた値", forecast="done")
    seed_for_rebuild(backend, REBUILD_B, "2026-10-09T03:00:00Z", forecast="done")
    assert main(["rebuild-summaries"]) == 1
    assert "処理した観測: 2 件、失敗: 1 件" in capsys.readouterr().out
    assert "weather_at_capture" in backend.repository.get_observation(REBUILD_B)


def rebuild(backend) -> tuple[int, int]:
    return admin.rebuild_summaries(
        backend.repository, backend.job_repository, Summarizer(backend.weather_store)
    )


def seed_with_old_summary(backend, fake_summary) -> tuple[dict, dict]:
    """前の要約が入っている完了済みの観測を作り、(前の天気, 前の答え) を返す。"""
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", forecast="done", label="done")
    old_weather = {**WEATHER_AT_CAPTURE, "category": "clear"}
    old_answer = {"result": "no_rain", "source": "open_meteo"}
    backend.repository.update_observation_fields(
        REBUILD_A, {"weather_at_capture": old_weather, "answer": old_answer}
    )
    for phase, summary in (
        ("forecast", {"weather_at_capture": old_weather}),
        ("label", {"answer": old_answer}),
    ):
        job = backend.job_repository.get_job(f"{REBUILD_A}_{phase}")
        job.summary = summary
        backend.job_repository.update_job(job)
    return old_weather, old_answer


def test_計算に失敗してもジョブと観測の前の要約が残る(backend, fake_summary, capsys):
    old_weather, old_answer = seed_with_old_summary(backend, fake_summary)
    fake_summary.error = RuntimeError("計算できない")
    assert main(["rebuild-summaries"]) == 1
    assert "処理した観測: 1 件、失敗: 1 件" in capsys.readouterr().out
    forecast = backend.job_repository.get_job(f"{REBUILD_A}_forecast")
    label = backend.job_repository.get_job(f"{REBUILD_A}_label")
    assert forecast.summary == {"weather_at_capture": old_weather}
    assert label.summary == {"answer": old_answer}
    record = backend.repository.get_observation(REBUILD_A)
    assert record["weather_at_capture"] == old_weather
    assert record["answer"] == old_answer


def test_観測への書き込みに失敗してもジョブの前の要約が残る(backend, fake_summary, monkeypatch):
    old_weather, old_answer = seed_with_old_summary(backend, fake_summary)
    original = backend.repository.update_observation_fields

    def broken(observation_id, fields):
        if "answer" in fields or "weather_at_capture" in fields:
            raise ConnectionError("書き込めない")
        original(observation_id, fields)

    monkeypatch.setattr(backend.repository, "update_observation_fields", broken)
    # main は保存先を作り直すので、差し替えた保存先を使うよう関数を直接呼ぶ
    assert rebuild(backend) == (1, 1)
    assert backend.job_repository.get_job(f"{REBUILD_A}_forecast").summary == {
        "weather_at_capture": old_weather
    }
    assert backend.job_repository.get_job(f"{REBUILD_A}_label").summary == {"answer": old_answer}
    record = backend.repository.get_observation(REBUILD_A)
    assert record["weather_at_capture"] == old_weather
    assert record["answer"] == old_answer


def test_作り直すと完了したlabelのジョブがある観測にlabel_doneが補われる(backend, fake_summary):
    ids = {
        status: f"dd000000-0000-4000-8000-00000000000{n}"
        for n, status in enumerate(["done", "failed", "skipped", "pending"], start=1)
    }
    for status, observation_id in ids.items():
        seed_for_rebuild(backend, observation_id, "2026-10-09T03:00:00Z", label=status)
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", forecast="done")
    assert main(["rebuild-summaries"]) == 0
    done = {s: backend.repository.get_observation(i).get("label_done") for s, i in ids.items()}
    assert done == {"done": True, "failed": True, "skipped": True, "pending": None}
    # label のジョブがない観測と、forecast だけの観測には書かない
    assert "label_done" not in backend.repository.get_observation(REBUILD_A)


def test_計算に失敗してもlabel_doneは補われる(backend, fake_summary):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", label="done")
    fake_summary.error = RuntimeError("計算できない")
    assert main(["rebuild-summaries"]) == 1
    assert backend.repository.get_observation(REBUILD_A)["label_done"] is True


def test_処理中に観測が増えたり消えたりしても落ちない(backend, fake_summary, capsys):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", forecast="done")
    seed_for_rebuild(backend, REBUILD_B, "2026-10-09T04:00:00Z", forecast="done")
    added = "ee000000-0000-4000-8000-000000000001"

    def disturb(open_meteo, captured_at):
        # 1件目の処理中に、2件目が消え、新しい観測が増える
        backend.repository.delete_observation(REBUILD_B)
        backend.repository.add_observation(
            {
                "observation_id": added,
                "user_id": "u",
                "captured_at": "2026-10-09T05:00:00Z",
                "received_at": "2026-10-09T05:05:00+00:00",
            }
        )
        return WEATHER_AT_CAPTURE

    fake_summary.weather_at_capture = disturb
    assert main(["rebuild-summaries"]) == 0
    # 消えた観測は数えず、増えた観測はこの回では処理しない
    assert "処理した観測: 1 件、失敗: 0 件" in capsys.readouterr().out
    assert "weather_at_capture" in backend.repository.get_observation(REBUILD_A)
    assert "captured_at_utc" not in backend.repository.get_observation(added)


def test_観測の一覧は先にIDをすべて読んでから1件ずつ処理する(backend, fake_summary, monkeypatch):
    seed_for_rebuild(backend, REBUILD_A, "2026-10-09T03:00:00Z", forecast="done")
    seed_for_rebuild(backend, REBUILD_B, "2026-10-09T04:00:00Z", forecast="done")
    events = []
    repository = backend.repository
    list_ids = repository.list_all_observation_ids
    get_observation = repository.get_observation
    monkeypatch.setattr(
        repository, "list_all_observation_ids", lambda: events.append("list") or list_ids()
    )
    monkeypatch.setattr(
        repository, "get_observation", lambda i: events.append("get") or get_observation(i)
    )
    assert rebuild(backend) == (2, 0)
    assert events == ["list", "get", "get"]
