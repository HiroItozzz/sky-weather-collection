# M4 の仕様：天気データの取得ジョブ

`docs/design.md` の 5節（天気データ）と 7節（`POST /internal/tasks/fetch-weather`）を、サーバーに実装するための詳細仕様。M4 は2つの PR に分ける。

- PR-A：ローカルで動く版（この文書の 1〜9節）
- PR-B：GCP 版（Firestore、Cloud Storage、Cloud Tasks、Cloud Run）（10節）

「不確か」と書いた点は、実際の API で確認できていない。この文書を書いた環境からは `api.open-meteo.com` と `www.jma.go.jp` に接続できなかったため、公開されているドキュメントと過去の知識をもとに決めている。最初の実データで確認する（9節）。

## 1. 全体の流れ

```
PUT /v1/observations/{id}（新規、または再送）
  └─ 天気ジョブを2つ用意する（なければ作る。予約していなければ予約する）
       ├─ forecast：アップロード直後に実行（特徴量）
       └─ label   ：撮影の6〜7時間後に実行（ラベル）

TaskScheduler（予約）
  ├─ ローカル版：data/tasks/ に予約を書き、管理コマンド run-due-jobs が期限の来たものを実行
  └─ Cloud Tasks 版（PR-B）：期限に POST /internal/tasks/fetch-weather を呼ぶ

JobRunner.run(job_id)
  ├─ 観測を読み、プロバイダーごとに取得する（Open-Meteo、アメダス）
  ├─ 生レスポンスを gzip にして BlobStore に保存する
  └─ ジョブの状態を更新する。失敗したら間隔を伸ばして予約し直す
```

## 2. ジョブ

### 2.1 種類

| phase | 実行する時刻 `run_at` | プロバイダー |
|---|---|---|
| `forecast` | 観測を受け取った時刻（`received_at`） | `open_meteo` |
| `label` | `max(ceil_hour(captured_at + 3時間) + 3時間, received_at)` | `open_meteo`、`amedas` |

- `label` は、取得する範囲の終わり（Open-Meteo の1時間値の `ceil_hour(t+3h)`）を過ぎてから、さらに3時間待って実行する（design.md 5節）。範囲の終わりより前に取ると最後の行が予報値になること、Open-Meteo の過去の値はその時点で最新のモデル実行から来るので、待つほど実況に近くなることが理由。アメダスの10分値の公開の遅れ（数十分）もこの中に収まる。
- アップロードが遅れてその時刻を過ぎていた場合、`label` もすぐに実行する。
- `label` の取得では「取得する範囲の終わり ≤ 取得の直前の時刻」を確かめる（Open-Meteo は `end_hour` と `end_minutely_15`、アメダスは t+3h）。満たさなければ、再試行できる失敗として扱う。`forecast` は予報を取るのが目的で、範囲の終わりが未来になるのは当然なので、この確認をしない。
- アップロードが撮影から大きく遅れた場合、`forecast` で取れる値は「撮影時点でわかっていた予報」ではなくなる。取得はするが、生レスポンスの封筒（5節）に `captured_at` と `fetched_at` を残し、学習時にずれの大きいものを除けるようにする。

### 2.2 ジョブの状態の形

1ジョブ1件で保存する。`job_id` は `{observation_id}_{phase}` で、同じ観測に同じ phase のジョブは1つしかできない。

```json
{
  "job_id": "123e4567-e89b-42d3-a456-426614174000_label",
  "observation_id": "123e4567-e89b-42d3-a456-426614174000",
  "phase": "label",
  "status": "pending",
  "created_at": "2026-10-09T03:00:00+00:00",
  "run_at": "2026-10-09T09:00:00+00:00",
  "next_attempt_at": "2026-10-09T09:00:00+00:00",
  "enqueued": true,
  "attempts": 0,
  "max_attempts": 6,
  "last_attempt_at": null,
  "last_error": null,
  "completed_at": null,
  "skip_reason": null,
  "providers": {
    "open_meteo": {"status": "pending", "blob_key": null, "completed_at": null, "error": null},
    "amedas": {"status": "pending", "blob_key": null, "completed_at": null, "error": null}
  }
}
```

