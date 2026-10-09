# サーバーのセキュリティの見直し（PR-S）

セキュリティの見直し（レビュー）で指摘された点の直し方を決める。対応する方針は `docs/design.md` の 8節「濫用への備え」と「最初から用意しておくもの」にある。

## 1. 本文を読む前の検査（アップロード）

FastAPI は、エンドポイントの依存（認証）を解決する前に multipart の本文をすべて読む。そのため、認証されていないリクエストでも、大きな本文でメモリと一時ファイルを使わせることができる。Cloud Run では一時ファイルもメモリに置かれるので、並べて送られると OOM で落ちる。

`PUT /v1/observations/{id}` だけに効く ASGI ミドルウェア（`sky_server/upload_guard.py`）を置き、次の順で検査する。

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
- 公開鍵の取得には httpx を使う（タイムアウトは 4節と同じ）。取得に失敗したら 401 にしてログに残す（Cloud Tasks が再送する）。

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
- 消すもの（この順で消す。途中で失敗しても、もう一度実行すれば残りを消せるように、撮影者そのものは最後に消す）
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
- 外部 API（Open-Meteo、アメダス、OIDC の公開鍵）の応答は、少しずつ読みながら大きさを数え、5MB を超えたら `RetryableError`（公開鍵なら取得の失敗）にする。タイムアウトは、接続や1回の読み取りごとではなく、1回の呼び出し全体（20秒）にかける。
- アメダスの観測点の一覧で、地点番号が `^\d{5}$` でないものは捨てる（URL に入れるため）。
- デプロイの手順書：付けるロールの一覧（誰に、どの範囲で、何のロールか）の表を足す。予算アラートに、予測額（forecasted-spend）のしきい値を足す。
- Dockerfile：uv のイメージを digest で固定する（`ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c`）。
- `server/README.md` の `SKY_TASK_AUTH` の説明を、今の仕様（未設定はすべて拒否、`none` はローカルだけ、`oidc` は GCP）に直す。

## 6. 設定（追加分）

| 名前 | 既定 | 内容 |
|---|---|---|
| `SKY_DAILY_UPLOAD_LIMIT` | `100` | 撮影者ごとの1日（UTC）の新規の観測の上限 |
