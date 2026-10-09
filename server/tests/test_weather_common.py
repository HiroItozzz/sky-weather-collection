import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import SteppingClock, fixed_clock

from sky_server.storage import LocalBlobStore
from sky_server.weather.common import (
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


def test_クライアントにUser_Agentとタイムアウトが設定される():
    with create_client() as client:
        assert client.headers["User-Agent"].startswith("sky-weather-collection/0.1 ")
        assert client.timeout.read == 20.0


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
