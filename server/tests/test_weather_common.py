import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import SteppingClock, fixed_clock

from sky_server.storage import LocalBlobStore
from sky_server.weather.common import (
    MAX_RESPONSE_BYTES,
    PermanentError,
    RetryableError,
    build_envelope,
    ceil_15min,
    ceil_hour,
    create_client,
    floor_3hours,
    floor_15min,
    floor_hour,
    get_checked,
    raw_key,
    round_coord,
    save_envelope,
)

NOW = datetime(2026, 10, 9, 6, 30, 5, tzinfo=UTC)
CLOCK = fixed_clock(NOW)


def client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_座標は小数第2位に丸めた文字列になる():
    assert round_coord(35.6812) == "35.68"
    assert round_coord(139.7) == "139.70"
    assert round_coord(-0.126) == "-0.13"


def test_時刻の切り捨てと切り上げ():
    t = datetime(2026, 10, 9, 3, 17, 42, tzinfo=UTC)
    assert floor_hour(t) == datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
    assert ceil_hour(t) == datetime(2026, 10, 9, 4, 0, tzinfo=UTC)
    assert floor_15min(t) == datetime(2026, 10, 9, 3, 15, tzinfo=UTC)
    assert ceil_15min(t) == datetime(2026, 10, 9, 3, 30, tzinfo=UTC)
    assert floor_3hours(t) == datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


def test_ちょうど境目の時刻は切り上げても動かない():
    t = datetime(2026, 10, 9, 3, 15, tzinfo=UTC)
    assert ceil_15min(t) == t
    assert ceil_hour(datetime(2026, 10, 9, 3, 0, tzinfo=UTC)) == datetime(
        2026, 10, 9, 3, 0, tzinfo=UTC
    )


def test_切り上げは日付をまたぐ():
    t = datetime(2026, 10, 9, 23, 50, tzinfo=UTC)
    assert ceil_hour(t) == datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    assert ceil_15min(t) == datetime(2026, 10, 10, 0, 0, tzinfo=UTC)


def test_クライアントにUser_Agentと圧縮なしの指定とタイムアウトが設定される():
    with create_client() as client:
        assert client.headers["User-Agent"].startswith("sky-weather-collection/0.1 ")
        assert client.headers["Accept-Encoding"] == "identity"
        assert client.timeout == httpx.Timeout(5.0)
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 5.0
        assert client.timeout.write == 5.0
        assert client.timeout.pool == 5.0


def test_送られるリクエストにAccept_Encodingのidentityが付く(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers["Accept-Encoding"])
        return httpx.Response(200, text="{}")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with create_client() as client:
        client.get("https://example.test/")
    assert seen == ["identity"]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_429と5xxは再試行できる失敗(status):
    client = client_with(lambda request: httpx.Response(status, text="x"))
    with pytest.raises(RetryableError, match=str(status)):
        get_checked(client, "https://example.test/", {}, CLOCK)


def test_接続エラーとタイムアウトは再試行できる失敗():
    def connect_error(request):
        raise httpx.ConnectError("refused")

    def timeout(request):
        raise httpx.ReadTimeout("slow")

    for handler in (connect_error, timeout):
        with pytest.raises(RetryableError):
            get_checked(client_with(handler), "https://example.test/", {}, CLOCK)


