import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from sky_server.summary import answer, category, is_correct, weather_at_capture

JST = timezone(timedelta(hours=9))
TIME_FORMAT = "%Y-%m-%dT%H:%M"

# 以下の封筒はすべて実データではなく、docs/m4-weather.md 5.2節の形に合わせて手で作ったもの。
# アメダスのキーは JST の YYYYMMDDHHMMSS、値は precipitation10m に [降水量, 品質の印] を持つ dict。
# Open-Meteo は jma_seamless の変数名（後ろに _jma_seamless が付く）だけを入れる。
T = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)  # JST では 12:00


def at(hour: int, minute: int, day: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def amedas_key(t: datetime) -> str:
    return t.astimezone(JST).strftime("%Y%m%d%H%M%S")


def entry(value: float | None, quality: int = 0) -> dict:
    """10分値のキー1つ分。`precipitation10m` は `[降水量, 品質の印]`。"""
    return {"precipitation10m": [value, quality], "temp": [20.0, 0]}


def amedas_data(start: datetime, end: datetime, overrides: dict | None = None) -> dict:
    """`start` から `end` まで（両端を含む）10分刻みのキーを、降水量 0.0 で作る。"""
    data = {}
    t = start
    while t <= end:
        data[amedas_key(t)] = entry(0.0)
        t += timedelta(minutes=10)
    for t, value in (overrides or {}).items():
        if value is None:
            data.pop(amedas_key(t), None)
        else:
            data[amedas_key(t)] = entry(value)
    return data


def amedas_request(code: str, data: dict, status: int = 200, hh: str = "12") -> dict:
    return {
        "url": f"https://www.jma.go.jp/bosai/amedas/data/point/{code}/20261009_{hh}.json",
        "params": {},
        "status": status,
        "requested_at": "2026-10-09T06:30:05+00:00",
        "body": json.dumps(data) if status == 200 else "",
    }


def amedas_envelope(stations: list[tuple[str, float]], requests: list[dict]) -> dict:
    return {
        "envelope_version": 1,
        "provider": "amedas",
        "phase": "label",
        "stations": [
            {"code": code, "name": "観測点", "lat": 35.0, "lon": 139.0, "distance_km": distance}
            for code, distance in stations
        ],
        "requests": requests,
    }


def simple_amedas(data: dict, distance: float = 1.6, code: str = "44132") -> dict:
    return amedas_envelope([(code, distance)], [amedas_request(code, data)])


def open_meteo_envelope(
    minutely: dict[datetime, float | None] | None = None,
    hourly: dict | None = None,
) -> dict:
    """15分値は `minutely` で指定した時刻だけを入れる。1時間値は `hourly` をそのまま入れる。"""
    minutely = minutely or {}
    body = {
        "minutely_15": {
            "time": [t.strftime(TIME_FORMAT) for t in minutely],
            "precipitation_jma_seamless": list(minutely.values()),
        },
        "hourly": hourly or {"time": []},
    }
    return {
        "envelope_version": 1,
        "provider": "open_meteo",
        "phase": "forecast",
        "requests": [
            {"url": "https://example.test/v1/forecast", "status": 200, "body": json.dumps(body)}
        ],
    }


def quarter_hours(start: datetime, end: datetime, value: float = 0.0) -> dict[datetime, float]:
    values = {}
    t = start
    while t <= end:
        values[t] = value
        t += timedelta(minutes=15)
    return values


def hourly_body(
    rows: list[tuple[str, int | None, float | None, float | None, float | None]],
) -> dict:
    """`(時刻, weather_code, temperature_2m, precipitation, cloud_cover)` の行から1時間値を作る。"""
    return {
        "time": [r[0] for r in rows],
        "weather_code_jma_seamless": [r[1] for r in rows],
        "temperature_2m_jma_seamless": [r[2] for r in rows],
        "precipitation_jma_seamless": [r[3] for r in rows],
        "cloud_cover_jma_seamless": [r[4] for r in rows],
    }


HOURLY = hourly_body(
    [
        ("2026-10-09T02:00", 0, 20.0, 0.0, 10),
        ("2026-10-09T03:00", 1, 21.5, 0.0, 20),
        ("2026-10-09T04:00", 61, 19.0, 1.2, 90),
    ]
)


# ---- category ----


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, "clear"),
        (1, "clear"),
        (2, "clear"),
        (3, "cloudy"),
        (4, None),
        (44, None),
        (45, "cloudy"),
        (46, None),
        (48, "cloudy"),
        (49, None),
        (50, None),
        (51, "rain"),
        (61, "rain"),
        (99, "rain"),
        (-1, None),
        (None, None),
    ],
)
def test_カテゴリは天気コードの境目で分かれる(code, expected):
    assert category(code) == expected


