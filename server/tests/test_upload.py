import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from conftest import IMAGE, OBSERVATION_ID, make_metadata


def test_新規は201で画像とメタデータが保存される(api, token, data_dir):
    res = api.put(token)
    assert res.status_code == 201
    assert res.json() == {"observation_id": OBSERVATION_ID, "status": "created"}
    record = api.repository.get_observation(OBSERVATION_ID)
    assert record["image_key"] == f"{OBSERVATION_ID}/{hashlib.sha256(IMAGE).hexdigest()}.jpg"
    assert (data_dir / "images" / record["image_key"]).read_bytes() == IMAGE
    assert record["user_id"]
    assert record["received_at"]


def test_再送は200で何も変えない(api, token):
    api.put(token)
    before = api.repository.get_observation(OBSERVATION_ID)
    res = api.put(token)
    assert res.status_code == 200
    assert res.json() == {"observation_id": OBSERVATION_ID, "status": "exists"}
    assert api.repository.get_observation(OBSERVATION_ID) == before


def test_IDの大文字小文字は区別せず小文字で扱う(api, token):
    upper = OBSERVATION_ID.upper()
    res = api.put(token, make_metadata(observation_id=upper), observation_id=upper)
    assert res.status_code == 201
    assert res.json()["observation_id"] == OBSERVATION_ID
    assert api.repository.get_observation(OBSERVATION_ID) is not None


def test_同じIDで撮影者が違えば409(api, token):
    api.put(token)
    _, other_token = api.new_user("other")
    assert api.put(other_token).status_code == 409


def test_同じIDで画像が違えば409(api, token):
    api.put(token)
    other = IMAGE + b"x"
    assert api.put(token, image=other).status_code == 409
    # 元の画像は上書きされない
    assert api.repository.get_observation(OBSERVATION_ID)["image_sha256"] == (
        hashlib.sha256(IMAGE).hexdigest()
    )


def test_認証ヘッダーがなければ401(api):
    assert api.put(None).status_code == 401


def test_コードが違えば401(api, token):
    assert api.put("wrong-token").status_code == 401


def test_無効にされたコードは401(api):
    from sky_server.admin import revoke_user

    user_id, token = api.new_user("revoked")
    assert api.put(token).status_code == 201
    revoke_user(api.repository, user_id)
    assert api.put(token).status_code == 401


def test_画像が10MBを超えたら413(api, token):
    big = b"\xff" * (10 * 1024 * 1024 + 1)
    assert api.put(token, image=big).status_code == 413


def test_画像がちょうど10MBなら受け付ける(api, token):
    exact = IMAGE + b"\xff" * (10 * 1024 * 1024 - len(IMAGE))
    assert api.put(token, image=exact).status_code == 201


def test_ハッシュが違えば422(api, token):
    meta = make_metadata()
    meta["image_sha256"] = hashlib.sha256(b"other").hexdigest()
    assert api.put(token, meta).status_code == 422


def test_パスとmetadataのIDが違えば422(api, token):
    meta = make_metadata(observation_id="00000000-0000-4000-8000-000000000000")
    assert api.put(token, meta).status_code == 422


def test_IDがUUIDの形式でなければ422(api, token):
    res = api.put(token, make_metadata(observation_id="not-a-uuid"), observation_id="not-a-uuid")
    assert res.status_code == 422


def test_知らない項目があれば422(api, token):
    meta = make_metadata()
    meta["lcoation"] = {}
    assert api.put(token, meta).status_code == 422


def test_入れ子の中の知らない項目も422(api, token):
    meta = make_metadata()
    meta["camera"]["isoo"] = 100
    assert api.put(token, meta).status_code == 422


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("location", "lat"), 90.1),
        (("location", "lat"), -90.1),
        (("location", "lon"), 180.1),
        (("location", "accuracy_m"), -1),
        (("orientation", "azimuth_deg"), 360),
        (("orientation", "azimuth_deg"), -0.1),
        (("orientation", "pitch_deg"), 91),
        (("orientation", "roll_deg"), -181),
        (("orientation", "accuracy"), "bad"),
        (("device", "platform"), "windows"),
        (("capture_path",), "other"),
        (("user_guess",), "maybe"),
        (("schema_version",), 2),
    ],
)
def test_範囲外や不正な値は422(api, token, path, value):
    meta = make_metadata()
    target = meta
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert api.put(token, meta).status_code == 422


def test_撮影時刻にタイムゾーンがなければ422(api, token):
    meta = make_metadata()
    meta["captured_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
    assert api.put(token, meta).status_code == 422


def test_撮影時刻が10分以上未来なら422(api, token):
    meta = make_metadata()
    meta["captured_at"] = (datetime.now(UTC) + timedelta(minutes=11)).isoformat()
    assert api.put(token, meta).status_code == 422


def test_撮影時刻が少し未来なら受け付ける(api, token):
    meta = make_metadata()
    meta["captured_at"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    assert api.put(token, meta).status_code == 201


def test_nullを許す項目はnullで受け付ける(api, token):
    meta = make_metadata()
    meta["location"] = None
    meta["orientation"] = None
    meta["camera"] = dict.fromkeys(meta["camera"])
    meta["user_guess"] = "rain"
    assert api.put(token, meta).status_code == 201


def test_画像のcontent_typeがjpegでなければ422(api, token):
    assert api.put(token, content_type="image/png").status_code == 422


def test_画像の先頭がJPEGの印でなければ422(api, token):
    png = b"\x89PNG\r\n\x1a\nfake"
    assert api.put(token, image=png).status_code == 422
    assert api.repository.get_observation(OBSERVATION_ID) is None


def test_画像の先頭が途中まで合っていても422(api, token):
    assert api.put(token, image=b"\xff\xd8\x00rest").status_code == 422
