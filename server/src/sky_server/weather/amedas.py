"""アメダス（気象庁サイトの内部 JSON）から、最寄りの観測点の10分値を取得する。"""

import json
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx

from sky_server.config import get_amedas_interval_s
from sky_server.storage import BlobStore
from sky_server.weather.common import (
    PermanentError,
    RetryableError,
    build_envelope,
    floor_3hours,
    get_checked,
    load_envelope,
    raw_key,
    round_coord,
    save_envelope,
)

PROVIDER = "amedas"
ATTRIBUTION = "出典：気象庁ホームページ（アメダス）"
LICENSE = "政府標準利用規約（第2.0版）"

TABLE_URL = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
POINT_URL = "https://www.jma.go.jp/bosai/amedas/data/point/{code}/{date}_{hh}.json"
TABLE_KEY = "cache/amedas/amedastable.json.gz"
TABLE_MAX_AGE = timedelta(days=7)
STATION_COUNT = 3
EARTH_RADIUS_KM = 6371.0
JST = timezone(timedelta(hours=9))


def dms_to_degrees(value: list[float]) -> float:
    """`[度, 分]` を度に直す。"""
    return value[0] + value[1] / 60


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の大円距離（km）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def load_station_table(client: httpx.Client, blob_store: BlobStore, now: datetime) -> dict:
    """観測点の一覧を返す。キャッシュが7日より古ければ取り直す。

    取り直しに再試行できる失敗をしたときは、古いキャッシュがあればそれを使う。
    """
    stale = None
    cached = load_envelope(blob_store, TABLE_KEY)
    if cached is not None:
        try:
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            table = json.loads(cached["requests"][0]["body"])
            if now - fetched_at < TABLE_MAX_AGE:
                return table
            stale = table
        except (KeyError, IndexError, TypeError, ValueError):
            pass  # 壊れたキャッシュは使わず、取り直す
    try:
        request = get_checked(client, TABLE_URL, {}, now)
    except RetryableError:
        if stale is None:
            raise
        return stale
    envelope = build_envelope(
        provider=PROVIDER,
        phase="station_table",
        observation_id=None,
        captured_at=None,
        fetched_at=now,
        attribution=ATTRIBUTION,
        license=LICENSE,
        query_location=None,
        requests=[request],
    )
    save_envelope(blob_store, TABLE_KEY, envelope)
    return json.loads(request["body"])


def select_stations(table: dict, lat: float, lon: float) -> list[dict]:
    """撮影地点に近い順に、最寄りの3地点を返す。

    `elems` の2文字目が `1` の地点（降水量を観測している地点）を優先する。
    該当する地点が1つもなければ、全地点から選ぶ。
    """
    rain = {code: s for code, s in table.items() if str(s.get("elems", ""))[1:2] == "1"}
    candidates = rain or table
    stations = []
    for code, s in candidates.items():
        s_lat = dms_to_degrees(s["lat"])
        s_lon = dms_to_degrees(s["lon"])
        stations.append(
            {
                "code": code,
                "name": s.get("kjName"),
                "lat": s_lat,
                "lon": s_lon,
                "distance_km": round(haversine_km(lat, lon, s_lat, s_lon), 1),
                "_distance": haversine_km(lat, lon, s_lat, s_lon),
            }
        )
    stations.sort(key=lambda s: s["_distance"])
    for s in stations:
        del s["_distance"]
    return stations[:STATION_COUNT]


def file_slots(captured_at: datetime) -> list[tuple[str, str]]:
    """取得するファイルの `(YYYYMMDD, HH)` を古い順に返す。日時は日本時間。

    範囲は撮影時刻の1時間前から3時間後まで。`HH` は3時間単位で、
    範囲の始まりと終わりをそれぞれ切り捨てた時刻のあいだを3時間おきに並べる。
    """
    start = floor_3hours(captured_at.astimezone(JST) - timedelta(hours=1))
    last = floor_3hours(captured_at.astimezone(JST) + timedelta(hours=3))
    slots = []
    t = start
    while t <= last:
        slots.append((t.strftime("%Y%m%d"), t.strftime("%H")))
        t += timedelta(hours=3)
    return slots


def fetch_amedas(
    client: httpx.Client,
    blob_store: BlobStore,
    *,
    observation_id: str,
    phase: str,
    captured_at: datetime,
    lat: float,
    lon: float,
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
    interval_s: float | None = None,
) -> str:
    """最寄り3地点の10分値を取得して封筒を保存し、`blob_key` を返す。

    404 はデータがないだけなので失敗にしない。それ以外で失敗したら例外を投げ、
    何も保存しない（部分的な保存はしない）。
    """
    if interval_s is None:
        interval_s = get_amedas_interval_s()
    table = load_station_table(client, blob_store, now)
    stations = select_stations(table, lat, lon)
    if not stations:
        raise PermanentError("観測点の一覧に地点がない")

    requests = []
    for station in stations:
        for date, hh in file_slots(captured_at):
            if requests:
                sleep(interval_s)
            url = POINT_URL.format(code=station["code"], date=date, hh=hh)
            requests.append(get_checked(client, url, {}, now, allowed_statuses=(404,)))

    envelope = build_envelope(
        provider=PROVIDER,
        phase=phase,
        observation_id=observation_id,
        captured_at=captured_at,
        fetched_at=now,
        attribution=ATTRIBUTION,
        license=LICENSE,
        query_location={"lat": round_coord(lat), "lon": round_coord(lon)},
        requests=requests,
        stations=stations,
    )
    key = raw_key(PROVIDER, observation_id, phase)
    save_envelope(blob_store, key, envelope)
    return key