- `status`：`pending`（未完了）/ `done`（すべてのプロバイダーが `done` か `disabled`）/ `failed`（諦めた。1つでも `failed` のプロバイダーがある）/ `skipped`（取得しない。理由は `skip_reason`）。
- `providers.*.status`：`pending` / `done` / `failed` / `disabled`（設定で無効にされていた）。
- `enqueued`：TaskScheduler への予約が成功したか。予約に失敗したジョブを見つけて予約し直すために使う（3.3節）。
- `attempts`：実行した回数。`last_error` は最後の失敗の内容（例外の種類とメッセージ、HTTP の状態コード。長さは 1000 文字まで）。
- 時刻はすべて UTC の ISO 8601。

### 2.3 位置がない観測

`location` が null の観測は天気を取れない。ジョブは作るが、`status: "skipped"`、`skip_reason: "no_location"` にして予約しない。後から「なぜ天気がないか」がわかるようにするため。

## 3. 予約と実行

### 3.1 インターフェース

- `JobRepository`：ジョブの状態の保存先。
  - `add_job(job) -> bool`：同じ `job_id` がすでにあれば保存せず False。
  - `get_job(job_id) -> WeatherJob | None`
  - `update_job(job) -> None`
  - ローカル版は `SKY_DATA_DIR/jobs/{job_id}.json`（1件1ファイル。`add_job` は排他的に作成する）。
- `TaskScheduler`：「この時刻にこのジョブを実行してほしい」という予約。
  - `schedule(job_id, run_at) -> None`：失敗したら例外を投げる。
  - ローカル版は `SKY_DATA_DIR/tasks/{job_id}.json` に `{"job_id": ..., "run_at": ...}` を書く（同じジョブの予約は上書きする）。
  - ローカル版には `due(now) -> list[tuple[str, datetime]]`（期限が来た `(job_id, run_at)` を `run_at` の古い順に返す）と `remove(job_id, run_at)`（予約の `run_at` が一致するときだけ消す）も持たせる。これらは管理コマンドだけが使う。

### 3.2 PUT のときの予約（`ensure_weather_jobs`）

PUT が 201（新規）と 200（再送）のどちらのときも、次の手順を実行する。何度呼んでも結果が同じになるように作る。

1. 2つの phase それぞれについて、ジョブがなければ作る（`add_job`）。位置がなければ `skipped` で作る。
2. `status` が `pending` で `enqueued` が false なら、`schedule(job_id, next_attempt_at)` を呼び、成功したら `enqueued` を true にして保存する。
3. 予約に失敗したら（例外）、PUT は 503 を返す。観測そのものは保存済みなので、アプリが再送すると 200 の経路で手順 2 がやり直される。

再送のときにも手順を実行するのは、この 503 から立ち直るため。

### 3.3 実行（`JobRunner.run(job_id, now)`）

戻り値は結果を表す文字列（`ran` / `ignored`）と理由。

1. ジョブがなければ `ignored`（`job_not_found`）。
2. `status` が `pending` でなければ `ignored`（`already_finished`）。同じ予約が2回届いても、2回取得しないため。
3. `now < next_attempt_at` なら実行しない。このとき `enqueued` が false なら予約し直す。`ignored`（`not_due`）。
4. 観測を読む。観測がなければ `status: "failed"`、`last_error: "observation_not_found"` で完了にする。位置がなければ `skipped`（`no_location`）で完了にする。どちらも手順 7 と同じく `completed_at` を入れ、`next_attempt_at` を null にする（2.3節で作った時点で `skipped` のジョブも同じ）。
5. `attempts` を1増やし、`last_attempt_at` を `now` にする。
6. `status` が `pending` のプロバイダーを順に取得する（`open_meteo` → `amedas`）。
   - アメダスが設定で無効なら `disabled` にする。
   - 成功：生レスポンスを保存して `done`、`blob_key` と `completed_at` を入れる。
   - 再試行できる失敗（4.1節）：そのプロバイダーは `pending` のまま。`error` と、ジョブの `last_error` に内容を入れる。次のプロバイダーには進む。
   - 再試行できない失敗：そのプロバイダーを `failed` にする。
   - それ以外の想定外の例外（応答の形が想定と違うなど）は、再試行できる失敗と同じに扱う。待ち時間と `max_attempts` を効かせて、同じ失敗を際限なく繰り返さないようにするため。
