# M5 の仕様（サーバー）：天気の要約と、一覧・記録の API

`docs/design.md` の 13.2〜13.5 を、サーバーに実装するための詳細。アプリ側（13.1、13.6）は別の PR で作る。

## 1. 要約の計算（`sky_server/summary.py`）

生レスポンスの封筒（`docs/m4-weather.md` 5.2節）から、要約を作る純粋な関数。入力は封筒の dict と撮影時刻で、ファイルや API には触れない。同じ入力からは、何度でも同じ結果が出る。

封筒の `requests[*].body` は、レスポンスの本文そのままの文字列。JSON として読んでから使う。

### 1.1 撮影時の天気（`weather_at_capture(open_meteo_envelope, captured_at)`）

- `forecast` のジョブで取った Open-Meteo の封筒を使う。
- `hourly.time`（UTC、`YYYY-MM-DDTHH:MM`）から、変数によって違う行を選ぶ（design.md 13.3、監督の決定）。
  - `precipitation`：時刻 T = `ceil_hour(captured_at)`（ちょうど時なら captured_at そのもの）の行。Open-Meteo の1時間値の降水量は「その時刻までの1時間の合計」なので、撮影時刻を含む1時間 (T-1h, T] の値はこの行になる。
  - `weather_code`、`temperature_2m`、`cloud_cover`：撮影時刻に最も近い時刻の行（時に丸める。ちょうど30分は切り上げる）。これらはその時刻の状態を表す値なので。
- 取り出す変数（`jma_seamless`。複数のモデルを1回で取っているので、変数名の後ろに `_jma_seamless` が付く）：`weather_code`、`temperature_2m`、`precipitation`、`cloud_cover`。
- 返す形：`{"category", "weather_code", "temperature_c", "precipitation_mm", "cloud_cover_pct"}`。
  - `weather_code` が取れない（行がない、値が null）か、`category` が決まらないときは、全体を null にする（アプリは `category` が3つのどれかでないと、その観測を読めないため）。
  - ほかの値は、取れなければその項目だけ null。
- `category`：`weather_code` が 0〜2 なら `clear`、3・45・48 なら `cloudy`、51 以上なら `rain`。どれにも当たらなければ決まらない（Open-Meteo が返す WMO のコードは、0〜3、45、48、51 以上だけのはず）。

### 1.2 答え合わせ（`answer(open_meteo_envelope, amedas_envelope, captured_at)`）

対象の時間帯は (t, t+60分]（t は含まず、t+60分は含む）。

1. アメダス：封筒の `stations` のうち、`distance_km` が 10 以下の観測点を、近い順に見る。
   - その観測点のファイル（`requests` のうち、URL が `/point/{地点番号}/` を含み、状態が 200 のもの）の本文をまとめる。キーは JST の `YYYYMMDDHHMMSS`。
   - 対象の時刻は、10分刻みで t < k ≤ t+60分 を満たす6つ。キーの時刻の値は「その時刻までの10分間」の降水量（`precipitation10m` の1つ目の値）。
   - 6つすべてがあり、値が数値（null でない）なら、その観測点で判定する。1つでも 0 より大きければ `rain`、すべて 0 なら `no_rain`。`source` は `amedas`。
   - そろわなければ次の観測点を見る。10km 以内にそろう観測点がなければ 2 へ。
   - 品質の印（`precipitation10m` の2つ目の値）は見ない（意味を確かめていないため。不確か）。
2. Open-Meteo：`minutely_15.time` と `precipitation_jma_seamless` を使う。
   - 対象は、15分刻みで t < T ≤ t+60分 を満たす4つの時刻。値は「その時刻までの15分間」の降水量。
   - 4つすべてが数値なら、合計（小数第6位で丸める）が 0.1 以上で `rain`、未満で `no_rain`。`source` は `open_meteo`。
3. どちらもそろわなければ `{"result": "unknown", "source": null}`。

- 10分値・15分値は「その時刻までの10分間・15分間」の値なので、対象の時間帯の始まりの刻みには、t より前の時間が最大で14分ほど含まれる（例：t が 12:03 なら 12:10 の10分値は 12:00〜12:10）。ゲームの答え合わせ用なので、このずれは許す（監督の決定）。学習用のラベルは別に決める。