# ---- weather_at_capture ----


def test_撮影時刻がちょうど時のときはその時刻の行を使う():
    result = weather_at_capture(open_meteo_envelope(hourly=HOURLY), at(3, 0))
    assert result == {
        "category": "clear",
        "weather_code": 1,
        "temperature_c": 21.5,
        "precipitation_mm": 0.0,
        "cloud_cover_pct": 20,
    }


def test_撮影時刻がちょうど時でないときは切り上げた時刻の行を使う():
    # 03:00 を1分過ぎただけでも、撮影時刻を含む1時間 (03:00, 04:00] の行（04:00）になる
    result = weather_at_capture(open_meteo_envelope(hourly=HOURLY), at(3, 1))
    assert result == {
        "category": "rain",
        "weather_code": 61,
        "temperature_c": 19.0,
        "precipitation_mm": 1.2,
        "cloud_cover_pct": 90,
    }
    assert weather_at_capture(open_meteo_envelope(hourly=HOURLY), at(3, 59))["weather_code"] == 61


def test_秒がある撮影時刻もちょうど時とはみなさない():
    result = weather_at_capture(
        open_meteo_envelope(hourly=HOURLY), datetime(2026, 10, 9, 3, 0, 1, tzinfo=UTC)
    )
    assert result["weather_code"] == 61


def test_JSTの時刻もUTCに直して行を選ぶ():
    # 12:30 JST は 03:30 UTC なので、04:00 の行になる
    captured_at = datetime(2026, 10, 9, 12, 30, tzinfo=JST)
    assert weather_at_capture(open_meteo_envelope(hourly=HOURLY), captured_at)["weather_code"] == 61


def test_日付をまたぐ時刻でも正しい行を選ぶ():
    hourly = hourly_body(
        [
            ("2026-10-09T23:00", 0, 18.0, 0.0, 0),
            ("2026-10-10T00:00", 3, 17.0, 0.0, 100),
        ]
    )
    envelope = open_meteo_envelope(hourly=hourly)
    # 23:30 UTC は翌日 00:00 の行
    assert weather_at_capture(envelope, at(23, 30))["weather_code"] == 3
    # 同じ時刻を JST で書いたもの（2026-10-10 08:30 +09:00）でも同じ
    assert (
        weather_at_capture(envelope, datetime(2026, 10, 10, 8, 30, tzinfo=JST))["weather_code"] == 3
    )
    # UTC の 23:00 ちょうどは JST では翌日の 08:00 だが、UTC の 23:00 の行を使う
    assert weather_at_capture(envelope, at(23, 0))["weather_code"] == 0
    # 00:00 UTC ちょうど（JST では 09:00）は 00:00 の行
    assert weather_at_capture(envelope, at(0, 0, day=10))["weather_code"] == 3


def test_行がないときはnullになる():
    assert weather_at_capture(open_meteo_envelope(hourly=HOURLY), at(5, 0)) is None


def test_天気コードが取れないときは全体がnullになる():
    hourly = hourly_body([("2026-10-09T03:00", None, 21.5, 0.0, 20)])
    assert weather_at_capture(open_meteo_envelope(hourly=hourly), at(3, 0)) is None