7. すべてのプロバイダーが `pending` でなくなったら、ジョブを完了にする（`failed` が1つでもあれば `failed`、なければ `done`）。`completed_at` を入れ、`next_attempt_at` を null にする。
8. まだ `pending` のプロバイダーがあり、`attempts >= max_attempts` なら、残りを `failed`（`error: "max_attempts"` を末尾に足す）にしてジョブを `failed` にする。
9. それ以外は `next_attempt_at = now + 待ち時間(attempts)`、`enqueued = false` で保存してから、`schedule` を呼び、成功したら `enqueued = true` で保存する。予約に失敗したら例外をそのまま上に投げる（HTTP なら 500 になり、Cloud Tasks が同じ配達をやり直す。そのとき手順 3 で予約し直される）。

取得に成功したプロバイダーは `done` になるので、再試行で同じデータを取り直すことはない。

同じジョブが同時に2回実行されることは、ローカル版では考えない（管理コマンドを1つだけ動かす前提）。GCP 版でも Cloud Tasks の重複配達はまれで、起きても同じキーに同じ内容を上書きするだけなので許容する。

### 3.4 再試行の方針

- 待ち時間は `5分 × 2^(attempts-1)`、上限 2時間。`max_attempts` は 6。
  - 1回目の失敗から 5、10、20、40、80 分後に再試行する。最後の試行は最初の試行から約2時間35分後。
- アメダスの10分値は数日で取れなくなる（不確か）ので、それより長くは待たない。

### 3.5 管理コマンド

```sh
uv run python -m sky_server.admin run-due-jobs
```

- 起動したときの `now` で `due(now)` を1回だけ取り、古い順に1件ずつ `JobRunner.run` を実行する。`run` に渡す `now` は1件ごとに時計から取り直す。実行中に新しく入った予約（再試行など）は、次に起動したときに実行する（1回の起動で回り続けないようにするため）。
- 予約を消すのは、`run` が正常に戻ったあと。ただし `run` の中で同じジョブが予約し直されていたら（予約の `run_at` が変わっていたら）消さない。例外が出たときは予約が残るので、次の起動でまた実行される。
- 1件ごとに `job_id`、結果、理由を1行で表示する。1件で予期しない例外が出ても、表示して残りを続ける。終了コードは、例外が1件でもあれば 1、なければ 0。
- 定期的に回したいときは `watch -n 60 uv run python -m sky_server.admin run-due-jobs` などを使う（README に書く）。

## 4. 外部 API の呼び出し

### 4.1 共通

- HTTP クライアントは httpx（同期）。タイムアウトは 20 秒。
- `User-Agent` は `sky-weather-collection/0.1 (+https://github.com/hiroitozzz/sky-weather-collection)`。
- 外部 API に渡す座標は、小数第2位に丸めた文字列（`f"{lat:.2f}"`）にする。
- 失敗の分類
  - 再試行できる：接続エラー、タイムアウト、HTTP 429、HTTP 5xx、本文が JSON として読めない。
  - 再試行できない：それ以外の HTTP 4xx（パラメーターの誤りなど。本文の先頭 500 文字を `error` に残す）。
- テストではネットワークに出ない。httpx の `MockTransport` を差し込む。

### 4.2 Open-Meteo

- URL：`https://api.open-meteo.com/v1/forecast`（設定 `SKY_OPEN_METEO_URL` で変えられる）。
- 1ジョブにつき1回だけ呼ぶ。3つのモデルを1回で取る（`models` に複数を並べると、変数名の後ろに `_jma_msm` などが付いて返ってくる）。
- パラメーター

| 名前 | 値 |
|---|---|
| `latitude`, `longitude` | 小数第2位に丸めた座標 |
| `models` | `jma_msm,jma_seamless,best_match` |
| `hourly` | `precipitation,weather_code,cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,temperature_2m,dew_point_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,wind_direction_10m,cape,shortwave_radiation,visibility` |
| `minutely_15` | `precipitation,weather_code,temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_direction_10m,shortwave_radiation,visibility,cape` |
| `start_hour` / `end_hour` | 時刻 t の3時間前を時の単位に切り捨てた時刻 / t の3時間後を時の単位に切り上げた時刻 |
| `start_minutely_15` / `end_minutely_15` | t の1時間前を15分単位に切り捨てた時刻 / t の2時間後を15分単位に切り上げた時刻 |
| `timezone` | `GMT` |
| `wind_speed_unit` | `ms` |

