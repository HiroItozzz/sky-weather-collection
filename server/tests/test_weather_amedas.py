import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import SteppingClock, fixed_clock

from sky_server.storage import LocalBlobStore
from sky_server.weather.amedas import (
    TABLE_KEY,
    dms_to_degrees,
    fetch_amedas,
    file_slots,
    load_station_table,
    select_stations,
)
from sky_server.weather.common import PermanentError, RetryableError

NOW = datetime(2026, 10, 9, 6, 30, 5, tzinfo=UTC)
CLOCK = fixed_clock(NOW)
# 撮影時刻 03:00 UTC = 12:00 JST。範囲は 11:00〜15:00 JST なので 09、12、15 の3ファイル
CAPTURED_AT = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


# 以下の観測点の一覧と10分値は実データではなく、仕様の形に合わせて手で作ったもの
def station(lat, lon, elems="11112010", name="地点"):
    return {
        "type": "A",
        "elems": elems,
        "lat": [int(lat), (lat - int(lat)) * 60],
        "lon": [int(lon), (lon - int(lon)) * 60],
        "alt": 10,
        "kjName": name,
    }


TABLE = {
    "10001": station(35.70, 139.75, name="近い"),
    "10002": station(35.60, 139.70, name="二番目"),
    "10003": station(35.90, 139.90, name="三番目"),
    "10004": station(36.50, 140.50, name="遠い"),
    "10005": station(35.681, 139.761, elems="10000000", name="雨を測らない"),
}
POINT = json.dumps({"20261009120000": {"precipitation10m": [0.0, 0]}})


class Server:
    """JMA の代わりに応答し、呼ばれた URL を記録する。"""

    def __init__(self, table=TABLE, not_found=(), fail_status=None):
        self.table = table
        self.not_found = not_found
        self.fail_status = fail_status
        self.urls = []

    def __call__(self, request):
        url = str(request.url)
        self.urls.append(url)
        if url.endswith("amedastable.json"):
            return httpx.Response(200, text=json.dumps(self.table))
        if self.fail_status:
            return httpx.Response(self.fail_status)
        if any(part in url for part in self.not_found):
            return httpx.Response(404, text="")
        return httpx.Response(200, text=POINT)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))

    @property
    def table_calls(self):
        return [u for u in self.urls if u.endswith("amedastable.json")]

    @property
    def point_calls(self):
        return [u for u in self.urls if not u.endswith("amedastable.json")]


def fetch(server, store, sleeps=None, captured_at=CAPTURED_AT, clock=CLOCK, phase="label"):
    return fetch_amedas(
        server.client(),
        store,
        observation_id="obs-1",
        phase=phase,
        captured_at=captured_at,
        lat=35.68,
        lon=139.76,
        clock=clock,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        interval_s=1.0,
    )


def test_度分を度に直す():
    assert dms_to_degrees([35, 41.5]) == pytest.approx(35.691666, abs=1e-5)


def test_最寄り3地点を近い順に選ぶ():
    stations = select_stations(TABLE, 35.68, 139.76)
    assert [s["code"] for s in stations] == ["10001", "10002", "10003"]
    assert stations[0]["name"] == "近い"
    assert stations[0]["lat"] == pytest.approx(35.70)
    assert stations[0]["distance_km"] == pytest.approx(2.4, abs=0.05)


def test_elemsの2文字目が1でない地点は選ばない():
    # 10005 は撮影地点のすぐそばだが、降水量を観測していない
    codes = [s["code"] for s in select_stations(TABLE, 35.681, 139.761)]
    assert "10005" not in codes


def test_elemsで絞れないときは全地点から選ぶ():
    table = {code: {**s, "elems": "00000000"} for code, s in TABLE.items()}
    codes = [s["code"] for s in select_stations(table, 35.681, 139.761)]
    assert codes[0] == "10005"
    assert len(codes) == 3


def test_地点が3つに満たないときはあるだけ返す():
    assert len(select_stations({"1": station(35.0, 139.0)}, 35.0, 139.0)) == 1


def test_2ファイルになる範囲():
    # 13:00 JST の撮影。範囲は 12:00〜16:00 JST で、12 と 15
    captured_at = datetime(2026, 10, 9, 4, 0, tzinfo=UTC)
    assert file_slots(captured_at) == [("20261009", "12"), ("20261009", "15")]


