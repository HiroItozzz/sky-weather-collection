import gzip
import json
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from sky_server.storage import LocalBlobStore
from sky_server.weather.common import PermanentError, RetryableError
from sky_server.weather.open_meteo import build_params, fetch_open_meteo

NOW = datetime(2026, 10, 9, 6, 30, 5, tzinfo=UTC)
# 実データではなく、仕様の形に合わせて手で作った小さな応答
BODY = json.dumps({"hourly": {"time": ["2026-10-09T00:00"], "precipitation_jma_msm": [0.0]}})


def test_パラメーターは仕様の表どおりになる():
    params = build_params(35.6812, 139.7671, datetime(2026, 10, 9, 3, 20, tzinfo=UTC))
    assert params["latitude"] == "35.68"
    assert params["longitude"] == "139.77"
    assert params["models"] == "jma_msm,jma_seamless,best_match"
    assert params["hourly"].startswith("precipitation,weather_code,cloud_cover,")
    assert params["hourly"].endswith(",shortwave_radiation,visibility")
    assert params["minutely_15"].endswith(",wind_direction_10m,shortwave_radiation,visibility,cape")
    assert params["start_hour"] == "2026-10-09T00:00"
    assert params["end_hour"] == "2026-10-09T07:00"
    assert params["start_minutely_15"] == "2026-10-09T02:15"
    assert params["end_minutely_15"] == "2026-10-09T05:30"
    assert params["timezone"] == "GMT"
    assert params["wind_speed_unit"] == "ms"


def test_ちょうど時と15分の境目では範囲が動かない():
    params = build_params(35.0, 139.0, datetime(2026, 10, 9, 3, 0, tzinfo=UTC))
    assert params["start_hour"] == "2026-10-09T00:00"
    assert params["end_hour"] == "2026-10-09T06:00"
    assert params["start_minutely_15"] == "2026-10-09T02:00"
    assert params["end_minutely_15"] == "2026-10-09T05:00"


def test_日付をまたぐときも範囲が正しい():
    params = build_params(35.0, 139.0, datetime(2026, 10, 9, 22, 40, tzinfo=UTC))
    assert params["start_hour"] == "2026-10-09T19:00"
    assert params["end_hour"] == "2026-10-10T02:00"
    assert params["start_minutely_15"] == "2026-10-09T21:30"
    assert params["end_minutely_15"] == "2026-10-10T00:45"

    early = build_params(35.0, 139.0, datetime(2026, 10, 9, 1, 10, tzinfo=UTC))
    assert early["start_hour"] == "2026-10-08T22:00"


def test_UTC以外の時刻もUTCに直して範囲を計算する():
    jst = timezone(timedelta(hours=9))
    params = build_params(35.0, 139.0, datetime(2026, 10, 9, 12, 20, tzinfo=jst))
    assert params["start_hour"] == "2026-10-09T00:00"


def test_取得すると封筒が保存されblob_keyが返る(tmp_path, monkeypatch):
    monkeypatch.setenv("SKY_OPEN_METEO_URL", "https://om.test/v1/forecast")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=BODY)

    store = LocalBlobStore(tmp_path, "weather")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    key = fetch_open_meteo(
        client,
        store,
        observation_id="obs-1",
        phase="forecast",
        captured_at=datetime(2026, 10, 9, 3, 0, tzinfo=UTC),
        lat=35.6812,
        lon=139.7671,
        now=NOW,
    )

    assert key == "raw/open_meteo/obs-1/forecast.json.gz"
    assert len(seen) == 1
    assert seen[0].url.host == "om.test"
    assert seen[0].url.params["latitude"] == "35.68"
    envelope = json.loads(gzip.decompress(store.get(key)))
    assert envelope["provider"] == "open_meteo"
    assert envelope["phase"] == "forecast"
    assert envelope["observation_id"] == "obs-1"
    assert envelope["captured_at"] == "2026-10-09T03:00:00+00:00"
    assert envelope["fetched_at"] == "2026-10-09T06:30:05+00:00"
    assert envelope["attribution"] == "Weather data by Open-Meteo.com"
    assert envelope["license"] == "CC BY 4.0"
    assert envelope["query_location"] == {"lat": "35.68", "lon": "139.77"}
    assert "stations" not in envelope
    (record,) = envelope["requests"]
    assert record["status"] == 200
    assert record["body"] == BODY
    assert record["params"]["models"] == "jma_msm,jma_seamless,best_match"


@pytest.mark.parametrize(
    ("status", "error"), [(500, RetryableError), (429, RetryableError), (400, PermanentError)]
)
def test_失敗したときは何も保存しない(tmp_path, status, error):
    store = LocalBlobStore(tmp_path, "weather")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(error):
        fetch_open_meteo(
            client,
            store,
            observation_id="obs-1",
            phase="label",
            captured_at=NOW,
            lat=35.0,
            lon=139.0,
            now=NOW,
        )
    assert store.get("raw/open_meteo/obs-1/label.json.gz") is None