def test_カテゴリが決まらないコードのときは全体がnullになる():
    hourly = hourly_body([("2026-10-09T03:00", 4, 21.5, 0.0, 20)])
    assert weather_at_capture(open_meteo_envelope(hourly=hourly), at(3, 0)) is None


def test_ほかの値が取れないときはその項目だけnullになる():
    hourly = hourly_body([("2026-10-09T03:00", 3, None, None, None)])
    assert weather_at_capture(open_meteo_envelope(hourly=hourly), at(3, 0)) == {
        "category": "cloudy",
        "weather_code": 3,
        "temperature_c": None,
        "precipitation_mm": None,
        "cloud_cover_pct": None,
    }


def test_変数の配列が行より短いときはその項目だけnullになる():
    hourly = hourly_body([("2026-10-09T03:00", 0, 21.5, 0.0, 20)])
    hourly["cloud_cover_jma_seamless"] = []
    result = weather_at_capture(open_meteo_envelope(hourly=hourly), at(3, 0))
    assert result["cloud_cover_pct"] is None
    assert result["temperature_c"] == 21.5


def test_撮影時の天気は封筒が使えないときnullになる():
    assert weather_at_capture(None, at(3, 0)) is None
    assert weather_at_capture({}, at(3, 0)) is None
    assert weather_at_capture({"requests": "x"}, at(3, 0)) is None
    broken = open_meteo_envelope(hourly=HOURLY)
    broken["requests"][0]["body"] = "{壊れた本文"
    assert weather_at_capture(broken, at(3, 0)) is None
    not_dict = open_meteo_envelope(hourly=HOURLY)
    not_dict["requests"][0]["body"] = "[1, 2]"
    assert weather_at_capture(not_dict, at(3, 0)) is None
    no_hourly = open_meteo_envelope()
    no_hourly["requests"][0]["body"] = json.dumps({"minutely_15": {}})
    assert weather_at_capture(no_hourly, at(3, 0)) is None


# ---- answer：アメダス ----


def test_アメダスで6つとも0なら降らなかった():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0)))
    assert answer(None, envelope, T) == {"result": "no_rain", "source": "amedas"}


def test_アメダスで1つでも0より大きければ降った():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0), {at(3, 40): 0.5}))
    assert answer(None, envelope, T) == {"result": "rain", "source": "amedas"}


def test_アメダスはtちょうどのキーを含まない():
    # t が10分の境目のとき、対象は 03:10〜04:00。t（03:00）の降水は数えない
    envelope = simple_amedas(amedas_data(at(3, 0), at(4, 0), {at(3, 0): 3.0}))
    assert answer(None, envelope, T) == {"result": "no_rain", "source": "amedas"}


def test_アメダスはt足す60分ちょうどのキーを含む():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 10), {at(4, 0): 0.5, at(4, 10): 9.0}))
    assert answer(None, envelope, T) == {"result": "rain", "source": "amedas"}
    # 04:10 は t+60分を過ぎているので数えない
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 10), {at(4, 10): 9.0}))
    assert answer(None, envelope, T) == {"result": "no_rain", "source": "amedas"}


def test_アメダスのt足す60分のキーがなければそろわない():
    envelope = simple_amedas(amedas_data(at(3, 10), at(3, 50)))
    assert answer(None, envelope, T) == {"result": "unknown", "source": None}


def test_アメダスのtが10分の境目でないときも範囲は正しい():
    # t = 03:05 の対象は 03:10〜04:00。03:00 と 04:10 は数えない
    t = at(3, 5)
    data = amedas_data(at(3, 0), at(4, 10), {at(3, 0): 3.0, at(4, 10): 9.0})
    assert answer(None, simple_amedas(data), t) == {"result": "no_rain", "source": "amedas"}
    # 04:00 は t+60分（04:05）以内なので数える
    data = amedas_data(at(3, 0), at(4, 10), {at(4, 0): 0.1})
    assert answer(None, simple_amedas(data), t) == {"result": "rain", "source": "amedas"}
    # 03:10 がなければそろわない
    data = amedas_data(at(3, 0), at(4, 10), {at(3, 10): None})
    assert answer(None, simple_amedas(data), t) == {"result": "unknown", "source": None}


