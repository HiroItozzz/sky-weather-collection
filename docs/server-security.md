# サーバーのセキュリティの見直し（PR-S）

セキュリティの見直し（レビュー）で指摘された点の直し方を決める。対応する方針は `docs/design.md` の 8節「濫用への備え」と「最初から用意しておくもの」にある。

## 1. 本文を読む前の検査（アップロード）

FastAPI は、エンドポイントの依存（認証）を解決する前に multipart の本文をすべて読む。そのため、認証されていないリクエストでも、大きな本文でメモリと一時ファイルを使わせることができる。Cloud Run では一時ファイルもメモリに置かれるので、並べて送られると OOM で落ちる。

`PUT /v1/observations/{id}` と `POST /internal/tasks/fetch-weather`（7.1節）に効く ASGI ミドルウェア（`sky_server/upload_guard.py`）を置き、アップロードでは次の順で検査する。

1. Content-Length がある場合
   - 数字として読めなければ 400 を返す。
   - `MAX_UPLOAD_BYTES` を超えていれば 413 を返す。`MAX_UPLOAD_BYTES` は、画像の上限 10MB にメタデータと multipart の区切りの分として 1MB を足した値（11MB）。
   - Content-Length がない場合は、ここでは何もしない（1.1節）。
2. `Authorization: Bearer <招待コード>` を照合する。照合できなければ 401 を返す。リポジトリへの問い合わせは同期なので、スレッドプールで行う。照合できた撮影者は `scope["state"]["user"]` に入れ、エンドポイント側の認証はそれを使う（同じリクエストで2回問い合わせない）。
3. 本文を読むときは、受け取ったバイト数を数える。`MAX_UPLOAD_BYTES` を超えたら、その時点で 413 を返して読むのをやめる。Content-Length を偽ったリクエストへの備え。

- 応答の本文は、FastAPI の `HTTPException` と同じ形（`{"detail": "..."}`）にする。401 には `WWW-Authenticate: Bearer` を付ける。
- 検査の順番は「大きさ → 認証 → 本文」。大きさの検査は資源を使わないので先に行い、問い合わせのある認証を後にする。

### 1.1 Content-Length がないとき

Content-Length がなくても 411 にはせず、受け付ける。ただし認証（手順 2）を先に通し、本文は手順 3 で数えながら打ち切る。

- アプリは React Native の `FormData` でファイルを送る。React Native の Android 版は Content-Length を付けるはずだが、確かめていない。付かない経路があると、M3 の分類では 411 が「断られた」（`rejected`）になり、すべての送信が失敗してしまう。
- この方法でも、検査の目的（未認証のリクエストに本文を読ませない、大きさで資源を使わせない）は満たせる。そこで、アプリを壊す危険がないほうを選んだ（監督が決定）。411 にする設定の切り替え口は作らない。
- 数えて打ち切る処理は、本文がちょうど上限なら通す、上限＋1バイトなら 413、Content-Length を小さく申告して上限を超えて送れば 413、の3つをテストする。

### 1.2 Cloud Run の設定

デプロイの手順書に `--concurrency=10`（1インスタンスが同時に処理するリクエストの数）と `--timeout=120`（1リクエストの打ち切り時間、秒）を足す。512MiB のメモリで、11MB のアップロードを 10 件同時に処理できる。

## 2. OIDC の公開鍵のキャッシュ

`google.oauth2.id_token.verify_oauth2_token` は、呼ぶたびに Google の公開鍵を取りに行く。未認証のリクエストで、外向きの通信を何倍にも増やせてしまう。

- トークンが `.` で区切られた3つの部分でなければ、検証する前に 401 にする。
- 公開鍵（`https://www.googleapis.com/oauth2/v1/certs`）は、取得してから 1時間キャッシュする。トークンの `kid` がキャッシュになければ取り直す。ただし取り直しは 60 秒に1回まで（知らない `kid` を並べて送られても、通信が増えないようにする）。
- 検証は `google.auth.jwt.decode(token, certs=キャッシュした公開鍵, audience=...)` で行い、発行者（`iss`）が `accounts.google.com` か `https://accounts.google.com` であることを自分で確かめる（`verify_oauth2_token` がしていることと同じ）。`email` と `email_verified` の確認は今までどおり。
- 公開鍵の取得には httpx を使う（タイムアウトは外部 API と同じ）。取得に失敗して、使える古い公開鍵もなければ 401 にしてログに残す（Cloud Tasks が再送する。7.2節）。

## 3. 撮影者ごとの1日の送信数の上限