- t は `captured_at`。時刻の形式は UTC の `YYYY-MM-DDTHH:MM`。
- `forecast` と `label` で同じパラメーターを使う。違うのは呼ぶ時刻だけ。
- 不確かな点
  - 日本の15分値は、ほとんどのモデルで1時間値からの補間と思われる（Open-Meteo のドキュメントでは、15分値をそのまま出すモデルは中欧と北米のものに限られている）。補間かどうかは生レスポンスからは判断できないので、学習時に1時間値と比べて確かめる。
  - `minutely_15` で使えない変数を指定すると 400 になる可能性がある。その場合はジョブが `failed` になり、`last_error` に本文が残る。最初の実データで確認し、必要なら変数を減らす。
  - JMA のモデルには CAPE、視程などがないことがある。その場合は値が null になる（`best_match` は他のモデルで補う）。
  - 過去の時刻（`label` のときの t-3h〜t）の値は、取得した時点で最新の予報の値で、観測値ではない。

### 4.3 アメダス

気象庁サイトが内部で使っている JSON で、公式の API ではない。設定 `SKY_AMEDAS_ENABLED=0` で無効にできる（既定は有効）。

- 観測点の一覧：`https://www.jma.go.jp/bosai/amedas/const/amedastable.json`
  - 形（不確か）：`{"44132": {"type": "A", "elems": "11112010", "lat": [35, 41.5], "lon": [139, 45.0], "alt": 25, "kjName": "東京", "knName": "トウキョウ", "enName": "Tokyo"}, ...}`。緯度経度は `[度, 分]`。
  - `elems` の2文字目が `1` の観測点を、降水量を観測している地点とみなす（不確か）。該当がなければ全地点から選ぶ。
  - 一覧は BlobStore の `cache/amedas/amedastable.json.gz` に、取得日時つきで保存する。7日より古ければ取り直す。取り直しに失敗したときは（再試行できるかどうかにかかわらず）、古い一覧があればそれを使う。
- 最寄りの3地点を、撮影地点の正確な座標からの大円距離（km、haversine、地球の半径 6371km）で選ぶ。距離の計算はサーバーの中だけで行うので、正確な座標を使う。
- 10分値：`https://www.jma.go.jp/bosai/amedas/data/point/{地点番号}/{YYYYMMDD}_{HH}.json`
  - 日時は日本時間（JST）。`HH` は 00, 03, …, 21 で、1ファイルに3時間分が入る（不確か。ファイルの境目の時刻がどちらのファイルに入るかもわからない）。
  - 本文の形（不確か）：`{"20261009120000": {"precipitation10m": [0.0, 0], "temp": [21.3, 0], ...}, ...}`。キーは JST。
- 取得する範囲：t-1h〜t+3h（JST に直す）。`HH` は「範囲の始まりを3時間単位に切り捨てた時刻」から「範囲の終わりを3時間単位に切り捨てた時刻」まで。4時間の範囲なので、1地点あたり2〜3ファイル、合計 6〜9 回の呼び出しになる。
- 呼び出しの間は 1 秒あける（設定 `SKY_AMEDAS_INTERVAL_S`、テストでは 0）。
- 404 はその地点・その時間のデータがないとみなし、そのファイルだけなら失敗にはしない（封筒に状態コードを残す）。ただし、ある観測点のファイルが1つも 200 にならなかったら、再試行できる失敗にする（design.md 5節。黙って空のラベルで完了にしないため）。データの保存期間を過ぎて取れなくなった場合は、`max_attempts` で `failed` になる。404 以外の失敗は 4.1節のとおり。1つでも再試行できる失敗があれば、そのアメダスの取得全体をやり直す（部分的な保存はしない）。
- 範囲の外の時刻のデータもファイルに入ってくるが、切り取らずにそのまま保存する。

## 5. 生レスポンスの保存

### 5.1 キー

| 内容 | キー |
|---|---|
| Open-Meteo | `raw/open_meteo/{observation_id}/{phase}.json.gz` |
| アメダス | `raw/amedas/{observation_id}/{phase}.json.gz` |
| アメダスの観測点の一覧 | `cache/amedas/amedastable.json.gz` |

- 天気データは画像とは別の BlobStore に保存する。ローカル版は `SKY_DATA_DIR/weather/` の下（画像は今までどおり `SKY_DATA_DIR/images/`）。`LocalBlobStore` に保存先のディレクトリ名を渡せるようにする（既定は `images`）。
- 同じキーに書くのは、取得に成功したときの1回だけ（再試行で成功したときも同じキー）。

### 5.2 封筒の形

1つのキーに、1回の取得で行ったすべての HTTP 呼び出しをまとめた JSON（封筒）を gzip で保存する。