def test_アメダスのtが10分の境目に近くても範囲は正しい():
    # t = 03:09:59 の対象は 03:10〜04:00（04:10 は t+60分の 04:09:59 を過ぎる）
    t = datetime(2026, 10, 9, 3, 9, 59, tzinfo=UTC)
    data = amedas_data(at(3, 0), at(4, 10), {at(4, 10): 9.0})
    assert answer(None, simple_amedas(data), t) == {"result": "no_rain", "source": "amedas"}
    # t = 03:10:01 の対象は 03:20〜04:10
    t = datetime(2026, 10, 9, 3, 10, 1, tzinfo=UTC)
    data = amedas_data(at(3, 10), at(4, 10), {at(3, 10): 9.0, at(4, 10): 0.2})
    assert answer(None, simple_amedas(data), t) == {"result": "rain", "source": "amedas"}


def test_アメダスはJSTの日付をまたぐキーも読める():
    # 14:30 UTC は JST で 23:30。対象は JST の 23:40〜翌日 00:30 で、キーの日付が変わる
    t = at(14, 30)
    data = amedas_data(at(14, 40), at(15, 30), {at(15, 20): 0.5})
    assert amedas_key(at(15, 0)) == "20261010000000"
    assert answer(None, simple_amedas(data), t) == {"result": "rain", "source": "amedas"}


def test_アメダスは複数のファイルの本文をまとめて読む():
    # 12:00 JST のファイルと 15:00 JST のファイルに分かれていても、キーで探す
    data = amedas_data(at(3, 10), at(4, 0), {at(3, 50): 0.5})
    first = {k: v for k, v in data.items() if k < amedas_key(at(3, 40))}
    second = {k: v for k, v in data.items() if k >= amedas_key(at(3, 40))}
    envelope = amedas_envelope(
        [("44132", 1.6)],
        [amedas_request("44132", first, hh="09"), amedas_request("44132", second, hh="12")],
    )
    assert answer(None, envelope, T) == {"result": "rain", "source": "amedas"}


def test_アメダスは10kmちょうどの観測点を使う():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0), {at(3, 30): 1.0}), distance=10.0)
    assert answer(None, envelope, T) == {"result": "rain", "source": "amedas"}


def test_アメダスは10_1kmの観測点を使わない():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0), {at(3, 30): 1.0}), distance=10.1)
    assert answer(None, envelope, T) == {"result": "unknown", "source": None}


def test_10kmを超える観測点しかないときはOpen_Meteoに移る():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0)), distance=10.1)
    open_meteo = open_meteo_envelope(quarter_hours(at(3, 15), at(4, 0), 0.5))
    assert answer(open_meteo, envelope, T) == {"result": "rain", "source": "open_meteo"}


def test_アメダスは近い順に見る():
    near = amedas_request("44001", amedas_data(at(3, 10), at(4, 0)))
    far = amedas_request("44002", amedas_data(at(3, 10), at(4, 0), {at(3, 30): 1.0}))
    # 並び順が遠い順でも、近い観測点（44001）で判定する
    envelope = amedas_envelope([("44002", 8.0), ("44001", 2.0)], [far, near])
    assert answer(None, envelope, T) == {"result": "no_rain", "source": "amedas"}


def test_値が1つ欠けたら次の観測点を見る():
    near = amedas_request("44001", amedas_data(at(3, 10), at(4, 0), {at(3, 30): None}))
    far = amedas_request("44002", amedas_data(at(3, 10), at(4, 0), {at(3, 30): 1.0}))
    envelope = amedas_envelope([("44001", 2.0), ("44002", 8.0)], [near, far])
    assert answer(None, envelope, T) == {"result": "rain", "source": "amedas"}


