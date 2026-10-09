# GCP へのデプロイ手順

サーバー（`server/`）を Cloud Run に置き、Firestore、Cloud Storage、Cloud Tasks を使う手順。仕様は `docs/m4-weather.md` の 10節にある。

- コマンドはすべて手元の PC で、`gcloud` を使って実行する。
- リージョンはすべて `us-central1` にする。Cloud Storage の常時無料枠が、米国の一部のリージョンだけで使えるため。日本からのアップロードが少し遅くなるが、写真1枚の送信なので問題にならない。
- 無料枠と料金は変わることがある。この手順書の値は書いた時点（2026-10）の理解で、確かめていないものには「要確認」と書いた。始める前と、運用を始めて1か月後に、公式のページと請求額を確認する（8節）。

## 0. 準備

- `gcloud` をインストールし、`gcloud auth login` でログインしておく。
- 請求先アカウント（クレジットカードを登録したもの）が必要。無料枠の範囲で使う場合でも、請求先アカウントを紐づけないと Cloud Run などを有効にできない。
- 以下の変数を、作業するシェルで設定しておく。

```sh
export PROJECT_ID=sky-weather-collection   # 取れなければ末尾に数字を付ける（例：sky-weather-collection-1）
export REGION=us-central1
export SERVICE=sky-server
export BUCKET=${PROJECT_ID}-sky-data
export QUEUE=weather-fetch
export RUN_SA=sky-server@${PROJECT_ID}.iam.gserviceaccount.com     # Cloud Run が動くときのアカウント
export TASKS_SA=sky-tasks@${PROJECT_ID}.iam.gserviceaccount.com    # Cloud Tasks が内部 API を呼ぶときのアカウント
```

## 1. プロジェクトと予算アラート

```sh
gcloud projects create $PROJECT_ID
gcloud config set project $PROJECT_ID

gcloud billing accounts list                     # 請求先アカウントの ID を確認する
export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX
gcloud billing projects link $PROJECT_ID --billing-account=$BILLING_ACCOUNT
```

想定外の請求に早く気づけるように、予算アラートを作っておく。金額の通貨は請求先アカウントの通貨に合わせる（円のアカウントなら `JPY`）。アラートはメールで知らせるだけで、使用を止めはしない。

```sh
gcloud services enable billingbudgets.googleapis.com
gcloud billing budgets create \
  --billing-account=$BILLING_ACCOUNT \
  --display-name="sky-weather-collection" \
  --filter-projects=projects/$PROJECT_ID \
  --budget-amount=500JPY \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=1.0 \
  --threshold-rule=percent=1.0,basis=forecasted-spend
```

最後のしきい値は、月末までの予測額が予算を超えそうになった時点で知らせる（実際に使った額が超える前に気づける）。

## 2. API を有効にする

```sh
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  cloudtasks.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

## 3. Firestore、Cloud Storage、Cloud Tasks

```sh
# Firestore（ネイティブモード、データベースは (default)）。場所は後から変えられない
gcloud firestore databases create --location=$REGION --type=firestore-native

# Cloud Storage（画像と天気データ）。公開アクセスを禁止する
gcloud storage buckets create gs://$BUCKET \
  --location=$REGION \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --public-access-prevention

# Cloud Tasks のキュー。アメダスへの負荷を抑えるため、同時に1件、1秒に1件までにする。
# 再試行の設定は、アプリが 500 を返したときだけ使われる（取得の失敗はアプリが自分で予約し直す）
gcloud tasks queues create $QUEUE \
  --location=$REGION \
  --max-dispatches-per-second=1 \
  --max-concurrent-dispatches=1 \
  --max-attempts=5 \
  --min-backoff=60s
```

- Firestore の無料枠は、プロジェクトの `(default)` データベースだけに付く（要確認）。別の名前のデータベースは作らない。

## 4. サービスアカウントと権限

```sh
gcloud iam service-accounts create sky-server --display-name="sky-server (Cloud Run)"
gcloud iam service-accounts create sky-tasks --display-name="sky-tasks (Cloud Tasks の呼び出し)"

# Cloud Run のアカウント：Firestore の読み書き
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member=serviceAccount:$RUN_SA --role=roles/datastore.user

# Cloud Run のアカウント：バケットのオブジェクトの読み書き（このバケットだけ）
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$RUN_SA --role=roles/storage.objectUser