```json
{
  "envelope_version": 1,
  "provider": "amedas",
  "phase": "label",
  "observation_id": "...",
  "captured_at": "2026-10-09T03:00:00+00:00",
  "fetched_at": "2026-10-09T06:30:05+00:00",
  "attribution": "出典：気象庁ホームページ（アメダス）",
  "license": "政府標準利用規約（第2.0版）",
  "query_location": {"lat": "35.68", "lon": "139.76"},
  "stations": [
    {"code": "44132", "name": "東京", "lat": 35.6917, "lon": 139.75, "distance_km": 1.6}
  ],
  "requests": [
    {
      "url": "https://www.jma.go.jp/bosai/amedas/data/point/44132/20261009_09.json",
      "params": {},
      "status": 200,
      "requested_at": "2026-10-09T06:30:05+00:00",
      "body": "{...レスポンスの本文をそのまま文字列で...}"
    }
  ]
}
```

- `body` は本文をそのままの文字列で入れる（解釈し直さないことで、元のレスポンスを失わない）。
- `requested_at` はその HTTP 呼び出しの直前に、`fetched_at` は最初の呼び出しの直前に、時計関数から取る（ジョブの `now` は使わない。何件もまとめて実行したときに古い時刻にならないようにするため）。
- `stations` はアメダスのときだけ。`query_location` は外部に渡した（丸めた）座標。アメダスでは外部に座標を渡さないが、比べやすいように同じ形で入れる。
- Open-Meteo の出典は `"Weather data by Open-Meteo.com"`、ライセンスは `"CC BY 4.0"`。
- 観測点の一覧のキャッシュも同じ形（`provider: "amedas"`、`phase: "station_table"`、`observation_id: null`、`captured_at: null`）。

## 6. `POST /internal/tasks/fetch-weather`

- 本文：`{"job_id": "<observation_id>_<forecast|label>"}`。形式が違えば 422。
- 応答
  - 200：`{"job_id": "...", "result": "ran" | "ignored", "reason": "..." | null, "status": "<ジョブの status>" | null}`。取得に失敗して再試行を予約したときも 200 を返す（再試行はこちらで予約するので、Cloud Tasks に再送させない）。
  - 401：認証の失敗。
  - 500：予期しない例外（予約の失敗を含む）。このときは Cloud Tasks の再送に任せる。
- 認証は `TaskAuthenticator`（`Request` を受け取り、だめなら 401 を投げる呼び出し可能なもの）として `create_app` に渡す。
  - 既定は「すべて拒否」。設定 `SKY_TASK_AUTH=none` のときだけ「すべて許可」（ローカルで curl から試すため）。それ以外の値は起動時にエラーにする。
  - Cloud Tasks の OIDC トークンの検証は PR-B で追加する（`SKY_TASK_AUTH=oidc`）。

## 7. 設定（環境変数）

| 名前 | 既定 | 内容 |
|---|---|---|
| `SKY_DATA_DIR` | `./data` | ローカル版のデータの置き場所（M1 から） |
| `SKY_AMEDAS_ENABLED` | `1` | `0` でアメダスを取得しない |
| `SKY_AMEDAS_INTERVAL_S` | `1` | アメダスの呼び出しの間隔（秒） |
| `SKY_OPEN_METEO_URL` | `https://api.open-meteo.com/v1/forecast` | |
| `SKY_TASK_AUTH` | （なし＝すべて拒否） | `none` で内部 API の認証を外す（ローカル専用） |

## 8. モジュールの構成

```
server/src/sky_server/
  jobs.py            WeatherJob、JobRepository、TaskScheduler とローカル版、ensure_weather_jobs、JobRunner
  weather/
    __init__.py
    common.py        丸め、時刻の切り捨て・切り上げ、失敗の分類（RetryableError、PermanentError）、封筒の作成と保存
    open_meteo.py    パラメーターの組み立てと取得
    amedas.py        観測点の一覧、最寄り3地点、ファイルの時刻の計算と取得
  task_auth.py       TaskAuthenticator と設定からの選択
```

- 依存パッケージ：httpx を開発用から本体の依存に移す。新しい依存は足さない。
- 現在時刻と HTTP クライアントと待ち（sleep）は、すべて外から渡せるようにする（テストのため）。

## 9. テストと、最初の実データでの確認

テスト（ネットワークに出ない）