def test_降水量がnullの観測点も次の観測点を見る():
    near = amedas_request("44001", amedas_data(at(3, 10), at(4, 0)))
    body = json.loads(near["body"])
    body[amedas_key(at(3, 30))] = entry(None)
    near["body"] = json.dumps(body)
    far = amedas_request("44002", amedas_data(at(3, 10), at(4, 0)))
    envelope = amedas_envelope([("44001", 2.0), ("44002", 8.0)], [near, far])
    assert answer(None, envelope, T) == {"result": "no_rain", "source": "amedas"}


def test_次の観測点が10kmより遠ければ使わずOpen_Meteoに移る():
    near = amedas_request("44001", amedas_data(at(3, 10), at(4, 0), {at(3, 30): None}))
    far = amedas_request("44002", amedas_data(at(3, 10), at(4, 0)))
    envelope = amedas_envelope([("44001", 2.0), ("44002", 10.1)], [near, far])
    open_meteo = open_meteo_envelope(quarter_hours(at(3, 15), at(4, 0), 0.5))
    assert answer(open_meteo, envelope, T) == {"result": "rain", "source": "open_meteo"}


def test_アメダスがそろわなければOpen_Meteoを使う():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0), {at(3, 30): None}))
    open_meteo = open_meteo_envelope(quarter_hours(at(3, 15), at(4, 0)))
    assert answer(open_meteo, envelope, T) == {"result": "no_rain", "source": "open_meteo"}


def test_どちらもそろわなければ不明になる():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0), {at(3, 30): None}))
    open_meteo = open_meteo_envelope(quarter_hours(at(3, 15), at(3, 45)))
    assert answer(open_meteo, envelope, T) == {"result": "unknown", "source": None}


def test_404のファイルは使わない():
    # 状態が 404 のファイルに本文があっても（本来は空）、読まない
    envelope = amedas_envelope(
        [("44132", 1.6)],
        [amedas_request("44132", amedas_data(at(3, 10), at(4, 0), {at(3, 30): 1.0}), status=404)],
    )
    envelope["requests"][0]["body"] = json.dumps(amedas_data(at(3, 10), at(4, 0)))
    assert answer(None, envelope, T) == {"result": "unknown", "source": None}


def test_ほかの観測点のファイルは使わない():
    envelope = amedas_envelope(
        [("44001", 2.0)], [amedas_request("44002", amedas_data(at(3, 10), at(4, 0)))]
    )
    assert answer(None, envelope, T) == {"result": "unknown", "source": None}


def test_品質の印は見ない():
    data = amedas_data(at(3, 10), at(4, 0))
    data[amedas_key(at(3, 30))] = entry(0.5, quality=5)
    assert answer(None, simple_amedas(data), T) == {"result": "rain", "source": "amedas"}


def test_アメダスの本文が壊れていても例外を投げない():
    envelope = simple_amedas({})
    envelope["requests"][0]["body"] = "{壊れた本文"
    open_meteo = open_meteo_envelope(quarter_hours(at(3, 15), at(4, 0)))
    assert answer(open_meteo, envelope, T) == {"result": "no_rain", "source": "open_meteo"}
    assert answer(None, envelope, T) == {"result": "unknown", "source": None}


@pytest.mark.parametrize(
    "bad_data",
    [
        [1, 2, 3],
        {"20261009123000": "文字列"},
        {"20261009123000": {"temp": [20.0, 0]}},
        {"20261009123000": {"precipitation10m": []}},
        {"20261009123000": {"precipitation10m": "0.0"}},
        {"20261009123000": {"precipitation10m": ["0.0", 0]}},
        {"20261009123000": {"precipitation10m": [True, 0]}},
    ],
)
def test_アメダスの本文の形が違っても例外を投げない(bad_data):
    data = amedas_data(at(3, 10), at(4, 0))
    if isinstance(bad_data, dict):
        data.update(bad_data)
    else:
        data = bad_data
    assert answer(None, simple_amedas(data), T) == {"result": "unknown", "source": None}


