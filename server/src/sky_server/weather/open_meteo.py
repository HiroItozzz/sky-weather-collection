"""Open-Meteo の予報 API から、撮影時刻の前後の天気を1回で取得する。"""

from datetime import UTC, datetime, timedelta

import httpx

from sky_server.config import get_open_meteo_url
from sky_server.storage import BlobStore
from sky_server.weather.common import (
    Clock,
    build_envelope,
    ceil_15min,
    ceil_hour,
    ensure_range_ended,
    floor_15min,
    floor_hour,
    get_checked,
    raw_key,
    round_coord,
    save_envelope,
    utc_now,
)

PROVIDER = "open_meteo"
ATTRIBUTION = "Weather data by Open-Meteo.com"
LICENSE = "CC BY 4.0"

MODELS = "jma_msm,jma_seamless,best_match"
HOURLY = (
    "precipitation,weather_code,cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
    "temperature_2m,dew_point_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,"
    "wind_direction_10m,cape,shortwave_radiation,visibility"
)
MINUTELY_15 = (
    "precipitation,weather_code,temperature_2m,relative_humidity_2m,dew_point_2m,"
    "wind_speed_10m,wind_direction_10m,shortwave_radiation,visibility,cape"
)
TIME_FORMAT = "%Y-%m-%dT%H:%M"


def build_params(lat: float, lon: float, captured_at: datetime) -> dict[str, str]:
    """撮影時刻 `captured_at` の前後を取るためのパラメーターを組み立てる。"""
    t = captured_at.astimezone(UTC)

    def fmt(x: datetime) -> str:
        return x.strftime(TIME_FORMAT)

    return {
        "latitude": round_coord(lat),
        "longitude": round_coord(lon),
        "models": MODELS,
        "hourly": HOURLY,
        "minutely_15": MINUTELY_15,
        "start_hour": fmt(floor_hour(t - timedelta(hours=3))),
        "end_hour": fmt(ceil_hour(t + timedelta(hours=3))),
        "start_minutely_15": fmt(floor_15min(t - timedelta(hours=1))),
        "end_minutely_15": fmt(ceil_15min(t + timedelta(hours=2))),
        "timezone": "GMT",
        "wind_speed_unit": "ms",
    }


def _parse(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)


def fetch_open_meteo(
    client: httpx.Client,
    blob_store: BlobStore,
    *,
    observation_id: str,
    phase: str,
    captured_at: datetime,
    lat: float,
    lon: float,
    clock: Clock = utc_now,
) -> str:
    """1回取得して封筒を保存し、`blob_key` を返す。

    失敗したときは `RetryableError` か `PermanentError` を投げ、何も保存しない。
    `label` のときは、範囲の終わりが来ていなければ取得せず `RetryableError` を投げる。
    """
    params = build_params(lat, lon, captured_at)
    if phase == "label":
        ensure_range_ended(
            max(_parse(params["end_hour"]), _parse(params["end_minutely_15"])), clock
        )
    fetched_at = clock()
    request = get_checked(client, get_open_meteo_url(), params, clock)
    envelope = build_envelope(
        provider=PROVIDER,
        phase=phase,
        observation_id=observation_id,
        captured_at=captured_at,
        fetched_at=fetched_at,
        attribution=ATTRIBUTION,
        license=LICENSE,
        query_location={"lat": params["latitude"], "lon": params["longitude"]},
        requests=[request],
    )
    key = raw_key(PROVIDER, observation_id, phase)
    save_envelope(blob_store, key, envelope)
    return key