- パラメーター：座標の丸め、時刻の範囲（ちょうど時・15分の境目のとき、日付をまたぐとき）。
- アメダス：最寄り3地点の選び方、`elems` で絞れないときの扱い、JST への変換と `HH` の範囲（2ファイルのとき、3ファイルのとき、日付をまたぐとき）、404 の扱い、一覧のキャッシュ。
- 封筒：gzip を解くと 5.2節の形になる。
- ジョブ：PUT で2つ予約される、再送で増えない、位置なしは `skipped`、予約に失敗したら 503 で再送で立ち直る、成功、再試行（待ち時間、`enqueued`）、`max_attempts` で `failed`、一部のプロバイダーだけ成功したときに再試行で取り直さない、二重の配達を無視する、アメダスを無効にすると `disabled`。
- 内部 API：既定で 401、`SKY_TASK_AUTH=none` で 200、本文の形式違いで 422。
- 管理コマンド：期限の来たものだけ実行し、予約を消す。

最初の実データでの確認（ネットワークに出られる環境で、ユーザーが行う）

1. ローカルでサーバーを起動し、位置つきの観測を1件 PUT する。
2. `run-due-jobs` を実行し、`forecast` のジョブが `done` になり、`SKY_DATA_DIR/weather/raw/open_meteo/...` に封筒ができることを確かめる。400 なら `last_error` を見て、`minutely_15` の変数を減らす。
3. 撮影の6〜7時間後（`label` の `run_at`）にもう一度 `run-due-jobs` を実行し、`label` のジョブが `done` になり、アメダスの封筒の `requests` の `status` が 200 で、撮影時刻の前後の10分値が入っていることを確かめる。

## 10. GCP 版（PR-B）

構成は design.md の 3節のとおり。リージョンは `us-central1` に統一する（Cloud Storage の常時無料枠が米国の一部リージョンだけのため）。デプロイの手順は `docs/deploy-gcp.md` にある。

### 10.1 どちらの版を使うか

- 設定 `SKY_BACKEND`：`local`（既定）か `gcp`。それ以外は起動時にエラー。
- `sky_server/backends.py` の `build_backend(data_dir)` が、設定から次の5つをまとめて作る：`repository`（観測と撮影者）、`blob_store`（画像）、`weather_store`（天気データ）、`job_repository`、`scheduler`。`create_app` と管理コマンドはこれを使う（引数で渡されたものが優先）。
- GCP 用のモジュール（`google.cloud.*`）は `gcp` のときだけ読み込む（ローカルでの起動とテストを軽くするため）。
- `gcp` のときの設定

| 名前 | 既定 | 内容 |
|---|---|---|
| `SKY_GCP_PROJECT` | （必須） | プロジェクト ID |
| `SKY_GCS_BUCKET` | （必須） | 画像と天気データを置くバケット |
| `SKY_TASKS_LOCATION` | `us-central1` | Cloud Tasks のキューのリージョン |
| `SKY_TASKS_QUEUE` | `weather-fetch` | キューの名前 |
| `SKY_TASKS_TARGET_URL` | （必須） | `https://<Cloud Run の URL>/internal/tasks/fetch-weather` |
| `SKY_TASKS_SERVICE_ACCOUNT` | （必須） | Cloud Tasks が OIDC トークンを発行するときのサービスアカウントのメール |

必須の値がなければ、起動時にどの名前が足りないかを示してエラーにする。

### 10.2 Firestore

- データベースは `(default)`、ネイティブモード。
- コレクション：`users`（ドキュメント ID は `user_id`）、`observations`（`observation_id`）、`weather_jobs`（`job_id`）。中身はローカル版の JSON と同じ形（`model_dump(mode="json")` や観測の dict をそのまま）。
- `add_observation` / `add_job` は `document(id).create(data)` で作り、`AlreadyExists`（`google.api_core.exceptions`）なら False。
- `get_user_by_token_hash` は `where(filter=FieldFilter("token_hash", "==", h)).limit(1)` で探す（単一項目のインデックスは自動で作られる）。
- `update_*` は `document(id).set(data)`。
- 実行の重複（3.3節）はローカル版と同じく許容する。トランザクションは使わない。

### 10.3 Cloud Storage

- `GcsBlobStore(bucket, prefix)`。画像は接頭辞 `images/`、天気データは `weather/`（キーの前に付ける）。
- `put` は `blob.upload_from_string(data)`、`get` は `blob.download_as_bytes()` で、`NotFound` なら None。
- バケットは公開アクセスを禁止し、均一なバケットレベルのアクセスにする（手順書）。

