# CD：GitHub Actions から Cloud Run へのデプロイ

`v` で始まるタグ（例：`v0.1.0`）を push すると、GitHub Actions がテストを流し、サーバーのイメージをビルドして、ステージング（`sky-weather-stg`）の Cloud Run に出す。ワークフローは `.github/workflows/deploy.yml`。

GCP の初回の準備（`docs/deploy-gcp.md` の 0〜6節）が終わっていることが前提。この文書の 2節は、その後に1回だけ行う。

## 1. 仕組み

| 段階 | 内容 |
|---|---|
| きっかけ | `v*` のタグの push だけ。ブランチへの push や PR では出さない |
| テスト | `ci.yml` を同じワークフローから呼ぶ（`workflow_call`）。落ちたらデプロイしない |
| 出してよいコミットか | タグの指すコミットが `main` に含まれていなければ止める（作業ブランチに打ったタグからは出さない） |
| GCP への認証 | Workload Identity 連携。GitHub が発行する一時的な ID トークンを GCP が信頼し、サービスアカウント `sky-deployer` として動く。鍵ファイル（JSON）は作らない |
| ビルド | Actions の中で `docker build`（`server/Dockerfile`）し、Artifact Registry の `sky` リポジトリに、コミットの SHA をタグにして push する |
| デプロイ | `gcloud run deploy --image` のあと、トラフィックを新しいリビジョンに全部向ける（`update-traffic --to-latest`）。環境変数やインスタンス数などの設定は前のリビジョンのものが引き継がれる。GitHub の Environment `staging` として記録される（初回に自動で作られる） |
| 確認 | デプロイ後に `GET /health` が 200 を返すか確かめる |

### 1.1 認証の条件（二重に絞る）

- GCP 側（プロバイダの属性条件）：トークンの `repository_id` がこのリポジトリの ID（`1214255489`）で、`ref` が `refs/tags/v` で始まるときだけ受け付ける。リポジトリ名ではなく ID で見るのは、名前が変わったり、同じ名前のリポジトリが作り直されたりしても、別のリポジトリを信頼しないため。
- GitHub 側（ワークフロー）：上の「出してよいコミットか」の検査。

### 1.2 `sky-deployer` の権限

| どの範囲で | ロール | 何のため |
|---|---|---|
| Artifact Registry のリポジトリ `sky` | `roles/artifactregistry.writer` | イメージの push |
| Cloud Run のサービス `sky-server` | `roles/run.developer` | 新しいリビジョンを出す |
| サービスアカウント `sky-server` | `roles/iam.serviceAccountUser` | `sky-server` として動くリビジョンを出す |

- プロジェクト全体のロールは付けない。Firestore や Cloud Storage のデータには触れない。
- `sky-deployer` を使えるのは、1.1節の条件を満たす GitHub のワークフローだけ（`roles/iam.workloadIdentityUser`）。

## 2. GCP の準備（1回だけ）

Cloud Shell で、`source ~/sky-env.sh` をしてから実行する。

```sh
cat >> ~/sky-env.sh <<'EOF'
export GITHUB_REPO_ID=1214255489   # HiroItozzz/sky-weather-collection のリポジトリ ID
export AR_REPO=sky
export DEPLOY_SA=sky-deployer@${PROJECT_ID}.iam.gserviceaccount.com
EOF
source ~/sky-env.sh

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com
```

リポジトリ ID は `https://api.github.com/repos/HiroItozzz/sky-weather-collection` の `id` で確かめられる。

### 2.1 Artifact Registry

```sh
gcloud artifacts repositories create $AR_REPO \
  --repository-format=docker --location=$REGION \
  --description="sky-server のイメージ（CD）"

# 古いイメージを消す（無料枠 0.5GB を超えないように。新しい3つは残す）
cat > cleanup-policy.json <<'JSON'
[
  {"name": "keep-latest-3", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "7d"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies $AR_REPO \
  --location=$REGION --policy=cleanup-policy.json --no-dry-run
```

### 2.2 サービスアカウントと権限

```sh
gcloud iam service-accounts create sky-deployer --display-name="sky-deployer (GitHub Actions)"

gcloud artifacts repositories add-iam-policy-binding $AR_REPO --location=$REGION \
  --member=serviceAccount:$DEPLOY_SA --role=roles/artifactregistry.writer

gcloud run services add-iam-policy-binding $SERVICE --region=$REGION \
  --member=serviceAccount:$DEPLOY_SA --role=roles/run.developer

gcloud iam service-accounts add-iam-policy-binding $RUN_SA \
  --member=serviceAccount:$DEPLOY_SA --role=roles/iam.serviceAccountUser
```

### 2.3 Workload Identity 連携

```sh
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc sky-weather-collection \
  --location=global --workload-identity-pool=github \
  --display-name="sky-weather-collection" \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository_id=='${GITHUB_REPO_ID}' && assertion.ref.startsWith('refs/tags/v')"

export POOL_NAME=$(gcloud iam workload-identity-pools describe github --location=global --format='value(name)')
gcloud iam service-accounts add-iam-policy-binding $DEPLOY_SA \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL_NAME}/attribute.repository_id/${GITHUB_REPO_ID}"

# ワークフローに書く値（プロバイダの名前）
gcloud iam workload-identity-pools providers describe sky-weather-collection \
  --location=global --workload-identity-pool=github --format='value(name)'
```

最後に表示される `projects/{プロジェクト番号}/locations/global/workloadIdentityPools/github/providers/sky-weather-collection` を、`deploy.yml` の `WIF_PROVIDER` に書く。秘密の値ではないので、ワークフローにそのまま書いてよい。

## 3. 使い方

`main` に入ったコミットにタグを打って push する。

```sh
git checkout main
git pull
git tag v0.1.0
git push origin v0.1.0
```

- 進み具合は GitHub の Actions タブで見る。
- タグの名前は `v{大}.{中}.{小}` にする。同じタグは2回使えない。出し直すときは番号を上げる。
- 動いているイメージがどのコミットのものかは、`gcloud run services describe $SERVICE --region=$REGION --format='value(spec.template.spec.containers[0].image)'` の末尾（コミットの SHA）で分かる。

### 3.1 元に戻す

前のリビジョンにトラフィックを戻す。

```sh
gcloud run revisions list --service=$SERVICE --region=$REGION
gcloud run services update-traffic $SERVICE --region=$REGION --to-revisions={戻したいリビジョン}=100
```

次にタグでデプロイすると、ワークフローがトラフィックを新しいリビジョンに戻す（`update-traffic --to-latest`）。

## 4. うまくいかないとき

- 認証（`google-github-actions/auth`）で失敗する：プロバイダの名前、属性条件（タグが `v` で始まっているか）、`workloadIdentityUser` の付け先を確かめる。
- デプロイで権限のエラーになる：エラーに出る権限を確かめる。サービス単位の `run.developer` で足りない操作が求められたときは、何の権限かを確かめてから、必要な範囲だけ足す。
- 「タグが main に含まれていない」で止まる：タグを消して（`git push origin :refs/tags/v0.1.0`、`git tag -d v0.1.0`）、`main` のコミットに打ち直す。