def test_3ファイルになる範囲():
    # 12:00 JST の撮影。範囲は 11:00〜15:00 JST で、始まりは 09、終わりはちょうど 15
    assert file_slots(CAPTURED_AT) == [
        ("20261009", "09"),
        ("20261009", "12"),
        ("20261009", "15"),
    ]


def test_範囲の始まりがちょうど境目なら前のファイルは取らない():
    # 13:00 JST の撮影。始まりの 12:00 JST は 12 のファイルの先頭
    captured_at = datetime(2026, 10, 9, 4, 0, tzinfo=UTC)
    assert ("20261009", "09") not in file_slots(captured_at)


def test_日付をまたぐときはJSTの日付でファイルが並ぶ():
    # 23:30 JST の撮影。範囲は 22:30〜翌 02:30 JST
    captured_at = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
    assert file_slots(captured_at) == [("20261009", "21"), ("20261010", "00")]


def test_日付をまたいで3ファイルになる範囲():
    # 21:30 JST の撮影。範囲は 20:30〜翌 00:30 JST
    captured_at = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
    assert file_slots(captured_at) == [
        ("20261009", "18"),
        ("20261009", "21"),
        ("20261010", "00"),
    ]


def test_UTCの日付とJSTの日付がずれる時刻():
    # 16:00 UTC（10/8）は 01:00 JST（10/9）。範囲は 00:00〜04:00 JST
    captured_at = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
    assert file_slots(captured_at) == [("20261009", "00"), ("20261009", "03")]


