# sky-server

空の写真と撮影時のメタデータを受け取るアップロード API。M1 としてローカルで動く版で、仕様は `docs/design.md` の 12 節にある。天気データの取得ジョブ（M4 の PR-A）もローカルで動く。GCP 対応は M4 の PR-B で行う。

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

## 天気ジョブ

仕様は `docs/m4-weather.md` にある。

### 流れ

1. `PUT /v1/observations/{id}` が成功すると（新規の 201 も、再送の 200 も）、天気ジョブが2つできて予約される。
   - `forecast`：受け取った時刻に実行する。Open-Meteo を取得する。
   - `label`：撮影の6〜7時間後（`ceil_hour(撮影 + 3時間) + 3時間`。アップロードが遅れたときは受け取った時刻）に実行する。Open-Meteo とアメダスを取得する。
   - 位置のない観測のジョブは `skipped`（`skip_reason: no_location`）になり、予約しない。
   - 予約に失敗すると PUT は 503 を返す。観測は保存済みなので、同じ内容を再送すれば予約がやり直される。
2. 期限の来た予約を `run-due-jobs` が実行する。取得に失敗したら5分、10分、20分、40分、80分と間隔を伸ばして予約し直し、6回目の失敗で `failed` にする。
3. 状態は `SKY_DATA_DIR/jobs/`、予約は `SKY_DATA_DIR/tasks/` にある。

### 期限の来たジョブを実行する

```sh
uv run python -m sky_server.admin run-due-jobs
# 1分ごとに回す
watch -n 60 uv run python -m sky_server.admin run-due-jobs
```

1回の起動では、起動した時点で期限の来ていた予約だけを古い順に1件ずつ実行する。再試行の予約は次の起動で実行される。1件ごとに `job_id`、結果、理由を1行で表示する。例外が出た件があれば終了コードは 1 になり、その予約は残って次の起動でやり直される。

### 内部 API を curl で試す

`POST /internal/tasks/fetch-weather` は、本来 Cloud Tasks が呼ぶ。既定ではすべて 401 になる。ローカルで試すときだけ、認証を外して起動する。

```sh
SKY_TASK_AUTH=none uv run uvicorn sky_server.main:create_app --factory --port 8000

curl -i -X POST http://localhost:8000/internal/tasks/fetch-weather \
  -H "Content-Type: application/json" \
  -d "{\"job_id\": \"${ID}_forecast\"}"
```

期限前のジョブは実行されず、`{"result": "ignored", "reason": "not_due", ...}` が返る。

`SKY_TASK_AUTH` の値は次のとおり。これ以外の値を入れると起動時にエラーになる。

- 未設定：内部 API をすべて 401 にする（既定）。
- `none`：認証を外す。ローカルで試すときだけ使う。`SKY_BACKEND=gcp` と組み合わせると起動時にエラーになる。
- `oidc`：Cloud Tasks の OIDC トークンを検証する（GCP 版）。

### 設定（環境変数）

| 名前 | 既定 | 内容 |
|---|---|---|
| `SKY_DATA_DIR` | `./data` | データの置き場所 |
| `SKY_AMEDAS_ENABLED` | `1` | `0` でアメダスを取得しない（ジョブの中では `disabled` になる） |
| `SKY_AMEDAS_INTERVAL_S` | `1` | アメダスの呼び出しの間隔（秒） |
| `SKY_OPEN_METEO_URL` | `https://api.open-meteo.com/v1/forecast` | Open-Meteo の URL |
| `SKY_TASK_AUTH` | （なし＝すべて拒否） | 内部 API の認証。`none`（ローカル専用）か `oidc`（GCP 版） |
| `SKY_DAILY_UPLOAD_LIMIT` | `100` | 撮影者ごとの1日（UTC）の新規の観測の上限。超えたら 429 |
| `SKY_REQUIRE_CONTENT_LENGTH` | `0` | `1` で Content-Length のないアップロードを 411 にする |

### 生レスポンスの置き場所

取得できたレスポンスは、1回の取得ごとに gzip の JSON（封筒）にまとめて保存する。

- `SKY_DATA_DIR/weather/raw/open_meteo/{observation_id}/{phase}.json.gz`
- `SKY_DATA_DIR/weather/raw/amedas/{observation_id}/{phase}.json.gz`
- `SKY_DATA_DIR/weather/cache/amedas/amedastable.json.gz`（アメダスの観測点の一覧。7日ごとに取り直す）

封筒の形は `docs/m4-weather.md` の 5.2節にある。

### アメダスについて

アメダスのデータは、気象庁のサイトが内部で使っている JSON を読んでいる。公式の API ではないので、予告なく形や場所が変わったり、使えなくなったりすることがある。出典は「気象庁ホームページ（アメダス）」。

## GCP 版

`SKY_BACKEND=gcp` にすると、保存先を Firestore と Cloud Storage、実行の予約を Cloud Tasks に切り替えて動く（既定は `local`）。内部 API の認証は `SKY_TASK_AUTH=oidc` で Cloud Tasks の OIDC トークンを検証する。必要な設定（`SKY_GCP_PROJECT`、`SKY_GCS_BUCKET`、`SKY_TASKS_TARGET_URL`、`SKY_TASKS_SERVICE_ACCOUNT` など）は `docs/m4-weather.md` の 10節にある。

- `run-due-jobs` は GCP 版では使えない（Cloud Tasks が実行する）。
- 撮影者の作成は、手元の PC から `SKY_BACKEND=gcp` を付けて `admin create-user` を実行する。

構築とデプロイの手順は `docs/deploy-gcp.md` を見る。

## テストとチェック

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```