- 封筒が null（そのプロバイダーの取得に失敗した）なら、そのプロバイダーは「そろわない」として扱う。
- 本文が JSON として読めない、形が違う（キーがない、配列の長さが違う）ときも「そろわない」とする。例外は投げない。

### 1.3 当たりかどうか

`correct`：`user_guess` が null か、`answer.result` が `unknown` なら null。そうでなければ `user_guess == answer.result`。

## 2. 要約の保存

- `WeatherJob` に `summary: dict | None`（既定 null）を足す。
  - `forecast`：`{"weather_at_capture": ...}`
  - `label`：`{"answer": ...}`
- ジョブが完了したとき（`done` か `failed`）に要約を計算する。design.md 13.4 は `done` のときと書いているが、`failed` でも取れたプロバイダーの封筒があれば使う（例：アメダスの取得だけが上限まで失敗しても、Open-Meteo で答えが出せる）。`skipped` では計算しない。
- 計算した要約は、ジョブの `summary` に入れ、同じ内容を観測のドキュメントにも写す（`weather_at_capture`、`answer`）。一覧と記録の API が、観測だけを読めば済むようにするため。
  - `ObservationRepository` に `update_observation_fields(observation_id, fields: dict)` を足す（Firestore は `update`、ローカル版は読んで書き直す）。
- 要約の計算や観測への書き込みに失敗しても、ジョブの状態はそのまま（`done` / `failed`）。`summary` は null にして、エラーをログに出す。
- 封筒は、ジョブの `providers.*.blob_key` から `weather_store` で読む。
- `label` のジョブが終わったとき（`done`、`failed`、`skipped` のどれでも。要約の計算に失敗したときも）、観測に `label_done: true` を書く。答えが出るのを待っているかどうか（4.1節の `pending`）を、観測だけで判断できるようにするため。
  - `answer` があるかどうかで判断しないのは、位置がない観測（`skipped`）や要約の計算に失敗した観測では `answer` が書かれず、いつまでも待ちのままに見えてしまうため。
  - 位置がない観測は、作った時点で `label` のジョブが `skipped` になるので、`ensure_weather_jobs` で観測に `label_done: true` を書く。

## 3. 撮影時刻の UTC（`captured_at_utc`）

- `captured_at` は 2020-01-01T00:00:00Z より前なら 422（過去側の下限）。年が4桁にならない時刻や、UTC に直すときにあふれる時刻で、カーソルが壊れたり 500 になったりしないようにするため。`captured_at_utc` の年は4桁で書く。

- `captured_at` はアプリが送ったタイムゾーンのまま保存しているので、文字列で並べると順番が崩れる。受け取ったときに `captured_at_utc`（UTC、ミリ秒まで、末尾 `Z`。例 `2026-10-09T03:00:00.123Z`）をサーバーの項目として足す。
- 並び順、カーソル、Firestore の複合インデックスはこれを使う。応答の `captured_at` は今までどおり（送られた値）。

## 4. API

すべて招待コードの認証が必要。自分の観測だけを返す。

### 4.1 観測の表示の形（共通）

```json
{
  "observation_id": "...",
  "received_at": "...",
  "captured_at": "...",
  "user_guess": "rain",
  "weather_at_capture": {"category": "cloudy", "weather_code": 3, "temperature_c": 18.2, "precipitation_mm": 0.0, "cloud_cover_pct": 90},
  "answer": {"result": "rain", "source": "amedas", "pending": false},
  "correct": true
}
```

- `answer` に `pending`（真偽値）を入れる（design.md 13.5、監督の決定）。観測に `label_done` が true でなければ `pending: true`、true なら `false`。
- 観測に `weather_at_capture` がなければ null。`answer` がなければ `{"result": "unknown", "source": null, "pending": ...}`。
- `before` は 128 文字まで（`Query(max_length=128)`）。深い入れ子の JSON で読み込みが例外を出さないようにするため。

### 4.2 `GET /v1/observations/{id}`

4.1節の形を返す。他人の観測と、ない観測は 404（今までどおり）。

### 4.3 `GET /v1/me/observations?limit=50&before=<カーソル>`