def test_取得すると地点ごとにファイルを取り封筒に残す(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server()
    sleeps = []
    key = fetch(server, store, sleeps)

    assert key == "raw/amedas/obs-1/label.json.gz"
    assert len(server.point_calls) == 9
    assert server.point_calls[0].endswith("/point/10001/20261009_09.json")
    assert server.point_calls[1].endswith("/point/10001/20261009_12.json")
    assert len(sleeps) == 8  # 呼び出しの間にだけ入る
    assert set(sleeps) == {1.0}

    envelope = json.loads(gzip.decompress(store.get(key)))
    assert envelope["provider"] == "amedas"
    assert envelope["phase"] == "label"
    assert envelope["observation_id"] == "obs-1"
    assert envelope["captured_at"] == "2026-10-09T03:00:00+00:00"
    assert envelope["query_location"] == {"lat": "35.68", "lon": "139.76"}
    assert envelope["attribution"] == "出典：気象庁ホームページ（アメダス）"
    assert envelope["license"] == "政府標準利用規約（第2.0版）"
    assert [s["code"] for s in envelope["stations"]] == ["10001", "10002", "10003"]
    assert set(envelope["stations"][0]) == {"code", "name", "lat", "lon", "distance_km"}
    assert len(envelope["requests"]) == 9
    first = envelope["requests"][0]
    assert first["params"] == {}
    assert first["status"] == 200
    assert first["body"] == POINT


def test_404は失敗にせず状態コードを封筒に残す(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server(not_found=("/10002/20261009_12",))
    key = fetch(server, store)

    envelope = json.loads(gzip.decompress(store.get(key)))
    statuses = [r["status"] for r in envelope["requests"]]
    assert statuses.count(404) == 1
    assert statuses.count(200) == 8
    assert envelope["requests"][4]["url"].endswith("/point/10002/20261009_12.json")
    assert envelope["requests"][4]["status"] == 404


@pytest.mark.parametrize(("status", "error"), [(500, RetryableError), (403, PermanentError)])
def test_404以外の失敗は例外になり何も保存しない(tmp_path, status, error):
    store = LocalBlobStore(tmp_path, "weather")
    with pytest.raises(error):
        fetch(Server(fail_status=status), store)
    assert store.get("raw/amedas/obs-1/label.json.gz") is None


def test_観測点の一覧はキャッシュされ再利用される(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server()
    fetch(server, store)
    fetch(server, store, clock=fixed_clock(NOW + timedelta(days=6, hours=23)))
    assert len(server.table_calls) == 1

    cached = json.loads(gzip.decompress(store.get(TABLE_KEY)))
    assert cached["provider"] == "amedas"
    assert cached["phase"] == "station_table"
    assert cached["observation_id"] is None
    assert cached["captured_at"] is None
    assert cached["fetched_at"] == "2026-10-09T06:30:05+00:00"
    assert json.loads(cached["requests"][0]["body"]) == TABLE


def test_観測点の一覧は7日たつと取り直す(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server()
    fetch(server, store)
    fetch(server, store, clock=fixed_clock(NOW + timedelta(days=7)))
    assert len(server.table_calls) == 2
    cached = json.loads(gzip.decompress(store.get(TABLE_KEY)))
    assert cached["fetched_at"] == "2026-10-16T06:30:05+00:00"


def test_壊れたキャッシュは取り直す(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    store.put(TABLE_KEY, b"not gzip")
    server = Server()
    assert load_station_table(server.client(), store, CLOCK) == TABLE
    assert len(server.table_calls) == 1


@pytest.mark.parametrize("status", [503, 403])
def test_古い一覧の取り直しが失敗したら古い一覧を使う(tmp_path, status):
    # 403 は再試行できない失敗だが、古い一覧があれば使う
    store = LocalBlobStore(tmp_path, "weather")
    load_station_table(Server().client(), store, CLOCK)

    def down(request):
        return httpx.Response(status)

    client = httpx.Client(transport=httpx.MockTransport(down))
    assert load_station_table(client, store, fixed_clock(NOW + timedelta(days=8))) == TABLE


def test_一覧がなく取得が再試行できない失敗なら例外になる(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(PermanentError):
        load_station_table(client, store, CLOCK)


def test_一覧がなく取得が一時的に失敗したら例外になる(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(RetryableError):
        load_station_table(client, store, CLOCK)


def test_古い一覧の取り直しが403でも取得は古い一覧で続く(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    fetch(Server(), store)
    server = Server()
    original = server.__call__

    def handler(request):
        if str(request.url).endswith("amedastable.json"):
            server.urls.append(str(request.url))
            return httpx.Response(403)
        return original(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    key = fetch_amedas(
        client,
        store,
        observation_id="obs-1",
        phase="label",
        captured_at=CAPTURED_AT,
        lat=35.68,
        lon=139.76,
        clock=fixed_clock(NOW + timedelta(days=8)),
        sleep=lambda s: None,
        interval_s=0,
    )
    assert len(server.table_calls) == 1
    envelope = json.loads(gzip.decompress(store.get(key)))
    assert [s["code"] for s in envelope["stations"]] == ["10001", "10002", "10003"]


def test_観測点のファイルがすべて404ならRetryableErrorで保存しない(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server(not_found=("/10002/",))
    with pytest.raises(RetryableError, match="10002"):
        fetch(server, store)
    assert store.get("raw/amedas/obs-1/label.json.gz") is None


def test_観測点のファイルが1つでも200なら成功する(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server(not_found=("/10002/20261009_09", "/10002/20261009_12"))
    key = fetch(server, store)
    envelope = json.loads(gzip.decompress(store.get(key)))
    statuses = [r["status"] for r in envelope["requests"]]
    assert statuses.count(404) == 2
    assert statuses.count(200) == 7


def test_labelは範囲の終わりより前なら何も呼ばずRetryableError(tmp_path):
    # 撮影 03:00 UTC の範囲の終わりは 06:00 UTC（撮影の3時間後）
    store = LocalBlobStore(tmp_path, "weather")
    server = Server()
    just_before = datetime(2026, 10, 9, 6, 0, tzinfo=UTC) - timedelta(seconds=1)
    with pytest.raises(RetryableError):
        fetch(server, store, clock=fixed_clock(just_before))
    assert server.urls == []
    assert store.get("raw/amedas/obs-1/label.json.gz") is None


def test_labelは範囲の終わりちょうどなら取得する(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server()
    key = fetch(server, store, clock=fixed_clock(datetime(2026, 10, 9, 6, 0, tzinfo=UTC)))
    assert len(server.point_calls) == 9
    assert store.get(key) is not None


def test_forecastは範囲の終わりが未来でも取得する(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    server = Server()
    key = fetch(server, store, phase="forecast", clock=fixed_clock(CAPTURED_AT))
    assert len(server.point_calls) == 9
    assert key == "raw/amedas/obs-1/forecast.json.gz"


def test_requested_atは呼び出しごとにclockの値になる(tmp_path):
    store = LocalBlobStore(tmp_path, "weather")
    clock = SteppingClock(NOW, timedelta(seconds=1))
    key = fetch(Server(), store, clock=clock)
    envelope = json.loads(gzip.decompress(store.get(key)))
    times = [r["requested_at"] for r in envelope["requests"]]
    assert len(times) == 9
    assert times == sorted(set(times))  # 1件ごとに違う値で、古い順に並ぶ
    assert envelope["fetched_at"] < times[0]