# Cloud Run のアカウント：キューにタスクを作る（このキューだけ）
gcloud tasks queues add-iam-policy-binding $QUEUE --location=$REGION \
  --member=serviceAccount:$RUN_SA --role=roles/cloudtasks.enqueuer

# Cloud Run のアカウントが、sky-tasks の名前で OIDC トークンつきのタスクを作れるようにする
gcloud iam service-accounts add-iam-policy-binding $TASKS_SA \
  --member=serviceAccount:$RUN_SA --role=roles/iam.serviceAccountUser
```

付けるロールの一覧：

| 誰に | どの範囲で | ロール | 何のため |
|---|---|---|---|
| `sky-server`（Cloud Run） | プロジェクト | `roles/datastore.user`（Cloud Datastore ユーザー） | Firestore の読み書き |
| `sky-server` | バケット `$BUCKET` | `roles/storage.objectUser`（Storage オブジェクト ユーザー） | 画像と天気データの読み書き・削除 |
| `sky-server` | キュー `$QUEUE` | `roles/cloudtasks.enqueuer`（Cloud Tasks エンキューア） | 天気ジョブの予約 |
| `sky-server` | サービスアカウント `sky-tasks` | `roles/iam.serviceAccountUser`（サービス アカウント ユーザー） | `sky-tasks` の名前で OIDC トークンつきのタスクを作る |
| `sky-tasks`（Cloud Tasks の呼び出し） | なし | なし | 内部 API はアプリがトークンのメールを確かめる |

- 権限は、必要なものだけをできるだけ狭い範囲に付けている。プロジェクト全体の編集者などは付けない。
- `sky-tasks` には権限を何も付けない。内部 API は、トークンのメールが `sky-tasks` であることをアプリで確かめる（`docs/m4-weather.md` 10.6節）。

## 5. デプロイ

Cloud Run の URL は `https://{サービス名}-{プロジェクト番号}.{リージョン}.run.app` の形で、デプロイの前に決まる。アプリは起動時に内部 API の URL を必要とするので、先に組み立てておく。

```sh
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
export SERVICE_URL=https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app
export TASKS_URL=${SERVICE_URL}/internal/tasks/fetch-weather
```

`server/` に移動してデプロイする。`--source .` で、Cloud Build が `server/Dockerfile` からイメージを作る。

```sh
cd server
gcloud run deploy $SERVICE \
  --source . \
  --region=$REGION \
  --service-account=$RUN_SA \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=10 \
  --timeout=120 \
  --memory=512Mi \
  --set-env-vars=SKY_BACKEND=gcp,SKY_GCP_PROJECT=$PROJECT_ID,SKY_GCS_BUCKET=$BUCKET,SKY_TASKS_LOCATION=$REGION,SKY_TASKS_QUEUE=$QUEUE,SKY_TASKS_TARGET_URL=$TASKS_URL,SKY_TASKS_SERVICE_ACCOUNT=$TASKS_SA,SKY_TASK_AUTH=oidc
```

- 初回は Artifact Registry のリポジトリ（`cloud-run-source-deploy`）を作るか聞かれるので、`Y` と答える。
- `--allow-unauthenticated` は、だれでも URL に届くようにする設定。アプリは招待コードで撮影者を確かめ、内部 API は OIDC トークンで確かめるので、Cloud Run 側の認証は使わない。
- `--concurrency=10` は、1つのインスタンスが同時に処理するリクエストの数。アップロードは1件で最大 11MB をメモリ（Cloud Run では一時ファイルもメモリに置かれる）に持つので、512MiB に収まるように絞っている。`--timeout=120` は、1リクエストの打ち切り時間（秒）。遅い回線からの写真の送信と、アメダスの取得（最大9回、1秒間隔）が収まる長さ。
- `--min-instances=0` で、使っていないときはインスタンスが0になる（費用がかからない。最初のリクエストだけ数秒遅くなる）。`--max-instances=2` は、想定外のアクセスで費用が膨らまないようにするための上限。
- ビルドが権限のエラーで失敗したときは、エラーに出るサービスアカウント（Cloud Build が使うもの）に、エラーが求めるロールを付ける。プロジェクトの作成時期によって、必要な設定が違う（要確認）。

デプロイが終わったら、URL が 5節で組み立てたものと同じか確かめる。

```sh
gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)'
echo $SERVICE_URL
```

