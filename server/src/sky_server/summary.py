"""生レスポンスの封筒から、撮影時の天気と答え合わせの要約を作る純粋な関数。

ファイルや API には触れない。本文が JSON として読めないときや形が違うときは、
例外を投げずに「そろわない」として扱う。
"""

import json
import math
from datetime import UTC, datetime, timedelta, timezone

from sky_server.weather.common import ceil_hour, floor_to_minutes

JST = timezone(timedelta(hours=9))
TIME_FORMAT = "%Y-%m-%dT%H:%M"
MODEL_SUFFIX = "_jma_seamless"
# アメダスの観測点は、この距離（km）以内のものだけを使う
MAX_DISTANCE_KM = 10
# 答え合わせの対象の時間帯は (t, t+60分]
WINDOW = timedelta(minutes=60)
# Open-Meteo の15分値の合計が、この値（mm）以上なら雨とする
RAIN_THRESHOLD_MM = 0.1


def category(weather_code: int | None) -> str | None:
    """WMO の天気コードを `clear` / `cloudy` / `rain` のどれかにする。決まらなければ None。"""
    if weather_code is None or isinstance(weather_code, bool):
        return None
    if 0 <= weather_code <= 2:
        return "clear"
    if weather_code in (3, 45, 48):
        return "cloudy"
    if weather_code >= 51:
        return "rain"
    return None


def _as_utc(t: datetime) -> datetime:
    """タイムゾーンのない時刻は UTC とみなして、UTC に直す。"""
    if t.tzinfo is None:
        return t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _bodies(envelope: object, accept=lambda request: True) -> list[dict]:
    """封筒の `requests` のうち、状態が 200 で `accept` を満たすものの本文を、dict にして返す。

    読めない本文や dict でない本文は飛ばす。
    """
    if not isinstance(envelope, dict) or not isinstance(envelope.get("requests"), list):
        return []
    bodies = []
    for request in envelope["requests"]:
        if not isinstance(request, dict) or request.get("status") != 200 or not accept(request):
            continue
        try:
            body = json.loads(request["body"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


def _open_meteo_body(envelope: object) -> dict | None:
    bodies = _bodies(envelope)
    return bodies[0] if bodies else None


def _series(body: dict, group: str, name: str) -> tuple[list, list] | None:
    """`body[group]` から、時刻の配列と変数の配列を取り出す。形が違えば None。"""
    block = body.get(group)
    if not isinstance(block, dict):
        return None
    times, values = block.get("time"), block.get(name)
    if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
        return None
    return times, values


def weather_at_capture(open_meteo_envelope: dict | None, captured_at: datetime) -> dict | None:
    """撮影時刻を含む1時間 (T-1h, T] の天気を返す。T は撮影時刻を切り上げた時刻。

    `weather_code` が取れないか `category` が決まらないときは None。
    ほかの値は、取れなければその項目だけ None にする。
    """
    body = _open_meteo_body(open_meteo_envelope)
    if body is None:
        return None
    hourly = body.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        return None
    key = ceil_hour(_as_utc(captured_at)).strftime(TIME_FORMAT)
    try:
        index = hourly["time"].index(key)
    except ValueError:
        return None

    def pick(name: str) -> object:
        values = hourly.get(name + MODEL_SUFFIX)
        if not isinstance(values, list) or index >= len(values):
            return None
        return values[index]

    code = pick("weather_code")
    if _is_number(code) and float(code).is_integer():
        code = int(code)
    else:
        return None
    result_category = category(code)
    if result_category is None:
        return None

    def number_or_none(name: str) -> float | int | None:
        value = pick(name)
        return value if _is_number(value) else None

    return {
        "category": result_category,
        "weather_code": code,
        "temperature_c": number_or_none("temperature_2m"),
        "precipitation_mm": number_or_none("precipitation"),
        "cloud_cover_pct": number_or_none("cloud_cover"),
    }


def _slots(captured_at: datetime, step_min: int) -> list[datetime]:
    """(t, t+60分] に入る、`step_min` 分刻みの時刻を古い順に返す。"""
    t = _as_utc(captured_at)
    # t ちょうどが刻みに乗っているときも、t は含まないので1刻み先から始める
    k = floor_to_minutes(t, step_min) + timedelta(minutes=step_min)
    slots = []
    while k <= t + WINDOW:
        slots.append(k)
        k += timedelta(minutes=step_min)
    return slots


def _amedas_stations(envelope: dict) -> list[str]:
    """10km 以内の観測点の地点番号を、近い順に返す。"""
    stations = envelope.get("stations")
    if not isinstance(stations, list):
        return []
    near = [
        s
        for s in stations
        if isinstance(s, dict)
        and isinstance(s.get("code"), str)
        and _is_number(s.get("distance_km"))
        and s["distance_km"] <= MAX_DISTANCE_KM
    ]
    near.sort(key=lambda s: s["distance_km"])
    return [s["code"] for s in near]


def _amedas_rain(envelope: dict, code: str, captured_at: datetime) -> bool | None:
    """1つの観測点で雨かどうかを返す。6つの値がそろわなければ None。"""
    marker = f"/point/{code}/"
    data: dict = {}
    for body in _bodies(envelope, lambda r: isinstance(r.get("url"), str) and marker in r["url"]):
        data.update(body)
    values = []
    for k in _slots(captured_at, 10):
        entry = data.get(k.astimezone(JST).strftime("%Y%m%d%H%M%S"))
        if not isinstance(entry, dict):
            return None
        precipitation = entry.get("precipitation10m")
        if not isinstance(precipitation, list) or not precipitation:
            return None
        if not _is_number(precipitation[0]):
            return None
        values.append(precipitation[0])
    return any(v > 0 for v in values)


def _open_meteo_rain(envelope: object, captured_at: datetime) -> bool | None:
    """Open-Meteo の15分値の合計から雨かどうかを返す。4つの値がそろわなければ None。"""
    body = _open_meteo_body(envelope)
    if body is None:
        return None
    series = _series(body, "minutely_15", "precipitation" + MODEL_SUFFIX)
    if series is None:
        return None
    times, values = series
    total = 0.0
    for k in _slots(captured_at, 15):
        key = k.strftime(TIME_FORMAT)
        if key not in times:
            return None
        value = values[times.index(key)]
        if not _is_number(value):
            return None
        total += value
    # 浮動小数の誤差で境目がずれないように、小数第6位で丸めてから比べる
    return round(total, 6) >= RAIN_THRESHOLD_MM


def answer(
    open_meteo_envelope: dict | None, amedas_envelope: dict | None, captured_at: datetime
) -> dict:
    """撮影の後の1時間 (t, t+60分] に雨が降ったかを返す。

    アメダス（10km 以内の近い順）、Open-Meteo の順に、値がそろうものを使う。
    どちらもそろわなければ `unknown`。
    """
    if isinstance(amedas_envelope, dict):
        for code in _amedas_stations(amedas_envelope):
            rain = _amedas_rain(amedas_envelope, code, captured_at)
            if rain is not None:
                return {"result": "rain" if rain else "no_rain", "source": "amedas"}
    rain = _open_meteo_rain(open_meteo_envelope, captured_at)
    if rain is not None:
        return {"result": "rain" if rain else "no_rain", "source": "open_meteo"}
    return {"result": "unknown", "source": None}


def is_correct(user_guess: str | None, answer: dict | None) -> bool | None:
    """予想が答えと一致したか。予想がないか答えが `unknown` なら None。"""
    if user_guess is None or answer is None or answer.get("result") == "unknown":
        return None
    return user_guess == answer.get("result")