- 設定 `SKY_DAILY_UPLOAD_LIMIT`（既定 100）。1日は UTC で区切る。
- 新しい観測を作る直前（重複の確認の後）に、その撮影者のその日の件数を読む。上限以上なら 429 を返し、保存しない。`Retry-After` には、次の UTC の0時までの秒数を入れる。
- 件数は、観測の作成に成功したときだけ1増やす。再送（200）、409、422 では増やさない。
- 再送（すでにある観測の 200）は、上限を超えていても受け付ける。アプリの再送のキューが詰まらないようにするため。
- 件数を読んでから増やすまでの間に同時に送られると、上限を少し超えることがある。被害を抑えるための上限なので許容する。
- 保存先：`ObservationRepository` に `get_daily_count(user_id, day) -> int` と `increment_daily_count(user_id, day) -> None` を足す。`day` は `YYYYMMDD`。
  - ローカル版：`SKY_DATA_DIR/usage/{user_id}_{day}.json`（`{"user_id": ..., "day": ..., "count": n}`）。
  - Firestore 版：コレクション `usage`、ドキュメント ID `{user_id}_{day}`、項目 `user_id`、`day`、`count`。増やすときは `firestore.Increment(1)` で `set(..., merge=True)`。

## 4. 「最初から用意しておくもの」の管理コマンド

アプリの画面は作らない。サーバーの管理コマンド（`python -m sky_server.admin`）で扱う。

### 4.1 プライバシーゾーン

- 撮影者ごとに複数持てる。`User` に `privacy_zones: list[PrivacyZone]`（既定は空）を足す。
- `PrivacyZone`：`zone_id`（UUID）、`lat`（-90〜90）、`lon`（-180〜180）、`radius_m`（0 より大きく 50000 以下）、`label`（文字列、null 可）、`created_at`。
- コマンド
  - `add-privacy-zone --user-id ID --lat LAT --lon LON --radius-m R [--label TEXT]`：追加して `zone_id` を表示する。
  - `list-privacy-zones --user-id ID`：1行に1つ、`zone_id lat lon radius_m label` を表示する。
  - `delete-privacy-zone --user-id ID --zone-id ZID`：消す。見つからなければ終了コード 1。
- ゾーンは、公開版を作るときに使う（8節）。サーバーが観測を受け取るときには使わない。

### 4.2 公開への同意

- `set-consent --user-id ID --public yes|no`：`consent_public` を変える。

### 4.3 撮影者のデータの削除

- `delete-user --user-id ID --yes`：撮影者と、その撮影者のデータをすべて消す。`--yes` がなければ、消す件数だけを表示して何も消さずに終了コード 1。
- 2段階で消す（7.3節）。消すもの（この順で消す。途中で失敗しても、もう一度実行すれば残りを消せるように、撮影者そのものは最後に消す）
  1. 観測ごとに：天気のジョブ（`{id}_forecast`、`{id}_label`）、天気の生レスポンス（ジョブの `providers.*.blob_key`。ジョブがなくても `raw/open_meteo/{id}/{phase}.json.gz` と `raw/amedas/{id}/{phase}.json.gz` を消してみる）、画像（`image_key`）、観測そのもの。
  2. その撮影者の送信数の記録（`usage`）。
  3. 撮影者。
- 予約済みのタスク（Cloud Tasks、ローカルの `tasks/`）は消さない。届いてもジョブがないので `job_not_found` で無視される。
- 必要になるインターフェースの追加
  - `ObservationRepository`：`list_observations_by_user(user_id) -> list[dict]`、`delete_observation(id)`、`delete_daily_counts(user_id)`、`delete_user(user_id)`。
  - `BlobStore`：`delete(key)`（なければ何もしない）。
  - `JobRepository`：`delete_job(job_id)`（なければ何もしない）。
- 表示：消した観測の数を最後に表示する。

## 5. 小さな修正

- `SKY_BACKEND=gcp` で `SKY_TASK_AUTH=none` なら、起動時にエラーにする（本番で内部 API の認証が外れないように）。
- `SKY_BACKEND=gcp` では、`/docs`、`/redoc`、`/openapi.json` を無効にする。
- 画像の先頭が `FF D8 FF`（JPEG）でなければ 422。
- 外部 API（Open-Meteo、アメダス、OIDC の公開鍵）の応答は、少しずつ読みながら大きさを数え、5MB を超えたら `RetryableError`（公開鍵なら取得の失敗）にする。タイムアウトは、接続や1回の読み取りごとのものに加えて、1回の呼び出し全体の期限（20秒）をかける（実際の最長は 7.6節）。
- アメダスの観測点の一覧で、地点番号が `^\d{5}$` でないものは捨てる（URL に入れるため）。
- デプロイの手順書：付けるロールの一覧（誰に、どの範囲で、何のロールか）の表を足す。予算アラートに、予測額（forecasted-spend）のしきい値を足す。
- Dockerfile：uv のイメージを digest で固定する（`ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c`）。
- `server/README.md` の `SKY_TASK_AUTH` の説明を、今の仕様（未設定はすべて拒否、`none` はローカルだけ、`oidc` は GCP）に直す。

