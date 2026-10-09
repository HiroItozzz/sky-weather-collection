# sky-server

空の写真と撮影時のメタデータを受け取るアップロード API。M1 としてローカルで動く版で、仕様は `docs/design.md` の 12 節にある。天気データの取得と GCP 対応は M4 で行う。

## 起動

```sh
cd server
uv sync
export SKY_DATA_DIR=./data   # 省略すると ./data
uv run uvicorn sky_server.main:create_app --factory --port 8000
```

`SKY_DATA_DIR` の下に、撮影者（`users/`）、観測のメタデータ（`observations/`）、画像（`images/`）が保存される。

## 撮影者の作成と無効化

```sh
uv run python -m sky_server.admin create-user --name alice
uv run python -m sky_server.admin revoke-user --user-id <user_id>
```

`create-user` は `user_id` と招待コードを表示する。招待コードはこのときの1回しか表示されない。サーバーにはそのハッシュだけが保存される。

## curl でアップロードを試す

`TOKEN` に `create-user` で表示された招待コードを入れる。

```sh
TOKEN=<招待コード>
ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
SHA=$(sha256sum photo.jpg | cut -d' ' -f1)
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)

cat > metadata.json <<JSON
{
  "schema_version": 1,
  "observation_id": "$ID",
  "captured_at": "$NOW",
  "tz_offset_min": 540,
  "location": {"lat": 35.68, "lon": 139.76, "accuracy_m": 8.5, "altitude_m": null, "fix_time": "$NOW"},
  "orientation": {"azimuth_deg": 123.4, "pitch_deg": 45.0, "roll_deg": 1.5, "declination_deg": -7.5, "accuracy": "high", "stddev_deg": 0.8},
  "camera": {"focal_length_mm": 5.4, "focal_length_35mm": 24, "exposure_time_s": 0.001, "iso": 50, "f_number": 1.8, "white_balance": null, "image_width": 4000, "image_height": 3000},
  "device": {"platform": "android", "os_version": "15", "model": "Pixel 8", "app_version": "0.1.0"},
  "capture_path": "native",
  "user_guess": null,
  "image_sha256": "$SHA"
}
JSON

curl -i -X PUT "http://localhost:8000/v1/observations/$ID" \
  -H "Authorization: Bearer $TOKEN" \
  -F "metadata=<metadata.json" \
  -F "image=@photo.jpg;type=image/jpeg"

curl -i "http://localhost:8000/v1/observations/$ID" -H "Authorization: Bearer $TOKEN"
```

新規なら 201、同じ内容の再送なら 200 が返る。

## テストとチェック

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```
