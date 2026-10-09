"""取得関数が実際に保存した封筒を、要約の関数に渡して通しで確かめる。

`httpx.MockTransport` で取得関数（`fetch_open_meteo`、`fetch_amedas`）を動かし、
`LocalBlobStore` に保存された封筒を `load_envelope` で読み直して使う。
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import httpx
from conftest import fixed_clock

from sky_server.storage import LocalBlobStore
from sky_server.summary import answer, weather_at_capture
from sky_server.weather.amedas import fetch_amedas
from sky_server.weather.common import load_envelope
from sky_server.weather.open_meteo import MODELS, fetch_open_meteo

JST = timezone(timedelta(hours=9))
# 撮影は 03:10 UTC（12:10 JST）。ラベルの取得は範囲が終わったあとの時刻で行う
CAPTURED_AT = datetime(2026, 10, 9, 3, 10, tzinfo=UTC)
NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
CLOCK = fixed_clock(NOW)
LAT, LON = 35.68, 139.76
OBSERVATION_ID = "obs-1"

# 以下の応答の本文は実データではなく、`models=jma_msm,jma_seamless,best_match` を指定したときの形
# （変数名の後ろにモデル名が付く）に合わせて手で作ったもの。
# jma_seamless 以外の値は、モデルの選び間違いに気づけるように、わざと違う値にしてある。


def model_values(name: str, per_model: dict[str, list]) -> dict[str, list]:
    return {f"{name}_{model}": values for model, values in per_model.items()}


def open_meteo_body(quarter_values: list[float]) -> str:
    """1時間値は 02:00〜05:00 の4行、15分値は 03:15〜04:00 の4つ（値は `quarter_values`）。"""
    hourly_time = [f"2026-10-09T{h:02d}:00" for h in range(2, 6)]
    quarter_time = ["2026-10-09T03:15", "2026-10-09T03:30", "2026-10-09T03:45", "2026-10-09T04:00"]
    hourly = {"time": hourly_time}
    hourly.update(
        model_values(
            "weather_code",
            {
                "jma_msm": [95, 95, 95, 95],
                "jma_seamless": [0, 1, 61, 3],
                "best_match": [99, 99, 99, 99],
            },
        )
    )
    hourly.update(
        model_values(
            "temperature_2m",
            {
                "jma_msm": [1.0, 1.0, 1.0, 1.0],
                "jma_seamless": [20.0, 21.5, 19.0, 18.0],
                "best_match": [2.0, 2.0, 2.0, 2.0],
            },
        )
    )
    hourly.update(
        model_values(
            "precipitation",
            {
                "jma_msm": [9.0, 9.0, 9.0, 9.0],
                "jma_seamless": [0.0, 0.0, 1.2, 0.4],
                "best_match": [8.0, 8.0, 8.0, 8.0],
            },
        )
    )
    hourly.update(
        model_values(
            "cloud_cover",
            {
                "jma_msm": [100, 100, 100, 100],
                "jma_seamless": [10, 20, 90, 70],
                "best_match": [50, 50, 50, 50],
            },
        )
    )
    minutely = {"time": quarter_time}
    minutely.update(
        model_values(
            "precipitation",
            {
                "jma_msm": [5.0] * 4,
                "jma_seamless": quarter_values,
                "best_match": [6.0] * 4,
            },
        )
    )
    return json.dumps({"hourly": hourly, "minutely_15": minutely})


def amedas_entry(value: float) -> dict:
    return {"precipitation10m": [value, 0], "temp": [20.0, 0]}


def amedas_point_body(overrides: dict[str, float] | None = None) -> str:
    """JST の 12:00〜15:00 の10分値を作る。キーは JST の YYYYMMDDHHMMSS。"""
    data = {}
    t = datetime(2026, 10, 9, 12, 0, tzinfo=JST)
    while t <= datetime(2026, 10, 9, 15, 0, tzinfo=JST):
        data[t.strftime("%Y%m%d%H%M%S")] = amedas_entry(0.0)
        t += timedelta(minutes=10)
    for key, value in (overrides or {}).items():
        data[key] = amedas_entry(value)
    return json.dumps(data)


def station(lat: float, lon: float, name: str) -> dict:
    return {
        "type": "A",
        "elems": "11112010",
        "lat": [int(lat), (lat - int(lat)) * 60],
        "lon": [int(lon), (lon - int(lon)) * 60],
        "alt": 10,
        "kjName": name,
    }


NEAR_TABLE = {"44132": station(35.70, 139.75, "近い")}  # 撮影地点から約2.3km
FAR_TABLE = {"99999": station(36.50, 140.50, "遠い")}  # 撮影地点から約100km以上


def make_client(open_meteo_text: str, amedas_table: dict, amedas_point_text: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("amedastable.json"):
            return httpx.Response(200, text=json.dumps(amedas_table))
        if "/amedas/data/point/" in url:
            return httpx.Response(200, text=amedas_point_text)
        # 取得関数が、仕様どおりのモデルを指定していることも確かめる
        assert request.url.params["models"] == MODELS
        return httpx.Response(200, text=open_meteo_text)

    return httpx.Client(transport=httpx.MockTransport(handler))


def run_pipeline(
    tmp_path, open_meteo_text: str, amedas_table: dict, amedas_point_text: str
) -> tuple[dict, dict, dict]:
    """取得して保存し、読み直した封筒 `(forecast, label, amedas)` を返す。"""
    store = LocalBlobStore(tmp_path)
    client = make_client(open_meteo_text, amedas_table, amedas_point_text)
    common = {
        "observation_id": OBSERVATION_ID,
        "captured_at": CAPTURED_AT,
        "lat": LAT,
        "lon": LON,
        "clock": CLOCK,
    }
    forecast_key = fetch_open_meteo(client, store, phase="forecast", **common)
    label_key = fetch_open_meteo(client, store, phase="label", **common)
    amedas_key = fetch_amedas(
        client, store, phase="label", sleep=lambda s: None, interval_s=0, **common
    )
    envelopes = tuple(load_envelope(store, key) for key in (forecast_key, label_key, amedas_key))
    assert all(e is not None for e in envelopes)
    return envelopes


def test_Open_Meteoの封筒から撮影時の天気が出る(tmp_path):
    forecast, _, _ = run_pipeline(
        tmp_path, open_meteo_body([0.0] * 4), NEAR_TABLE, amedas_point_body()
    )
    # 03:10 に最も近い時刻は 03:00（天気・気温・雲量）。降水量は (03:00, 04:00] の 04:00 の行
    assert weather_at_capture(forecast, CAPTURED_AT) == {
        "category": "clear",
        "weather_code": 1,
        "temperature_c": 21.5,
        "precipitation_mm": 1.2,
        "cloud_cover_pct": 20,
    }


def test_撮影が30分ちょうどなら天気は次の時刻の行になる(tmp_path):
    forecast, _, _ = run_pipeline(
        tmp_path, open_meteo_body([0.0] * 4), NEAR_TABLE, amedas_point_body()
    )
    result = weather_at_capture(forecast, datetime(2026, 10, 9, 3, 30, tzinfo=UTC))
    assert result["weather_code"] == 61
    assert result["precipitation_mm"] == 1.2


def test_アメダスが10km以内なら雨かどうかをアメダスで答える(tmp_path):
    # 12:40 JST（03:40 UTC）の10分値が 0.5mm。Open_Meteo の15分値は 0 なので、
    # 雨と答えたならアメダスを使ったことになる
    amedas_text = amedas_point_body({"20261009124000": 0.5})
    _, label, amedas = run_pipeline(tmp_path, open_meteo_body([0.0] * 4), NEAR_TABLE, amedas_text)
    assert answer(label, amedas, CAPTURED_AT) == {"result": "rain", "source": "amedas"}


def test_アメダスが10km以内で降っていなければ降らなかったと答える(tmp_path):
    # Open_Meteo の15分値が 0.5 でも、アメダスがそろっていればアメダスを使う
    _, label, amedas = run_pipeline(
        tmp_path, open_meteo_body([0.5] * 4), NEAR_TABLE, amedas_point_body()
    )
    assert answer(label, amedas, CAPTURED_AT) == {"result": "no_rain", "source": "amedas"}


def test_アメダスが遠いときはOpen_Meteoの15分値で答える(tmp_path):
    # アメダスの10分値は雨を示しているが、観測点が遠いので使わない
    amedas_text = amedas_point_body({"20261009124000": 5.0})
    _, label, amedas = run_pipeline(
        tmp_path, open_meteo_body([0.0, 0.05, 0.0, 0.05]), FAR_TABLE, amedas_text
    )
    assert amedas["stations"][0]["distance_km"] > 10
    # 03:15、03:30、03:45、04:00 の合計 0.1mm ちょうどで雨
    assert answer(label, amedas, CAPTURED_AT) == {"result": "rain", "source": "open_meteo"}


def test_アメダスが遠く15分値も少なければ降らなかったと答える(tmp_path):
    _, label, amedas = run_pipeline(
        tmp_path, open_meteo_body([0.0, 0.03, 0.0, 0.03]), FAR_TABLE, amedas_point_body()
    )
    assert answer(label, amedas, CAPTURED_AT) == {"result": "no_rain", "source": "open_meteo"}


def test_どちらの封筒もなければ不明になる():
    assert answer(None, None, CAPTURED_AT) == {"result": "unknown", "source": None}