### 10.4 Cloud Tasks

- `CloudTasksScheduler.schedule(job_id, run_at)` は、キューに次のタスクを作る。
  - `name`：`{queue のパス}/tasks/{job_id}-{run_at の UNIX 秒}`。同じ予約を二重に作らないため。`AlreadyExists` なら予約済みとみなして成功にする。
  - `schedule_time`：`run_at`。
  - `http_request`：`POST SKY_TASKS_TARGET_URL`、本文 `{"job_id": ...}`、`Content-Type: application/json`、`oidc_token`（`service_account_email` は `SKY_TASKS_SERVICE_ACCOUNT`、`audience` は `SKY_TASKS_TARGET_URL`）。
- 同じ観測の再送が同時に2つ届くと、両方が `schedule` を呼ぶことがある。`job_id` と `run_at` が同じなのでタスク名も同じになり、2つ目は Cloud Tasks に `AlreadyExists` で弾かれる（Cloud Tasks のドキュメントによれば、タスクが残っている間と、実行・削除されたあともしばらくの間は、同じ名前のタスクを作れない）。実際のサービスでは確かめていない。名前を付けたタスクは作成が少し遅くなることがあるが、この用途では問題にならない。
- キューの再試行の設定（手順書で作る）：アプリが 500 を返したときだけ使われる。最大5回、最小の間隔 60 秒。同時に送る数は1、1秒あたり1件まで（アメダスへの負荷を抑えるため）。
- `run-due-jobs` は `gcp` では使わない（Cloud Tasks が呼ぶため）。`SKY_BACKEND=gcp` で実行したらエラーで終わる。

### 10.5 予定より少し早く届いたとき

Cloud Tasks の時計とサーバーの時計は少しずれることがある。予約した時刻よりわずかに早く届くと、3.3節の手順 3 で `not_due` として捨てられ、`enqueued` が true のままなので、そのジョブは二度と実行されなくなる。これを防ぐため、手順 3 は `now < next_attempt_at - 2分` のときだけ `not_due` にする（ローカル版でも同じ）。

### 10.6 OIDC の検証（`SKY_TASK_AUTH=oidc`）

- `Authorization: Bearer <ID トークン>` を `google.oauth2.id_token.verify_oauth2_token(token, Request(), audience=SKY_TASKS_TARGET_URL)` で検証する（署名、有効期限、発行者が Google であることを確かめる）。
- 加えて、`email` が `SKY_TASKS_SERVICE_ACCOUNT` と一致し、`email_verified` が true であることを確かめる。
- どれかが満たされなければ 401。理由はログにだけ出し、応答には出さない。
- 検証に使う関数は差し替えられるようにする（テストでは偽物を使う）。

### 10.7 撮影者の作成

`SKY_BACKEND=gcp` と 10.1節の設定を付けて、手元の PC から `admin create-user` を実行する（`gcloud auth application-default login` の認証情報で Firestore に書く）。

### 10.8 テスト

実際の GCP には接続しない。Firestore、Cloud Storage、Cloud Tasks のクライアントは、使う範囲だけを真似たメモリ上の偽物をテストに置き、それを差し込んで確かめる。Firestore のエミュレーターは Java と gcloud のコンポーネントが要るので使わない。

- Firestore 版：M1 と M4 のリポジトリの振る舞い（作成、同じ ID なら False、取得、更新、トークンのハッシュでの検索）。
- GCS 版：接頭辞が付くこと、ないキーは None。
- Cloud Tasks 版：作るタスクの中身（名前、時刻、URL、本文、OIDC）、`AlreadyExists` を成功とみなすこと。
- OIDC：正しいトークンは通る。`audience` やメールの違い、`email_verified` が false、ヘッダーなし、検証関数の例外はすべて 401。
- `build_backend` と設定：不正な `SKY_BACKEND`、必須の値が足りないときのエラー。
- 10.5節：予約の2分前までは実行し、それより前なら `not_due`。

### 10.9 PUT を同期の関数にする

Firestore と Cloud Tasks のクライアントは同期（呼ぶと結果が返るまで待つ）なので、`PUT /v1/observations/{id}` は `async def` ではなく `def` にする。FastAPI は `def` のエンドポイントをスレッドプールで動かすので、保存先を待つ間もほかのリクエストを受け付けられる。