def test_アメダスの封筒の形が違っても例外を投げない():
    for envelope in [
        {},
        {"stations": "x", "requests": []},
        {"stations": [None, {"code": 1, "distance_km": 1.0}, {"code": "44132"}], "requests": []},
        {"stations": [{"code": "44132", "distance_km": 1.0}], "requests": "x"},
        {"stations": [{"code": "44132", "distance_km": 1.0}], "requests": [None, {"status": 200}]},
        {
            "stations": [{"code": "44132", "distance_km": 1.0}],
            "requests": [{"url": 1, "status": 200}],
        },
    ]:
        assert answer(None, envelope, T) == {"result": "unknown", "source": None}


# ---- answer：Open-Meteo ----


def test_Open_Meteoで合計が0_1ちょうどなら降った():
    minutely = dict(zip(quarter_hours(at(3, 15), at(4, 0)), [0.1, 0.0, 0.0, 0.0], strict=True))
    assert answer(open_meteo_envelope(minutely), None, T) == {
        "result": "rain",
        "source": "open_meteo",
    }


def test_Open_Meteoで合計が0_09なら降らなかった():
    minutely = dict(zip(quarter_hours(at(3, 15), at(4, 0)), [0.03, 0.03, 0.03, 0.0], strict=True))
    assert answer(open_meteo_envelope(minutely), None, T) == {
        "result": "no_rain",
        "source": "open_meteo",
    }


@pytest.mark.parametrize(
    "values",
    [
        [0.03, 0.07, 0.0, 0.0],
        # このまま足すと 0.09999999999999999 になる組み合わせ
        [0.03, 0.03, 0.03, 0.01],
        [0.01, 0.03, 0.03, 0.03],
        # 0.1 を3つに分けたもの（小数第6位までで足すと 0.1）
        [0.033333, 0.033333, 0.033334, 0.0],
    ],
)
def test_Open_Meteoの合計は浮動小数の誤差があっても0_1なら降った(values):
    minutely = dict(zip(quarter_hours(at(3, 15), at(4, 0)), values, strict=True))
    assert answer(open_meteo_envelope(minutely), None, T) == {
        "result": "rain",
        "source": "open_meteo",
    }


def test_Open_Meteoの合計が0_1に届かない小さな差は降らなかった():
    values = [0.033333, 0.033333, 0.033333, 0.0]  # 合計 0.099999
    minutely = dict(zip(quarter_hours(at(3, 15), at(4, 0)), values, strict=True))
    assert answer(open_meteo_envelope(minutely), None, T) == {
        "result": "no_rain",
        "source": "open_meteo",
    }


def test_Open_Meteoはtちょうどの時刻を含まない():
    minutely = quarter_hours(at(3, 0), at(4, 0))
    minutely[at(3, 0)] = 5.0
    assert answer(open_meteo_envelope(minutely), None, T) == {
        "result": "no_rain",
        "source": "open_meteo",
    }


def test_Open_Meteoはt足す60分ちょうどの時刻を含む():
    minutely = quarter_hours(at(3, 15), at(4, 15))
    minutely[at(4, 0)] = 0.1
    minutely[at(4, 15)] = 5.0
    assert answer(open_meteo_envelope(minutely), None, T) == {
        "result": "rain",
        "source": "open_meteo",
    }
    # 04:15 は t+60分を過ぎているので数えない
    minutely[at(4, 0)] = 0.0
    assert answer(open_meteo_envelope(minutely), None, T) == {
        "result": "no_rain",
        "source": "open_meteo",
    }


def test_Open_Meteoのt足す60分の時刻がなければそろわない():
    minutely = quarter_hours(at(3, 15), at(3, 45), 0.5)
    assert answer(open_meteo_envelope(minutely), None, T) == {"result": "unknown", "source": None}


