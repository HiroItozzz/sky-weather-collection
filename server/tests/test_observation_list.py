"""観測の一覧・記録・1件の表示（ローカル版と Firestore 版）。"""

import base64
import json
import uuid
from dataclasses import dataclass

import pytest
from conftest import RAIN_ANSWER, WEATHER_AT_CAPTURE, Api
from fastapi.testclient import TestClient
from gcp_fakes import FakeFirestoreClient

from sky_server.admin import create_user
from sky_server.gcp.firestore import FirestoreObservationRepository
from sky_server.main import create_app
from sky_server.storage import LocalObservationRepository

T1 = "2026-10-09T01:00:00.000Z"
T2 = "2026-10-09T02:00:00.000Z"
T3 = "2026-10-09T03:00:00.000Z"


def oid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def make_record(n: int, user_id: str, captured_at_utc: str | None = T1, **extra) -> dict:
    record = {
        "observation_id": oid(n),
        "user_id": user_id,
        "captured_at": "2026-10-09T10:00:00+09:00",
        "received_at": "2026-10-09T01:05:00+00:00",
        "user_guess": None,
        **extra,
    }
    if captured_at_utc is not None:
        record["captured_at_utc"] = captured_at_utc
    return record


def encode(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


@dataclass
class Env:
    repository: LocalObservationRepository | FirestoreObservationRepository
    client: TestClient
    user_id: str
    headers: dict

    def add(self, n: int, captured_at_utc: str | None = T1, **extra) -> dict:
        record = make_record(n, self.user_id, captured_at_utc, **extra)
        assert self.repository.add_observation(record)
        return record

    def get(self, path: str, **params):
        return self.client.get(path, headers=self.headers, params=params)


@pytest.fixture(params=["local", "firestore"])
def env(request, tmp_path, monkeypatch, fake_summary) -> Env:
    monkeypatch.delenv("SKY_BACKEND", raising=False)
    if request.param == "local":
        repository = LocalObservationRepository(tmp_path)
    else:
        repository = FirestoreObservationRepository(FakeFirestoreClient())
    client = TestClient(create_app(tmp_path, repository=repository))
    user, token = create_user(repository, "alice")
    return Env(repository, client, user.user_id, {"Authorization": f"Bearer {token}"})


# 保存先


def test_項目を書き換えても他の項目は残る(env):
    env.add(1, answer=None, user_guess="rain")
    env.repository.update_observation_fields(oid(1), {"answer": RAIN_ANSWER, "new": 1})
    record = env.repository.get_observation(oid(1))
    assert record["answer"] == RAIN_ANSWER
    assert record["new"] == 1
    assert record["user_guess"] == "rain"


def test_ない観測の書き換えは例外(env):
    with pytest.raises(Exception):  # noqa: B017 (ローカル版と Firestore 版で例外の型が違う)
        env.repository.update_observation_fields(oid(1), {"answer": RAIN_ANSWER})
    assert env.repository.get_observation(oid(1)) is None


def test_すべての観測を撮影者を問わず読める(env):
    other, _ = create_user(env.repository, "bob")
    env.add(1)
    env.repository.add_observation(make_record(2, other.user_id))
    ids = {record["observation_id"] for record in env.repository.list_all_observations()}
    assert ids == {oid(1), oid(2)}


def test_ページは新しい順で同じ時刻ならIDの降順(env):
    env.add(1, T2)
    env.add(3, T1)
    env.add(2, T2)
    page = env.repository.list_observations_page(env.user_id, 10, None)
    assert [r["observation_id"] for r in page] == [oid(2), oid(1), oid(3)]


def test_ページのbeforeは指定した組より後ろだけを返す(env):
    for n, t in [(1, T1), (2, T2), (3, T2), (4, T3)]:
        env.add(n, t)
    page = env.repository.list_observations_page(env.user_id, 10, (T2, oid(3)))
    assert [r["observation_id"] for r in page] == [oid(2), oid(1)]
    # 指定した組そのものは含まれない
    page = env.repository.list_observations_page(env.user_id, 10, (T1, oid(1)))
    assert page == []


def test_ページのlimitで件数が絞られる(env):
    for n in range(1, 5):
        env.add(n, T1)
    assert len(env.repository.list_observations_page(env.user_id, 3, None)) == 3


def test_captured_at_utcがない観測はページに出ない(env):
    env.add(1, None)
    env.add(2, T1)
    page = env.repository.list_observations_page(env.user_id, 10, None)
    assert [r["observation_id"] for r in page] == [oid(2)]


# 一覧の API


def test_一覧は新しい順で表示の形になっている(env):
    env.add(1, T1)
    env.add(
        2,
        T2,
        user_guess="rain",
        weather_at_capture=WEATHER_AT_CAPTURE,
        answer=RAIN_ANSWER,
    )
    res = env.get("/v1/me/observations")
    assert res.status_code == 200
    body = res.json()
    assert body["next_before"] is None
    assert body["observations"] == [
        {
            "observation_id": oid(2),
            "received_at": "2026-10-09T01:05:00+00:00",
            "captured_at": "2026-10-09T10:00:00+09:00",
            "user_guess": "rain",
            "weather_at_capture": WEATHER_AT_CAPTURE,
            "answer": RAIN_ANSWER,
            "correct": True,
        },
        {
            "observation_id": oid(1),
            "received_at": "2026-10-09T01:05:00+00:00",
            "captured_at": "2026-10-09T10:00:00+09:00",
            "user_guess": None,
            "weather_at_capture": None,
            "answer": {"result": "unknown", "source": None},
            "correct": None,
        },
    ]


def test_観測がなければ空の一覧(env):
    assert env.get("/v1/me/observations").json() == {"observations": [], "next_before": None}


def test_同じ時刻の観測がページの境目に並んでも抜けも重複もない(env):
    # 時刻 T2 の観測が 5 件並び、境目がその途中に来るようにする
    layout = [(1, T1), (2, T2), (3, T2), (4, T2), (5, T2), (6, T2), (7, T3)]
    for n, t in layout:
        env.add(n, t)
    expected = [oid(7), oid(6), oid(5), oid(4), oid(3), oid(2), oid(1)]
    for limit in (1, 2, 3, 4, 6, 7, 8):
        seen = []
        before = None
        for _ in range(20):
            params = {"limit": limit} | ({"before": before} if before else {})
            body = env.get("/v1/me/observations", **params).json()
            seen += [o["observation_id"] for o in body["observations"]]
            before = body["next_before"]
            if before is None:
                break
        assert seen == expected, f"limit={limit}"


def test_ちょうど最後のページではnext_beforeがnull(env):
    for n in range(1, 4):
        env.add(n)
    body = env.get("/v1/me/observations", limit=3).json()
    assert len(body["observations"]) == 3
    assert body["next_before"] is None
    body = env.get("/v1/me/observations", limit=2).json()
    assert body["next_before"] == encode([T1, oid(2)])


def test_limitの既定は50(env):
    for n in range(1, 52):
        env.add(n, T1)
    body = env.get("/v1/me/observations").json()
    assert len(body["observations"]) == 50
    assert body["next_before"] is not None


@pytest.mark.parametrize("limit", [1, 100])
def test_limitの境目は通る(env, limit):
    assert env.get("/v1/me/observations", limit=limit).status_code == 200


@pytest.mark.parametrize("limit", [0, -1, 101, "abc", ""])
def test_limitが範囲外なら422(env, limit):
    assert env.get("/v1/me/observations", limit=limit).status_code == 422


@pytest.mark.parametrize(
    "before",
    [
        "",
        "!!!!",
        "あ",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        encode({"a": 1}),
        encode(None),
        encode("text"),
        encode([]),
        encode([T1]),
        encode([T1, oid(1), "x"]),
        encode([T1, 1]),
        encode([1, oid(1)]),
        encode([None, oid(1)]),
        encode(["2026-10-09T01:00:00Z", oid(1)]),
        encode(["2026-10-09 01:00:00.000Z", oid(1)]),
        encode(["2026-10-09T01:00:00.000+00:00", oid(1)]),
        encode([T1, "not-a-uuid"]),
        encode([T1, ""]),
    ],
)
def test_不正なカーソルは422(env, before):
    env.add(1)
    res = env.get("/v1/me/observations", before=before)
    assert res.status_code == 422


def test_他人の観測は一覧に出ない(env):
    other, _ = create_user(env.repository, "bob")
    env.add(1)
    env.repository.add_observation(make_record(2, other.user_id, T3))
    body = env.get("/v1/me/observations").json()
    assert [o["observation_id"] for o in body["observations"]] == [oid(1)]


def test_カーソルで他人の観測の位置を指しても他人の観測は出ない(env):
    other, _ = create_user(env.repository, "bob")
    env.add(1, T1)
    env.repository.add_observation(make_record(2, other.user_id, T3))
    body = env.get("/v1/me/observations", before=encode([T3, oid(2)])).json()
    assert [o["observation_id"] for o in body["observations"]] == [oid(1)]


def test_一覧と記録は認証が必要(env):
    assert env.client.get("/v1/me/observations").status_code == 401
    assert env.client.get("/v1/me/stats").status_code == 401


# 記録の API


def test_観測がなくても記録は4つのキーを0で返す(env):
    assert env.get("/v1/me/stats").json() == {
        "observations_total": 0,
        "answered_total": 0,
        "guesses_total": 0,
        "guesses_correct": 0,
        "by_category": {"clear": 0, "cloudy": 0, "rain": 0, "unknown": 0},
    }


def test_記録の数え方(env):
    def weather(category: str) -> dict:
        return {**WEATHER_AT_CAPTURE, "category": category}

    def answer(result: str) -> dict:
        return {"result": result, "source": None if result == "unknown" else "amedas"}

    env.add(1, user_guess="rain", weather_at_capture=weather("rain"), answer=answer("rain"))
    env.add(2, user_guess="rain", weather_at_capture=weather("clear"), answer=answer("no_rain"))
    env.add(3, user_guess="no_rain", weather_at_capture=weather("clear"), answer=answer("no_rain"))
    # 答えが出ていない予想は、予想の数にも入れない
    env.add(4, user_guess="rain", weather_at_capture=weather("cloudy"), answer=answer("unknown"))
    # 予想がなくても、答えが出ていれば answered に入る
    env.add(5, weather_at_capture=weather("cloudy"), answer=answer("rain"))
    # 要約がまだない観測
    env.add(6, user_guess="rain")
    env.add(7)
    assert env.get("/v1/me/stats").json() == {
        "observations_total": 7,
        "answered_total": 4,
        "guesses_total": 3,
        "guesses_correct": 2,
        "by_category": {"clear": 2, "cloudy": 2, "rain": 1, "unknown": 2},
    }


def test_他人の観測は記録に数えない(env):
    other, _ = create_user(env.repository, "bob")
    env.add(1)
    env.repository.add_observation(make_record(2, other.user_id, T3, answer=RAIN_ANSWER))
    body = env.get("/v1/me/stats").json()
    assert body["observations_total"] == 1
    assert body["answered_total"] == 0


# 1件の表示の API


def test_1件の表示は要約があればそれを返す(env):
    env.add(1, user_guess="no_rain", weather_at_capture=WEATHER_AT_CAPTURE, answer=RAIN_ANSWER)
    body = env.get(f"/v1/observations/{oid(1)}").json()
    assert body["weather_at_capture"] == WEATHER_AT_CAPTURE
    assert body["answer"] == RAIN_ANSWER
    assert body["user_guess"] == "no_rain"
    assert body["correct"] is False


def test_1件の表示は要約がなければnullと不明を返す(env):
    env.add(1)
    assert env.get(f"/v1/observations/{oid(1)}").json() == {
        "observation_id": oid(1),
        "received_at": "2026-10-09T01:05:00+00:00",
        "captured_at": "2026-10-09T10:00:00+09:00",
        "user_guess": None,
        "weather_at_capture": None,
        "answer": {"result": "unknown", "source": None},
        "correct": None,
    }


def test_他人の観測とない観測は404(env):
    other, _ = create_user(env.repository, "bob")
    env.repository.add_observation(make_record(2, other.user_id))
    assert env.get(f"/v1/observations/{oid(2)}").status_code == 404
    assert env.get(f"/v1/observations/{uuid.uuid4()}").status_code == 404


def test_アップロードした観測を一覧で読める(api: Api, token):
    # PUT で保存した観測に captured_at_utc が入り、一覧に出る
    api.put(token)
    headers = {"Authorization": f"Bearer {token}"}
    body = api.client.get("/v1/me/observations", headers=headers).json()
    assert len(body["observations"]) == 1