def test_本文がJSONでなければ再試行できる失敗():
    client = client_with(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(RetryableError):
        get_checked(client, "https://example.test/", {}, CLOCK)


def test_400は再試行できない失敗で本文の先頭500文字が残る():
    client = client_with(lambda request: httpx.Response(400, text="あ" * 600))
    with pytest.raises(PermanentError) as e:
        get_checked(client, "https://example.test/", {}, CLOCK)
    assert str(e.value) == "HTTP 400: " + "あ" * 500


def test_許可した状態コードは失敗にならない():
    client = client_with(lambda request: httpx.Response(404, text="not json"))
    record = get_checked(
        client, "https://example.test/", {"a": "1"}, CLOCK, allowed_statuses=(404,)
    )
    assert record == {
        "url": "https://example.test/",
        "params": {"a": "1"},
        "status": 404,
        "requested_at": "2026-10-09T06:30:05+00:00",
        "body": "not json",
    }


def test_封筒はgzipを解くと仕様の形になる(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    captured_at = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
    envelope = build_envelope(
        provider="amedas",
        phase="label",
        observation_id="obs-1",
        captured_at=captured_at,
        fetched_at=NOW,
        attribution="出典：気象庁ホームページ（アメダス）",
        license="政府標準利用規約（第2.0版）",
        query_location={"lat": "35.68", "lon": "139.76"},
        requests=[],
        stations=[],
    )
    key = raw_key("amedas", "obs-1", "label")
    assert key == "raw/amedas/obs-1/label.json.gz"
    save_envelope(store, key, envelope)

    saved = json.loads(gzip.decompress(store.get(key)))
    assert list(saved) == [
        "envelope_version",
        "provider",
        "phase",
        "observation_id",
        "captured_at",
        "fetched_at",
        "attribution",
        "license",
        "query_location",
        "stations",
        "requests",
    ]
    assert saved["envelope_version"] == 1
    assert saved["captured_at"] == "2026-10-09T03:00:00+00:00"
    assert saved["fetched_at"] == "2026-10-09T06:30:05+00:00"
    assert (tmp_path / "weather" / key).exists()


def test_stationsはアメダス以外では封筒に入らない():
    envelope = build_envelope(
        provider="open_meteo",
        phase="forecast",
        observation_id="obs-1",
        captured_at=NOW,
        fetched_at=NOW,
        attribution="Weather data by Open-Meteo.com",
        license="CC BY 4.0",
        query_location={"lat": "35.68", "lon": "139.76"},
        requests=[],
    )
    assert "stations" not in envelope


def test_requested_atは呼び出しごとにclockの値になる():
    clock = SteppingClock(NOW, timedelta(seconds=10))
    client = client_with(lambda request: httpx.Response(200, text="{}"))
    first = get_checked(client, "https://example.test/", {}, clock)
    second = get_checked(client, "https://example.test/", {}, clock)
    assert first["requested_at"] == "2026-10-09T06:30:05+00:00"
    assert second["requested_at"] == "2026-10-09T06:30:15+00:00"
    assert clock.calls == 2


def test_requested_atは通信の直前に取る():
    order = []

    def clock():
        order.append("clock")
        return NOW

    def handler(request):
        order.append("request")
        return httpx.Response(200, text="{}")

    get_checked(client_with(handler), "https://example.test/", {}, clock)
    assert order == ["clock", "request"]


def test_応答がちょうど5MBなら受け取る():
    body = b" " * (MAX_RESPONSE_BYTES - 2) + b"{}"
    client = client_with(lambda request: httpx.Response(200, content=body))
    record = get_checked(client, "https://example.test/", {}, CLOCK)
    assert len(record["body"]) == MAX_RESPONSE_BYTES


def test_応答が5MBを1バイト超えたら再試行できる失敗():
    body = b" " * (MAX_RESPONSE_BYTES - 1) + b"{}"
    client = client_with(lambda request: httpx.Response(200, content=body))
    with pytest.raises(RetryableError, match="大きすぎる"):
        get_checked(client, "https://example.test/", {}, CLOCK)


def test_大きさは複数に分けて届く本文でも合計で数える():
    def chunks():
        for _ in range(6):
            yield b" " * (1024 * 1024)

    client = client_with(lambda request: httpx.Response(200, content=chunks()))
    with pytest.raises(RetryableError, match="大きすぎる"):
        get_checked(client, "https://example.test/", {}, CLOCK)


def test_圧縮された応答は解いたあとの大きさを数える():
    body = gzip.compress(b" " * (MAX_RESPONSE_BYTES + 1))
    assert len(body) < 100 * 1024
    client = client_with(
        lambda request: httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"})
    )
    with pytest.raises(RetryableError, match="大きすぎる"):
        get_checked(client, "https://example.test/", {}, CLOCK)


class FakeMonotonic:
    """呼ばれるたびに `step` 秒ずつ進む時計。"""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._now
        self._now += self._step
        return now


def slow_body():
    yield b"{"
    yield b"}"


def test_呼び出し全体の期限を過ぎたら再試行できる失敗():
    client = client_with(lambda request: httpx.Response(200, content=slow_body()))
    # 開始で 0 秒、1つ目の塊を読んだあとで 15 秒、2つ目のあとで 30 秒になる
    with pytest.raises(RetryableError, match="期限"):
        get_checked(client, "https://example.test/", {}, CLOCK, monotonic=FakeMonotonic(15.0))


def test_期限の内側なら受け取る():
    client = client_with(lambda request: httpx.Response(200, content=slow_body()))
    record = get_checked(client, "https://example.test/", {}, CLOCK, monotonic=FakeMonotonic(5.0))
    assert record["body"] == "{}"
