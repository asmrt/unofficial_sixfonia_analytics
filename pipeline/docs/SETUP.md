# GCP セットアップ手順書

このドキュメントの手順を上から順に実行すれば、YouTube データ連携パイプラインの構築が完了します。

**推奨環境: [Cloud Shell](https://console.cloud.google.com/?cloudshell=true)**（gcloud / bq 設定済み・Linux 環境のためコマンドをそのまま貼り付け可能）。
ローカル PC の gcloud CLI でも実行できますが、その場合は事前に `gcloud auth login` が必要です。

## 構成

```text
Cloud Scheduler（01:00 JST）
  └→ Cloud Function youtube-data-fetch
        1. YouTube API から全動画の再生数などを取得
        2. GCS にチャンネル別 CSV を保存（生データの保管用）
        3. BigQuery テーブルに前日分を読み込み（取得できたチャンネルの前日分を消してから追加）
        4. 動画マスタ（videos.json）を公開URLから GCS にコピー
BigQuery
  ├ video_statistics            … 日次スナップショット（view_date で日付パーティション）
  └ video_statistics_with_diff  … 前日比（view_diff）をその場で計算するビュー
```

- 取得から BigQuery への読み込みまで 1 つの関数で完結します。スケジュールクエリや外部テーブルは使いません。
- 読み込みは「その日・そのチャンネルの行を消してから追加する」方式なので、何度リトライしても重複しません。取得に失敗したチャンネルの行は触らないので、前の試行で入った分も消えません。
- 前日比はテーブルに保存せず、ビューで直前の日との差を計算します（欠けた日があっても次の日の値が出ます）。

## 全体の流れ

| # | 作業 | 所要目安 |
|---|---|---|
| 1 | 変数設定・API 有効化 | 5分 |
| 2 | YouTube API キー作成 | 5分 |
| 3 | GCS バケット作成 | 2分 |
| 4 | BigQuery テーブル・ビュー作成 | 5分 |
| 5 | サービスアカウント作成 | 3分 |
| 6 | Cloud Functions デプロイ | 5分 |
| 7 | 動作確認（手動実行） | 5分 |
| 8 | Cloud Scheduler 登録 | 5分 |
| 9 | 失敗通知の設定 | 5分 |
| 10 | 予算アラートの設定 | 3分 |

## 1. 変数設定・API 有効化

Cloud Shell を開き、以下を実行します（**セッションを開き直したら再実行が必要**）。

```bash
export PROJECT_ID="<GCPのプロジェクトID>"
export REGION="asia-northeast1"
export BUCKET="youtube-metrics-bucket"
export DATASET="youtube_stat"
export SNAPSHOT_URL="<動画マスタ videos.json の公開URL>"

gcloud config set project $PROJECT_ID
```

> `PROJECT_ID` と `SNAPSHOT_URL` はリポジトリには書かず、ここで設定した値をデプロイ時に渡します。

必要な API を有効化します。

```bash
gcloud services enable \
  youtube.googleapis.com \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  bigquery.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  monitoring.googleapis.com
```

- `run` / `artifactregistry` / `cloudbuild` は Cloud Functions 第2世代の実行基盤として必要。
- `secretmanager` は API キーの安全な保管（手順2）、`monitoring` は失敗通知（手順9）に必要。

## 2. YouTube API キー作成

コンソール操作です。

1. [API とサービス → 認証情報](https://console.cloud.google.com/apis/credentials) を開く
2. 「認証情報を作成」→「API キー」
3. 作成されたキーの「キーを制限」を開き、
   - **API の制限**: 「キーを制限」→ **YouTube Data API v3** のみにチェック
   - アプリケーションの制限: なし（サーバー間通信のため）
4. キーの値を控える

キーは環境変数ではなく **Secret Manager** に保管します（コンソールの関数詳細画面に平文で表示されるのを防ぐため）。

```bash
echo -n "ここにAPIキーを貼り付け" | gcloud secrets create youtube-api-key --data-file=-
```

> キーをローテーションする場合は `gcloud secrets versions add youtube-api-key --data-file=-` で
> 新バージョンを追加し、関数を再デプロイ（または新リビジョン作成）してください。

> **補足（クォータ）**: YouTube Data API の無料クォータは 10,000 ユニット/日。
> 本システムの消費は約 150 ユニット/日です（動画 約3,400本 ÷ 50件 × 2種類の list 呼び出し × 1 ユニット + チャンネル7件）。
> 動画マスタ（`videos.json`）は生成側が別の API キーで取得しており、この関数では API を使いません。

## 3. GCS バケット作成

BigQuery のデータセットと同じリージョン（`asia-northeast1`）に作成します。

```bash
gcloud storage buckets create gs://$BUCKET \
  --location=$REGION \
  --uniform-bucket-level-access
```

> バケット名はグローバル一意です。`youtube-metrics-bucket` が取得できない場合は
> 別名（例: `youtube-metrics-bucket-<任意の接尾辞>`）にし、`$BUCKET` を変更してください。

> **保存期間**: 当面は期限なしで保存します。YouTube API 規約の確認（`IMPROVEMENTS.md` の A）の結果、
> 保存期間を区切る必要が出た場合は、ライフサイクルルールで古い CSV を自動削除できます
> （例: 30日で削除 → `gcloud storage buckets update gs://$BUCKET --lifecycle-file=lifecycle.json`）。

## 4. BigQuery テーブル・ビュー作成

このリポジトリ（`unofficial_sixfonia_analytics`）を Cloud Shell に `git clone` するかアップロードし、**`pipeline/` ディレクトリから**実行します（手順6のデプロイも同じ場所基準）。

```bash
cd ~/unofficial_sixfonia_analytics/pipeline
```

> 非公開リポジトリのため、`git clone` には GitHub の認証（`gh auth login` または Personal Access Token）が必要です。
> Cloud Shell へのアップロード: Cloud Shell 右上「⋮」→「アップロード」でフォルダごとアップロードできます。

データセットを作成します。

```bash
bq mk --location=$REGION --dataset $DATASET
```

リポジトリの SQL は、プロジェクトIDとデータセット名を `YOUR_PROJECT_ID` / `YOUR_DATASET` という
プレースホルダで書いています。実行時に置換して `bq` に流す関数を定義してから実行します
（ファイル自体は書き換えないので、置換後の内容がコミットされることはありません）。

```bash
run_sql() { sed -e "s/YOUR_PROJECT_ID/$PROJECT_ID/g" -e "s/YOUR_DATASET/$DATASET/g" "$1" | bq query --use_legacy_sql=false; }

run_sql sql/01_create_table.sql
run_sql sql/02_create_view.sql
```

- `video_statistics`: 日次スナップショット。`view_date` で日付パーティション。
- `video_statistics_with_diff`: `video_statistics` の全列に `view_diff`（直前の日との再生数差）と
  `diff_days`（何日前との差か。通常は 1）を足したビュー。分析はこちらを読みます。

> **保存期間**: テーブルも当面は期限なしです。規約確認の結果、区切る必要が出た場合は
> `bq update --time_partitioning_expiration <秒数> $PROJECT_ID:$DATASET.video_statistics`
> で、期限を過ぎたパーティション（日）が自動で削除されるようにできます。

## 5. サービスアカウント作成

### 5-1. Cloud Functions 実行用

関数に必要な権限だけを付けます（GCS への書き込み、API キーの読み取り、BigQuery のテーブル1つへの書き込み）。

```bash
gcloud iam service-accounts create youtube-fetch-sa \
  --display-name="YouTube fetch functions runtime"

SA="youtube-fetch-sa@$PROJECT_ID.iam.gserviceaccount.com"

# GCS: このバケットだけに書き込める
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$SA" \
  --role="roles/storage.objectAdmin"

# Secret Manager: API キーだけを読める
gcloud secrets add-iam-policy-binding youtube-api-key \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"

# BigQuery: このテーブルだけに書き込める（手順4で作成済みのテーブルに付与する）
bq add-iam-policy-binding \
  --member="serviceAccount:$SA" \
  --role="roles/bigquery.dataEditor" \
  $PROJECT_ID:$DATASET.video_statistics

# BigQuery: 読み込みジョブを実行できる（ジョブの実行権限はプロジェクト単位でしか付けられない）
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" \
  --role="roles/bigquery.jobUser" \
  --condition=None
```

### 5-2. Cloud Scheduler 起動用（関数の呼び出し権限のみ）

```bash
gcloud iam service-accounts create scheduler-invoker \
  --display-name="Cloud Scheduler invoker"
```

（関数への `run.invoker` 付与はデプロイ後の手順8で行います）

## 6. Cloud Functions デプロイ

`pipeline/` ディレクトリから実行します。

```bash
cd youtube-data-fetch

gcloud functions deploy youtube-data-fetch \
  --gen2 \
  --region=$REGION \
  --runtime=python312 \
  --source=. \
  --entry-point=main \
  --trigger-http \
  --no-allow-unauthenticated \
  --service-account="youtube-fetch-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --set-env-vars="GCS_BUCKET=$BUCKET,BQ_TABLE=$PROJECT_ID.$DATASET.video_statistics,SNAPSHOT_URL=$SNAPSHOT_URL" \
  --set-secrets="YOUTUBE_API_KEY=youtube-api-key:latest" \
  --memory=512Mi \
  --timeout=540s

cd ..
```

> `--no-allow-unauthenticated` により、URL を知っていても認証なしでは実行できません
> （API キーの無駄消費・不正実行の防止）。

## 7. 動作確認（手動実行）

関数の URL を取得し、自分の認証トークンで叩きます。

```bash
STATS_URL=$(gcloud functions describe youtube-data-fetch --gen2 --region=$REGION --format='value(serviceConfig.uri)')

# 統計取得（約3,400本のため数分かかる）
curl -m 600 -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$STATS_URL"
```

期待レスポンス（例）:

```json
{"message": "Stats: 7/7 completed", "results": {"hima72": true, ...}, "bq_rows": 3443, "snapshot_updated_at": "2026-09-19T02:21:52+09:00", "duration_seconds": 123.4}
```

GCS にファイルができているか確認します。

```bash
gcloud storage ls "gs://$BUCKET/**" | head -20
```

- `{channel}/{channel}_video_statistics_{YYYYMMDD}.csv` × 7（日付は**実行日の前日**）
- `master/videos.json` × 1（動画マスタ。`SNAPSHOT_URL` の `videos.json` のコピー）

BigQuery に入っているか確認します。

```bash
bq query --use_legacy_sql=false "
SELECT channel, COUNT(*) AS videos, MAX(view_date) AS latest
FROM \`$PROJECT_ID.$DATASET.video_statistics\`
GROUP BY channel ORDER BY channel"
```

7 チャンネル分の行数が出れば成功です（初日は前の日のデータが無いため、ビューの `view_diff` は全行 NULL。2日目から値が入ります）。

失敗したチャンネルがある場合はログを確認します。

```bash
gcloud functions logs read youtube-data-fetch --gen2 --region=$REGION --limit=50
```

## 8. Cloud Scheduler 登録

まず scheduler-invoker に関数（= Cloud Run サービス）の起動権限を付与します。

```bash
gcloud run services add-iam-policy-binding youtube-data-fetch \
  --region=$REGION \
  --member="serviceAccount:scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

ジョブを作成します（毎日 01:00 JST）。

```bash
gcloud scheduler jobs create http youtube-daily-stats \
  --location=$REGION \
  --schedule="0 1 * * *" \
  --time-zone="Asia/Tokyo" \
  --uri="$STATS_URL" \
  --http-method=GET \
  --oidc-service-account-email="scheduler-invoker@$PROJECT_ID.iam.gserviceaccount.com" \
  --attempt-deadline=600s \
  --max-retry-attempts=3 \
  --min-backoff=5m
```

> 関数は一部チャンネルの失敗や BigQuery への読み込み失敗で 500 を返すため、Scheduler が最大3回リトライします。
> GCS は同名ファイルの上書き、BigQuery は「その日・そのチャンネルの行を消してから追加」なので、リトライしても重複しません。
> 取り込みも同じ関数の中で行うため、リトライが何時に終わっても取りこぼしは起きません。

スケジュールを待たずに動作確認する場合:

```bash
gcloud scheduler jobs run youtube-daily-stats --location=$REGION
```

## 9. 失敗通知の設定（Cloud Monitoring）

関数はエラーを severity=ERROR の構造化ログで出力するため、ログベースのアラートで失敗に気づけるようにします。コンソール操作です。

1. **通知チャネル作成**: [Monitoring → アラート → Edit notification channels](https://console.cloud.google.com/monitoring/alerting/notifications) → 「Email」の「ADD NEW」→ 自分のメールアドレスを登録
2. **アラートポリシー作成**: [ログエクスプローラ](https://console.cloud.google.com/logs/query) で以下のクエリを入力

   ```text
   resource.type="cloud_run_revision"
   resource.labels.service_name="youtube-data-fetch"
   severity>=ERROR
   ```

3. クエリ結果上部の「アクション」→「ログアラートを作成」で以下を設定して保存:
   - ポリシー名: `youtube-fetch-error-alert`
   - 通知の頻度: 30分（同一障害での連続通知を抑制）
   - 通知チャネル: 手順1で作成したメール

> これで「一部チャンネルの取得失敗」「BigQuery への読み込み失敗」「動画マスタのコピー失敗」「関数自体のクラッシュ」をすべてこの 1 つのアラートで拾えます。
> 動画マスタのコピー失敗は統計の取得とは独立なので、関数は 200 を返し、Scheduler のリトライは起きません（翌日の実行で最新版に上書きされます）。

## 10. 予算アラートの設定

設定ミスなどで想定外の課金が発生したときに気づけるようにします。コンソール操作です。

1. [お支払い → 予算とアラート](https://console.cloud.google.com/billing/budgets) を開き「予算を作成」
2. 以下を設定して保存:
   - 名前: `youtube-pipeline-budget`
   - 対象: このプロジェクト
   - 金額: 月額 **500 円**（想定は実質 0 円。これを超えたら何かがおかしい）
   - アラートのしきい値: 50% / 90% / 100%
   - 通知: 「請求先アカウント管理者とユーザーにメールで通知する」にチェック

> 予算アラートは**通知だけ**で、課金を止めはしません。通知が来たら、関数の実行回数やログを確認してください。

## 11. 日常運用

### 毎日のフロー（すべて自動）

```text
01:00 JST  youtube-data-fetch  → GCS に前日分 CSV、BigQuery に前日分を読み込み、動画マスタ（videos.json）をコピー

（別系統）動画マスタ videos.json は別システムが毎日生成（01:00 の時点で前日版のことがある）
```

### よく使う確認コマンド

```bash
# 関数ログ
gcloud functions logs read youtube-data-fetch --gen2 --region=$REGION --limit=50

# スケジューラの実行履歴
gcloud scheduler jobs list --location=$REGION

# 前日比増分が大きい動画 Top 20
bq query --use_legacy_sql=false "
SELECT channel, videoId, viewCount, view_diff, videoURL
FROM \`$PROJECT_ID.$DATASET.video_statistics_with_diff\`
WHERE view_date = DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 1 DAY)
ORDER BY view_diff DESC
LIMIT 20"
```

### チャンネルの追加・削除

`sixfonia_analytics/config.py` と `pipeline/youtube-data-fetch/channels.json` の両方を編集し（不一致はテストで検出されます）、手順6のデプロイコマンドを再実行します。

### 障害時の手動リカバリ

01:00 の実行がリトライも含めて全部失敗した場合は、**その日のうちに**関数を手動実行します（手順7の `curl`、または `gcloud scheduler jobs run`）。
関数は常に「実行日の前日」の分として保存するので、同じ日のうちなら正しい日付で入ります。

- 日付が変わってしまうと、その日の分は取り直せません（YouTube API は過去時点の再生数を返さないため）。
- 欠けた日があっても、ビューの `view_diff` は直前にある日との差を返します（`diff_days` が 2 以上になります）。

## コスト目安

| サービス | 見込み |
|---|---|
| Cloud Functions | 1日1回・数分程度 → 無料枠内 |
| Cloud Storage | CSV 約 200KB/日 + JSON → 月数円以下 |
| BigQuery | ストレージ・クエリとも無料枠内（10GB / 1TB スキャン） |
| Cloud Scheduler | 3ジョブまで無料 |
| Secret Manager | 無料枠内 |
| YouTube Data API | 無料（クォータ内） |

実質ほぼ 0 円で運用できます（予算アラートで想定外の課金を見張ります）。
