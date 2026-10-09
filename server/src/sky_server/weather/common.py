"""天気データの取得に共通の部品：丸め、時刻の計算、失敗の分類、封筒の保存。"""

import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from sky_server.storage import BlobStore

USER_AGENT = "sky-weather-collection/0.1 (+https://github.com/hiroitozzz/sky-weather-collection)"
TIMEOUT_S = 20.0
ENVELOPE_VERSION = 1
ERROR_BODY_LIMIT = 500

# 現在時刻を返す関数。テストでは固定の時刻を返すものに差し替える
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class RetryableError(Exception):
    """あとでやり直せば成功するかもしれない失敗。"""


class PermanentError(Exception):
    """やり直しても成功しない失敗。"""


def create_client() -> httpx.Client:
    """User-Agent とタイムアウトを設定した HTTP クライアントを作る。"""
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)


def round_coord(value: float) -> str:
    """外部 API に渡す座標を、小数第2位に丸めた文字列にする。"""
    return f"{value:.2f}"


def floor_to_minutes(t: datetime, step_min: int) -> datetime:
    """時刻を `step_min` 分単位に切り捨てる。`step_min` は 1440 の約数。同じタイムゾーンで返す。"""
    minutes = t.hour * 60 + t.minute
    minutes -= minutes % step_min
    return t.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)


def ceil_to_minutes(t: datetime, step_min: int) -> datetime:
    """時刻を `step_min` 分単位に切り上げる。ちょうど境目ならそのまま。"""
    floored = floor_to_minutes(t, step_min)
    return floored if floored == t else floored + timedelta(minutes=step_min)


def floor_hour(t: datetime) -> datetime:
    return floor_to_minutes(t, 60)


def ceil_hour(t: datetime) -> datetime:
    return ceil_to_minutes(t, 60)


def floor_15min(t: datetime) -> datetime:
    return floor_to_minutes(t, 15)


def ceil_15min(t: datetime) -> datetime:
    return ceil_to_minutes(t, 15)


def floor_3hours(t: datetime) -> datetime:
    return floor_to_minutes(t, 180)


def classify_response(response: httpx.Response) -> None:
    """応答が成功でなければ、失敗の種類に応じた例外を投げる。

    再試行できるのは 429、5xx、本文が JSON として読めない場合。
    それ以外の 4xx など（パラメーターの誤りなど）は再試行できない。
    """
    status = response.status_code
    if status == 429 or status >= 500:
        raise RetryableError(f"HTTP {status}")
    if not 200 <= status < 300:
        raise PermanentError(f"HTTP {status}: {response.text[:ERROR_BODY_LIMIT]}")
    try:
        json.loads(response.text)
    except ValueError as e:
        raise RetryableError(f"本文が JSON として読めない: {e}") from e


def get_checked(
    client: httpx.Client,
    url: str,
    params: dict[str, str],
    clock: Clock,
    allowed_statuses: tuple[int, ...] = (),
) -> dict:
    """GET して、封筒の `requests` に入れる1件分の記録を返す。

    `allowed_statuses` に含まれる状態コードは失敗にせず、本文の検査もしない。
    接続エラーとタイムアウトは `RetryableError` にする。`requested_at` は呼び出しの直前の時刻。
    """
    requested_at = clock()
    try:
        response = client.get(url, params=params)
    except httpx.TransportError as e:
        raise RetryableError(f"{type(e).__name__}: {e}") from e
    if response.status_code not in allowed_statuses:
        classify_response(response)
    return {
        "url": url,
        "params": params,
        "status": response.status_code,
        "requested_at": requested_at.astimezone(UTC).isoformat(),
        "body": response.text,
    }


def ensure_range_ended(range_end: datetime, clock: Clock) -> None:
    """取得する範囲の終わりがまだ来ていなければ、再試行できる失敗にする（ラベル用）。"""
    now = clock()
    if now < range_end:
        raise RetryableError(
            f"取得する範囲の終わり（{range_end.astimezone(UTC).isoformat()}）がまだ来ていない"
        )


def raw_key(provider: str, observation_id: str, phase: str) -> str:
    return f"raw/{provider}/{observation_id}/{phase}.json.gz"


def build_envelope(
    *,
    provider: str,
    phase: str,
    observation_id: str | None,
    captured_at: datetime | None,
    fetched_at: datetime,
    attribution: str,
    license: str,
    query_location: dict[str, str] | None,
    requests: list[dict],
    stations: list[dict] | None = None,
) -> dict:
    """生レスポンスの封筒（仕様 5.2節）を作る。`stations` はアメダスのときだけ入れる。"""
    envelope = {
        "envelope_version": ENVELOPE_VERSION,
        "provider": provider,
        "phase": phase,
        "observation_id": observation_id,
        "captured_at": captured_at.astimezone(UTC).isoformat() if captured_at else None,
        "fetched_at": fetched_at.astimezone(UTC).isoformat(),
        "attribution": attribution,
        "license": license,
        "query_location": query_location,
    }
    if stations is not None:
        envelope["stations"] = stations
    envelope["requests"] = requests
    return envelope


def save_envelope(blob_store: BlobStore, key: str, envelope: dict) -> None:
    """封筒を gzip にして保存する。"""
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    blob_store.put(key, gzip.compress(data))


def load_envelope(blob_store: BlobStore, key: str) -> dict | None:
    """保存した封筒を読む。なければ、または壊れていれば None。"""
    data = blob_store.get(key)
    if data is None:
        return None
    try:
        return json.loads(gzip.decompress(data))
    except (OSError, EOFError, ValueError):
        return None
