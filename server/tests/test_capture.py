from datetime import datetime, timedelta, timezone

import pytest
from conftest import OBSERVATION_ID, make_capture, make_metadata


def put_with_capture(api, token, capture):
    meta = make_metadata()
    meta["capture"] = capture
    return api.put(token, meta)


def test_captureがなくても受け付ける(api, token):
    meta = make_metadata()
    assert "capture" not in meta
    assert api.put(token, meta).status_code == 201
    assert api.repository.get_observation(OBSERVATION_ID)["capture"] is None


def test_captureがnullでも受け付ける(api, token):
    assert put_with_capture(api, token, None).status_code == 201
    assert api.repository.get_observation(OBSERVATION_ID)["capture"] is None


def test_正しいcaptureは受け付けて保存される(api, token):
    capture = make_capture()
    assert put_with_capture(api, token, capture).status_code == 201
    saved = api.repository.get_observation(OBSERVATION_ID)["capture"]
    # 時刻は UTC の文字列に直されて保存される
    assert datetime.fromisoformat(saved["pressed_at"]) == datetime.fromisoformat(
        capture["pressed_at"]
    )
    assert datetime.fromisoformat(saved["completed_at"]) == datetime.fromisoformat(
        capture["completed_at"]
    )
    for key in capture.keys() - {"pressed_at", "completed_at"}:
        assert saved[key] == capture[key]


def test_nullを許す項目はnullで受け付ける(api, token):
    capture = make_capture()
    for key in [
        "motion_deg",
        "sensor_clock_offset_ms",
        "exif_datetime_original",
        "exif_subsec_time_original",
        "orientation_trace",
    ]:
        capture[key] = None
    assert put_with_capture(api, token, capture).status_code == 201


def test_キーが足りないcaptureは422(api, token):
    capture = make_capture()
    del capture["motion_deg"]
    assert put_with_capture(api, token, capture).status_code == 422


def test_captureの知らない項目は422(api, token):
    capture = make_capture()
    capture["motion_degg"] = 1.0
    assert put_with_capture(api, token, capture).status_code == 422


def test_orientation_traceの知らない項目は422(api, token):
    capture = make_capture()
    capture["orientation_trace"]["extra"] = 1
    assert put_with_capture(api, token, capture).status_code == 422


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("duration_ms", -1),
        ("duration_ms", 600001),
        ("duration_ms", 1.5),
        ("motion_deg", -0.1),
        ("motion_deg", 180.1),
        ("sensor_clock_offset_ms", "abc"),
        ("exif_datetime_original", "x" * 65),
        ("exif_datetime_original", 20261010),
        ("exif_subsec_time_original", "x" * 65),
        ("pressed_at", "2026-10-10T01:23:45.678"),
        ("completed_at", "2026-10-10T01:23:45.678"),
        ("pressed_at", "not-a-time"),
    ],
)
def test_captureの範囲外や不正な値は422(api, token, key, value):
    capture = make_capture()
    capture[key] = value
    assert put_with_capture(api, token, capture).status_code == 422


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("duration_ms", 0),
        ("duration_ms", 600000),
        ("motion_deg", 0),
        ("motion_deg", 180),
        ("exif_datetime_original", "x" * 64),
        ("exif_subsec_time_original", "x" * 64),
    ],
)
def test_captureの境界の値は受け付ける(api, token, key, value):
    capture = make_capture()
    capture[key] = value
    assert put_with_capture(api, token, capture).status_code == 201


def test_completed_atがpressed_atより前なら422(api, token):
    capture = make_capture()
    pressed_at = datetime.fromisoformat(capture["pressed_at"])
    capture["completed_at"] = (pressed_at - timedelta(milliseconds=1)).isoformat()
    assert put_with_capture(api, token, capture).status_code == 422


def test_completed_atがpressed_atと同じなら受け付ける(api, token):
    capture = make_capture()
    capture["completed_at"] = capture["pressed_at"]
    capture["duration_ms"] = 0
    assert put_with_capture(api, token, capture).status_code == 201