def test_Open_Meteoのtが15分の境目でないときも範囲は正しい():
    # t = 03:10 の対象は 03:15〜04:00。03:00 と 04:15 は数えない
    t = at(3, 10)
    minutely = quarter_hours(at(3, 0), at(4, 15))
    minutely[at(3, 0)] = 5.0
    minutely[at(4, 15)] = 5.0
    assert answer(open_meteo_envelope(minutely), None, t) == {
        "result": "no_rain",
        "source": "open_meteo",
    }
    minutely[at(4, 0)] = 0.2
    assert answer(open_meteo_envelope(minutely), None, t) == {
        "result": "rain",
        "source": "open_meteo",
    }


def test_Open_Meteoのtが15分ちょうどのときの範囲():
    # t = 03:15 の対象は 03:30〜04:15。03:15 は数えず、04:15 は数える
    t = at(3, 15)
    minutely = quarter_hours(at(3, 15), at(4, 15))
    minutely[at(3, 15)] = 5.0
    assert answer(open_meteo_envelope(minutely), None, t) == {
        "result": "no_rain",
        "source": "open_meteo",
    }
    minutely[at(4, 15)] = 0.1
    assert answer(open_meteo_envelope(minutely), None, t) == {
        "result": "rain",
        "source": "open_meteo",
    }


def test_Open_Meteoで値が1つnullならそろわない():
    minutely = quarter_hours(at(3, 15), at(4, 0), 0.5)
    minutely[at(3, 45)] = None
    assert answer(open_meteo_envelope(minutely), None, T) == {"result": "unknown", "source": None}


def test_Open_Meteoの封筒が使えないときは不明になる():
    assert answer(None, None, T) == {"result": "unknown", "source": None}
    assert answer({}, {}, T) == {"result": "unknown", "source": None}
    broken = open_meteo_envelope(quarter_hours(at(3, 15), at(4, 0)))
    broken["requests"][0]["body"] = "{壊れた本文"
    assert answer(broken, None, T) == {"result": "unknown", "source": None}


def test_Open_Meteoの配列の長さが違うときはそろわない():
    envelope = open_meteo_envelope(quarter_hours(at(3, 15), at(4, 0), 0.5))
    body = json.loads(envelope["requests"][0]["body"])
    body["minutely_15"]["precipitation_jma_seamless"].pop()
    envelope["requests"][0]["body"] = json.dumps(body)
    assert answer(envelope, None, T) == {"result": "unknown", "source": None}


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"minutely_15": []},
        {"minutely_15": {"time": ["2026-10-09T03:15"]}},
        {"minutely_15": {"time": "x", "precipitation_jma_seamless": "y"}},
    ],
)
def test_Open_Meteoの本文の形が違っても例外を投げない(body):
    envelope = open_meteo_envelope()
    envelope["requests"][0]["body"] = json.dumps(body)
    assert answer(envelope, None, T) == {"result": "unknown", "source": None}


def test_アメダスが使えればOpen_Meteoより先に使う():
    envelope = simple_amedas(amedas_data(at(3, 10), at(4, 0)))
    open_meteo = open_meteo_envelope(quarter_hours(at(3, 15), at(4, 0), 0.5))
    assert answer(open_meteo, envelope, T) == {"result": "no_rain", "source": "amedas"}


# ---- is_correct ----


def test_予想と答えが同じなら当たり():
    assert is_correct("rain", {"result": "rain", "source": "amedas"}) is True
    assert is_correct("no_rain", {"result": "no_rain", "source": "open_meteo"}) is True


def test_予想と答えが違えばはずれ():
    assert is_correct("rain", {"result": "no_rain", "source": "amedas"}) is False


def test_予想がないときは当たりかどうかをnullにする():
    assert is_correct(None, {"result": "rain", "source": "amedas"}) is None


def test_答えが不明のときは当たりかどうかをnullにする():
    assert is_correct("rain", {"result": "unknown", "source": None}) is None
    assert is_correct("rain", None) is None