違っていたら、`TASKS_URL` を表示された URL で作り直し、`gcloud run services update $SERVICE --region=$REGION --update-env-vars=SKY_TASKS_TARGET_URL=$TASKS_URL` を実行する。

## 6. 動作の確認

```sh
curl -i $SERVICE_URL/healthz                       # 200 {"status":"ok"}
curl -i -X POST $TASKS_URL -H 'Content-Type: application/json' -d '{"job_id":"x"}'   # 401
```

撮影者は手元の PC から作る。手元の認証情報で Firestore に書くので、先にログインしておく。

```sh
gcloud auth application-default login
cd server
SKY_BACKEND=gcp SKY_GCP_PROJECT=$PROJECT_ID SKY_GCS_BUCKET=$BUCKET \
SKY_TASKS_LOCATION=$REGION SKY_TASKS_QUEUE=$QUEUE \
SKY_TASKS_TARGET_URL=$TASKS_URL SKY_TASKS_SERVICE_ACCOUNT=$TASKS_SA \
  uv run python -m sky_server.admin create-user --name alice
```

表示された招待コードで、`server/README.md` の curl の例（`http://localhost:8000` を `$SERVICE_URL` に変える）を使って1件アップロードする。

```sh
# ジョブの予約（forecast はすぐ実行されて消え、label が残る）
gcloud tasks list --queue=$QUEUE --location=$REGION

# 取得した生レスポンス
gcloud storage ls -r gs://$BUCKET/weather/raw/
gcloud storage cat gs://$BUCKET/weather/raw/open_meteo/<observation_id>/forecast.json.gz | gunzip | head -c 2000

# ログ
gcloud run services logs read $SERVICE --region=$REGION --limit=50
```

ジョブの状態は、Cloud Console の Firestore の画面で `weather_jobs` コレクションを開くと見られる。`forecast` が `done` になっていることを確かめ、3時間30分後に `label` も `done` になり、`weather/raw/amedas/` に封筒ができていることを確かめる（`docs/m4-weather.md` 9節）。

## 7. 更新

コードを変えたら、5節の `gcloud run deploy` を同じ引数でもう一度実行する（環境変数は前回の値が残るので、`--set-env-vars` は省いてもよい）。

Artifact Registry には、デプロイのたびにイメージが残る。無料枠（0.5GB、要確認）を超えないように、古いイメージを消す設定を入れておく。

```sh
cat > cleanup-policy.json <<'JSON'
[
  {"name": "keep-latest-3", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "7d"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy \
  --location=$REGION --policy=cleanup-policy.json --no-dry-run
```

## 8. 無料枠と費用の目安（要確認）

最新の値は https://cloud.google.com/free/docs/free-cloud-features で確認する。この節の値はすべて要確認。

| サービス | 無料枠（月あたり、書いた時点の理解） | この用途での使い方 |
|---|---|---|
| Cloud Run | リクエスト 200万件、vCPU と メモリの使用時間に枠あり | 1件のアップロードで、PUT 1回と内部 API 2回以上 |
| Firestore | 1日あたり 読み取り 5万、書き込み 2万、削除 2万、保存 1GiB（`(default)` のみ） | 1件あたり書き込み 10回前後 |
| Cloud Storage | Standard の保存 5GB（`us-central1` などの米国の一部リージョンのみ）、書き込みなどの操作 5,000回、読み取りの操作 5万回 | 1件あたり画像 数MB と天気データ 100KB 未満 |
| Cloud Tasks | 100万オペレーション | 1件あたり 2〜数回 |
| Artifact Registry | 保存 0.5GB | イメージ1つで数百MB。7節の設定で古いものを消す |
| Cloud Build | 1日あたりのビルド時間に枠あり | デプロイのたびに数分 |

- 最初に無料枠を超えそうなのは、Cloud Storage の保存容量。写真1枚を 3MB とすると、5GB でおよそ 1,600 枚。超えた分は、1GB あたり月に数円程度（要確認）。
- 日本から `us-central1` へのアップロード（受信）は無料。サーバーから外への送信（ダウンロード）は、この用途ではほぼ発生しない。
- 運用を始めて1か月後に、Cloud Console の「お支払い」で実際の請求額を確認する。

## 9. ユーザーが決めること

- プロジェクト ID（`sky-weather-collection` が取れるか）
- 請求先アカウントと、予算アラートの金額（例では 500 円）
- `--max-instances` の上限（例では 2）