## 6. 設定（追加分）

| 名前 | 既定 | 内容 |
|---|---|---|
| `SKY_DAILY_UPLOAD_LIMIT` | `100` | 撮影者ごとの1日（UTC）の新規の観測の上限 |

## 7. レビューを受けての修正

PR #11 のレビューの指摘への対応。判断は監督が決めた。

### 7.1 内部 API の本文の大きさ（1節の追加）

`POST /internal/tasks/fetch-weather` も、FastAPI が OIDC の検証の前に JSON の本文をすべて読む。`UploadGuard` の対象に加え、このパスでは上限 4KB で、Content-Length の検査（数字でなければ 400、超えていれば 413）と、数えながらの打ち切り（413）だけを行う。認証はアプリの OIDC の検証に任せる（ミドルウェアでは行わない）。

### 7.2 公開鍵の取り直しに失敗したとき（2節の追加）

- 取り直しに失敗したとき、または 60 秒の間隔の内側で取りに行けないときに、古い公開鍵を持っていれば、それを使う（stale-if-error）。古い公開鍵を使えるのは、最後に取得できてから 24 時間まで。それより古ければ 401。
- 60 秒に1回の制限はそのまま。

### 7.3 撮影者のデータの削除を2段階にする（4.3節の変更）

`delete-user` の実行中や直後に、実行中の天気ジョブが、ジョブの状態や生レスポンスを書き戻すことがある。これらは観測を消したあとでは撮影者からたどれないので、消した観測の ID を撮影者に記録しておき、時間をおいてもう一度消す。

- 1回目の `delete-user --yes`
  1. 撮影者を無効にする（`revoked_at` がなければ入れる）。`deletion_started_at`（なければ今の時刻）を入れて保存する。これ以降、新しい観測は 401 で受け付けない。
  2. 撮影者の観測を一覧し、その ID を `deletion_observation_ids`（撮影者に持たせる、重複なし）に足して保存する。
  3. `deletion_observation_ids` のすべての ID について、観測ごとのデータを消す（4.3節の 1。画像は 7.4節）。
  4. 撮影者は消さずに、「数分おいてもう一度実行してください」と表示して終了コード 0。
- 2回目以降の `delete-user --yes`：上の 1〜3 をもう一度行う。`deletion_started_at` から 10 分以上たっていれば、続けて送信数の記録（`usage`）と撮影者を消す。10 分たっていなければ、1回目と同じく撮影者を残して、あと何分待つかを表示する。
- 10 分は、実行中のジョブ（Cloud Run の打ち切りは 120 秒）が書き戻しを終えるのに十分な長さとして決めた。
- `User` に `deletion_started_at: datetime | None`、`deletion_observation_ids: list[str]`（既定は空）を足す。
- 手順書と README に「`delete-user` は、数分おいてもう一度実行する」と書く。

### 7.4 画像を接頭辞で消す（4.3節の変更）

- `BlobStore` に `delete_prefix(prefix)` を足す（ローカル版はディレクトリごと消す、GCS 版は接頭辞で一覧して消す）。
- 観測ごとに、画像は `{id}/`、天気の生レスポンスは `raw/open_meteo/{id}/` と `raw/amedas/{id}/` を接頭辞で消す（同じ ID で別の画像が送られて 409 になったときに、先に保存された画像が残らないように）。ジョブの `blob_key` が標準の場所と違えば、それも消す。
- 観測の記録がない ID の残骸（観測の保存の前に失敗したときの画像など）は、撮影者からたどれないので残りうる。

### 7.5 件数の増加に失敗したとき（3節の変更）

観測の作成に成功したら、先に天気ジョブを予約し（`reserve_weather_jobs`）、そのあとで件数を増やす。件数の増加が例外を投げても、ログに出すだけで 201 を返す（上限は被害を抑えるためのもので、数え漏れは許容する）。

### 7.6 外部 API のタイムアウトと圧縮（5節の変更）

- httpx のタイムアウト（接続、1回の読み取り、書き込み、接続の取得）をそれぞれ 5 秒にする。呼び出し全体の期限（20 秒）は、塊を1つ読むたびに確かめる。そのため、1回の呼び出しが最も長くかかるのは、20 秒の期限の直前に読み取りが止まった場合の約 25 秒。
- 外部 API には `Accept-Encoding: identity` を送り、圧縮していない応答を求める（小さな圧縮データが展開すると巨大になる攻撃への備え。5MB の上限は展開後の大きさで数えているので、圧縮されて返ってきても上限は効く）。

### 7.7 その他

- `SKY_DAILY_UPLOAD_LIMIT` が1以上の整数でなければ、起動時に `ConfigError`。
- `list-privacy-zones` の出力はタブ区切りにする。