- 応答：`{"observations": [...], "next_before": "<カーソル>" | null}`。
- 並び順：`captured_at_utc` の降順、同じなら `observation_id` の降順。
- `limit`：1〜100（既定 50）。範囲外は 422。
- カーソル：最後の観測の `[captured_at_utc, observation_id]` を JSON にして base64url（`=` なし）にした文字列。`before` を付けると、その観測より後ろ（並び順で）のものだけを返す。
  - 読めない、JSON でない、2要素の文字列の配列でない、`captured_at_utc` や `observation_id` の形式が違うときは 422。
- 次のページがあるかは、`limit + 1` 件を読んで判断する。なければ `next_before` は null。
- `ObservationRepository` に `list_observations_page(user_id, limit, before: tuple[str, str] | None) -> list[dict]` を足す。
  - ローカル版：撮影者の観測を全部読んで並べる。
  - Firestore 版：`where user_id ==`、`order_by captured_at_utc DESC`、`order_by observation_id DESC`、`start_after`、`limit`。

### 4.4 `GET /v1/me/stats`

```json
{
  "observations_total": 42,
  "answered_total": 30,
  "guesses_total": 20,
  "guesses_correct": 14,
  "by_category": {"clear": 20, "cloudy": 15, "rain": 5, "unknown": 2}
}
```

- 撮影者の観測を全部読んで数える（`list_observations_by_user`）。
- `answered_total`：`answer.result` が `unknown` でない数。
- `guesses_total`：`user_guess` があり、かつ答えが出た数。`guesses_correct`：そのうち当たった数。
- `by_category`：`weather_at_capture.category` ごとの数。`weather_at_capture` が null なら `unknown`。4つのキーは、0 でも必ず返す。

## 5. 要約の作り直し（管理コマンド）

`uv run python -m sky_server.admin rebuild-summaries`

- すべての観測について、次を行う。
  - `captured_at_utc` がなければ足す。
  - `forecast` と `label` のジョブが `done` か `failed` なら、封筒から要約を計算し直し、ジョブと観測に保存する。計算や観測への書き込みに失敗したときは、ジョブにも観測にも何も書かない（前の要約を null で消さない）。
  - `label` のジョブが `done`・`failed`・`skipped` なら、観測に `label_done: true` を書く。
- 観測の一覧は、先にすべての ID を読んでから1件ずつ処理する（Firestore のストリームを開いたまま長く処理すると、期限が切れることがあるため）。
- 1件の失敗で止めず、最後に「処理した件数、失敗した件数」を表示する。失敗が1件でもあれば終了コード 1。
- `ObservationRepository` に `list_all_observation_ids() -> list[str]` を足す（`list_all_observations` の代わり）。

## 6. Firestore の複合インデックス

`observations` に、`user_id` 昇順・`captured_at_utc` 降順・`observation_id` 降順の複合インデックスが必要。デプロイの手順書に作り方を足す。

```sh
gcloud firestore indexes composite create \
  --collection-group=observations \
  --field-config=field-path=user_id,order=ascending \
  --field-config=field-path=captured_at_utc,order=descending \
  --field-config=field-path=observation_id,order=descending
```

## 7. テスト

- 要約の関数は、生レスポンスの fixture（仕様の形に合わせて手で作ったもの）でテストする。境界を必ず入れる。
  - t ちょうどのキーは含まず、t+60分ちょうどのキーは含む（アメダス・Open-Meteo の両方）。
  - 10km ちょうどの観測点は使い、10.1km は使わない。
  - Open-Meteo の合計が 0.1mm ちょうどなら `rain`、0.09 なら `no_rain`（浮動小数の誤差が出る組み合わせも）。
  - 値が1つ欠けたら、次の観測点、Open-Meteo の順に移る。どれもそろわなければ `unknown`。
  - 撮影時刻がちょうど時のときと、そうでないときの `weather_at_capture` の行。
  - カテゴリの境目（2・3・45・48・50・51）。
- 一覧：同じ `captured_at_utc` の観測がページの境目に並ぶときに、抜けも重複もないこと。不正なカーソルで 422。
- 記録：数え方の各項目。
- ジョブ：完了で要約が保存され、観測にも写ること。計算が例外を投げてもジョブは `done` のまま。
- 作り直しのコマンド。