def test_タイムゾーンが違っても時刻の前後で比べる(api, token):
    capture = make_capture()
    pressed_at = datetime.fromisoformat(capture["pressed_at"])
    # 同じ時刻を +09:00 で書く
    capture["pressed_at"] = pressed_at.astimezone(timezone(timedelta(hours=9))).isoformat()
    capture["completed_at"] = (pressed_at + timedelta(seconds=1)).isoformat()
    assert put_with_capture(api, token, capture).status_code == 201


TRACE_KEYS = ["t_sensor_ms", "alpha", "beta", "gamma"]


def trace_of_length(n: int) -> dict:
    trace = make_capture()["orientation_trace"]
    for key in TRACE_KEYS:
        trace[key] = [float(i) for i in range(n)]
    return trace


def test_配列が空でも受け付ける(api, token):
    capture = make_capture()
    capture["orientation_trace"] = trace_of_length(0)
    assert put_with_capture(api, token, capture).status_code == 201


def test_配列は1000件まで受け付けて1001件は422(api, token):
    capture = make_capture()
    capture["orientation_trace"] = trace_of_length(1000)
    assert put_with_capture(api, token, capture).status_code == 201
    capture["orientation_trace"] = trace_of_length(1001)
    assert put_with_capture(api, token, capture).status_code == 422


@pytest.mark.parametrize("key", TRACE_KEYS)
def test_配列の長さがそろっていなければ422(api, token, key):
    capture = make_capture()
    capture["orientation_trace"][key].append(0.0)
    assert put_with_capture(api, token, capture).status_code == 422


@pytest.mark.parametrize("key", TRACE_KEYS)
def test_配列の要素が数でなければ422(api, token, key):
    capture = make_capture()
    capture["orientation_trace"][key][0] = "a"
    assert put_with_capture(api, token, capture).status_code == 422


@pytest.mark.parametrize("key", TRACE_KEYS)
def test_配列の項目がなければ422(api, token, key):
    capture = make_capture()
    del capture["orientation_trace"][key]
    assert put_with_capture(api, token, capture).status_code == 422


def test_配列の中に配列があれば422(api, token):
    capture = make_capture()
    capture["orientation_trace"]["alpha"][0] = [0.1]
    assert put_with_capture(api, token, capture).status_code == 422


def test_以前の形のsamplesは422(api, token):
    capture = make_capture()
    capture["orientation_trace"] = {"source": "x", "samples": [[1.0, 2.0, 3.0, 4.0]]}
    assert put_with_capture(api, token, capture).status_code == 422


@pytest.mark.parametrize("source", ["", "x" * 201])
def test_sourceが空か201文字以上なら422(api, token, source):
    capture = make_capture()
    capture["orientation_trace"]["source"] = source
    assert put_with_capture(api, token, capture).status_code == 422


def test_sourceは200文字まで受け付ける(api, token):
    capture = make_capture()
    capture["orientation_trace"]["source"] = "x" * 200
    assert put_with_capture(api, token, capture).status_code == 201


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_captureの有限でない数は500でなく422(api, token, value):
    # JSON の標準にはないが、Python の json.dumps は NaN と Infinity を書く
    for index in range(3):
        capture = make_capture()
        if index == 0:
            capture["motion_deg"] = value
        elif index == 1:
            capture["sensor_clock_offset_ms"] = value
        else:
            capture["orientation_trace"]["alpha"][0] = value
        res = put_with_capture(api, token, capture)
        assert res.status_code == 422
        assert "input" not in res.text


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_既存の項目の有限でない数も500でなく422(api, token, value):
    meta = make_metadata()
    meta["location"]["lat"] = value
    meta["orientation"]["stddev_deg"] = value
    assert api.put(token, meta).status_code == 422


def test_422の応答に送られた値を含めない(api, token):
    meta = make_metadata()
    meta["orientation"]["accuracy"] = "secret-value"
    res = api.put(token, meta)
    assert res.status_code == 422
    assert "secret-value" not in res.text
