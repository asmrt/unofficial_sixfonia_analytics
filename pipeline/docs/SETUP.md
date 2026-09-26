# GCP セットアップ手順書

このドキュメントの手順を上から順に実行すれば、YouTube データ連携パイプラインの構築が完了します。

**推奨環境: [Cloud Shell](https://console.cloud.google.com/?cloudshell=true)**（gcloud / bq 設定済み・Linux 環境のためコマンドをそのまま貼り付け可能）。
ローカル PC の gcloud CLI でも実行できますが、その場合は事前に `gcloud auth login` が必要です。

## 構成

```text
Cloud Scheduler（01:00 JST）
  └→ Cloud Function youtube-data-fetch
        1. YouTube API から全動画の再生数などを取得
        2. GCS にチャンネル別 CSV を保存（channel 列つき。ここが唯一のデータ保管場所）
        3. 動画マスタ（videos.json）を公開URLから GCS にコピー。あわせて videos.ndjson も書く
BigQuery（外部テーブル・ビューのみ。データは持たない）
  ├ ext_video_statistics / ext_videos      … GCS の CSV・NDJSON を直接読む外部テーブル
  └ video_statistics / video_statistics_with_diff / videos … スネークケースに変換したビュー
```

- BigQuery はデータを持たず、クエリのたびに GCS を直接読みます。関数は BigQuery に一切触りません。
- CSV を差し替えれば、次のクエリから内容が反映されます。
- 移し替え処理（スケジュールクエリ・読み込みジョブ）が無いので、関数の実行やリトライが何時に終わっても取りこぼしません。
- 列名は BigQuery 側（外部テーブル・ビュー）はスネークケース、GCS 上の CSV の列名は既存のまま（camelCase）です。

## 全体の流れ

| # | 作業 | 所要目安 |
|---|---|---|
| 1 | 変数設定・API 有効化 | 5分 |
| 2 | YouTube API キー作成 | 5分 |
| 3 | GCS バケット作成 | 2分 |
| 4 | BigQuery 外部テーブル・ビュー作成 | 5分 |
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

キーをコマンドに直接書くと、Cloud Shell のコマンド履歴に残ります。次のどちらかで登録してください。

**A. コンソールで登録する**: [Secret Manager](https://console.cloud.google.com/security/secret-manager) →「シークレットを作成」→ 名前を `youtube-api-key`、シークレットの値にキーを貼り付け。
（リージョンは自動のままでよい。作成後、シークレット名が `youtube-api-key` になっていることを確認する）

**B. コマンドで登録する**: 入力欄に貼り付ける形にすると、画面にも履歴にも残りません。

```bash
read -rs -p "APIキー: " KEY; echo
printf %s "$KEY" | gcloud secrets create youtube-api-key --data-file=-
unset KEY
```

> キーをローテーションする場合は、A ならシークレットの「新しいバージョン」から、B なら
> `gcloud secrets create` を `gcloud secrets versions add youtube-api-key` に替えて、新バージョンを追加します。
> そのあと関数を再デプロイ（または新リビジョン作成）してください。

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

> **既存データの移行**: 既存の Colab 運用の CSV をこのバケットに移す場合は、
> このバケット（`$BUCKET`）を作成したあとに [docs/MIGRATION.md](MIGRATION.md) の手順で移行する。

### 3-1. バージョニングの有効化

GCS の CSV はデータの唯一の保管場所で、差し替えなどで上書き・削除することがあります。
バージョニングを有効にすると、上書き・削除する前の版が 30 日間残り、元に戻せます（手順11「CSV を差し替える」）。
30 日で自動削除されるのは古い版だけで、今のデータは削除されません。

```bash
gcloud storage buckets update gs://$BUCKET --versioning

cat > lifecycle-noncurrent.json <<'EOF'
{"rule": [{"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 30}}]}
EOF
gcloud storage buckets update gs://$BUCKET --lifecycle-file=lifecycle-noncurrent.json

# 確認（versioning_enabled: true と、上のルールが出ればOK）
gcloud storage buckets describe gs://$BUCKET --format="yaml(versioning_enabled,lifecycle_config)"
```

> バケットを作成済みの場合も、このブロックだけ実行すれば有効になります。
> 古い版が残るのは毎日上書きされる `master/videos.json` などで、費用は月数円以下です。
> 後でライフサイクルルールを足す場合は、このルールも同じファイルに含めてください（`--lifecycle-file` は設定を丸ごと置き換えます）。

## 4. BigQuery 外部テーブル・ビュー作成

このリポジトリ（`unofficial_sixfonia_analytics`）を Cloud Shell に `git clone` するかアップロードし、**`pipeline/` ディレクトリから**実行します（手順6のデプロイも同じ場所基準）。

```bash
cd ~/unofficial_sixfonia_analytics/pipeline
```

> 非公開リポジトリのため、`git clone` には GitHub の認証が必要です。`gh auth login`（画面の案内に従ってブラウザで認証）を使ってください。
> Personal Access Token を `https://<token>@github.com/...` の形で URL に埋め込むと、トークンがコマンド履歴に残るので避けてください。
> Cloud Shell へのアップロード: Cloud Shell 右上「⋮」→「アップロード」でフォルダごとアップロードできます。

データセットを作成します。

```bash
bq mk --location=$REGION --dataset $DATASET
```

リポジトリの SQL は、プロジェクトID・データセット名・バケット名を `YOUR_PROJECT_ID` / `YOUR_DATASET` / `YOUR_BUCKET` という
プレースホルダで書いています。実行時に置換して `bq` に流す関数を定義してから実行します
（ファイル自体は書き換えないので、置換後の内容がコミットされることはありません）。

```bash
run_sql() { sed -e "s/YOUR_PROJECT_ID/$PROJECT_ID/g" -e "s/YOUR_DATASET/$DATASET/g" -e "s/YOUR_BUCKET/$BUCKET/g" "$1" | bq query --use_legacy_sql=false; }

run_sql sql/01_create_external_tables.sql
run_sql sql/02_create_views.sql
```

作られるもの:

- `ext_video_statistics`（外部テーブル）: `gs://$BUCKET/*.csv` を直接読む。列は `videoId, viewCount, likeCount, commentCount, videoURL, view_date, channel`。
- `ext_videos`（外部テーブル）: `gs://$BUCKET/master/videos.ndjson` を直接読む。列は `videoId, channel, title, publishedAt, durationSec, isShort, thumbnail, tags, available`。
- `video_statistics`（ビュー）: `ext_video_statistics` をスネークケースの列名にしたもの。
- `video_statistics_with_diff`（ビュー）: `video_statistics` の全列に `view_diff`（直前の日との再生数差）と
  `diff_days`（何日前との差か。通常は 1）を足したもの。分析はこちらを読みます。
- `videos`（ビュー）: `ext_videos` をスネークケースの列名にしたもの。

> この時点ではまだ GCS にファイルが無いので（手順7で初めて書き込まれる）、ここで上記のビューにクエリを投げると
> 「一致するファイルが無い」エラーになります。想定どおりです。

## 5. サービスアカウント作成

### 5-1. Cloud Functions 実行用

関数に必要な権限だけを付けます（GCS への書き込み、API キーの読み取り）。関数は BigQuery に一切触らないので、BigQuery まわりの権限は不要です。

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
```

> BigQuery を読むのは自分のアカウント（オーナー）なので、追加の権限は不要です。Looker Studio も
> 作成者の認証情報でデータを読む設定にしていれば、追加設定は不要です。

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
  --set-env-vars="GCS_BUCKET=$BUCKET,SNAPSHOT_URL=$SNAPSHOT_URL" \
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
{"message": "Stats: 7/7 completed", "results": {"hima72": true, ...}, "snapshot_updated_at": "2026-09-19T02:21:52+09:00", "duration_seconds": 123.4}
```

GCS にファイルができているか確認します。

```bash
gcloud storage ls "gs://$BUCKET/**" | head -20
```

- `{channel}/{channel}_video_statistics_{YYYYMMDD}.csv` × 7（日付は**実行日の前日**。7列で、最後の列が `channel`）
- `master/videos.json` × 1（動画マスタ。`SNAPSHOT_URL` の `videos.json` のコピー）
- `master/videos.ndjson` × 1（`videos.json` の `videos` 配列を1行1動画にした NDJSON。BigQuery の外部テーブルが読む）

CSV の中身も1つ見て、列名の行が 7 列で、最後が `channel` になっていること、各行の末尾にチャンネル名が入っていることを確認します。

```bash
gcloud storage cat "gs://$BUCKET/hima72/hima72_video_statistics_*.csv" | head -3
```

BigQuery（外部テーブル経由）で読めるか確認します。

```bash
bq query --use_legacy_sql=false "
SELECT channel, COUNT(*) AS videos, MAX(view_date) AS latest
FROM \`$PROJECT_ID.$DATASET.video_statistics\`
GROUP BY channel ORDER BY channel"

bq query --use_legacy_sql=false "SELECT COUNT(*) FROM \`$PROJECT_ID.$DATASET.videos\`"
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

ジョブを作成します（毎日 01:00 JST）。関数の URL は手順7で変数に入れたものです。
Cloud Shell を開き直していて空になっている場合は、先に取り直してください。

```bash
STATS_URL=$(gcloud functions describe youtube-data-fetch --gen2 --region=$REGION --format='value(serviceConfig.uri)')
echo "$STATS_URL"   # https:// で始まる URL が出ることを確認

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

> 関数は一部チャンネルの取得失敗で 500 を返すため、Scheduler が最大3回リトライします。
> リトライは同じパス・同じファイル名の CSV を上書きするだけなので、重複は発生しません。
> BigQuery への移し替え処理が無いため、リトライが何時に終わっても取りこぼしは起きません。

スケジュールを待たずに動作確認する場合:

```bash
gcloud scheduler jobs run youtube-daily-stats --location=$REGION
```

## 9. 失敗通知の設定（Cloud Monitoring）

関数はエラーを severity=ERROR の構造化ログで出力するため、ログベースのアラートで失敗に気づけるようにします。コンソール操作です。

1. **通知チャネル作成**: [Monitoring → アラート → Edit notification channels](https://console.cloud.google.com/monitoring/alerting/notifications) → 「Email」の「ADD NEW」→ 自分のメールアドレスを登録
2. **アラートポリシー作成**: [ログエクスプローラ](https://console.cloud.google.com/logs/query) で以下のクエリを入力

   ```text
   (resource.type="cloud_run_revision" AND resource.labels.service_name="youtube-data-fetch" AND severity>=ERROR)
   OR (resource.type="cloud_scheduler_job" AND severity>=ERROR)
   ```

   2 行目は、Scheduler が関数を呼べなかった場合（権限の設定漏れ・URL の間違いなど）を拾うためのものです。
   この場合は関数が一度も動かないので、関数のログにはエラーが出ません。

3. クエリ結果上部の「アクション」→「ログアラートを作成」で以下を設定して保存:
   - ポリシー名: `youtube-fetch-error-alert`
   - 通知の頻度: 30分（同一障害での連続通知を抑制）
   - 通知チャネル: 手順1で作成したメール

> これで「一部チャンネルの取得失敗」「動画マスタのコピー失敗」「関数自体のクラッシュ」「Scheduler が関数を呼べない」をこの 1 つのアラートで拾えます。
> 関数の失敗は関数のログ（`gcloud functions logs read ...`）、呼び出しの失敗は Scheduler のログで確認します。
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
01:00 JST  youtube-data-fetch  → GCS に前日分 CSV（channel列つき）、動画マスタ（videos.json / videos.ndjson）をコピー

（別系統）動画マスタ videos.json は別システムが毎日生成（01:00 の時点で前日版のことがある）
```

BigQuery は上記の GCS を外部テーブル経由でそのつど読むだけで、移し替え処理はありません。

### よく使う確認コマンド

```bash
# 関数ログ
gcloud functions logs read youtube-data-fetch --gen2 --region=$REGION --limit=50

# スケジューラの実行履歴
gcloud scheduler jobs list --location=$REGION

# 前日比増分が大きい動画 Top 20（タイトルを付ける場合は videos を JOIN）
bq query --use_legacy_sql=false "
SELECT s.channel, s.video_id, v.title, s.view_count, s.view_diff, s.video_url
FROM \`$PROJECT_ID.$DATASET.video_statistics_with_diff\` AS s
LEFT JOIN \`$PROJECT_ID.$DATASET.videos\` AS v USING (video_id)
WHERE s.view_date = DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 1 DAY)
ORDER BY s.view_diff DESC
LIMIT 20"
```

### CSV を差し替える

GCS 上の CSV が唯一のデータなので、間違いに気づいたら同じパス・同じファイル名で上書きアップロードすれば、次のクエリから反映されます。

```bash
gcloud storage cp 修正版.csv "gs://$BUCKET/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv"
```

- 列の並び（7列: `videoId, viewCount, likeCount, commentCount, videoURL, view_date, channel`）は変えないこと。外部テーブルは位置で列を読みます。
- 壊れた（列数が合わない・型が合わないなど）CSV が1つでも `gs://$BUCKET/*.csv` に混じると、`video_statistics` へのクエリ全体がエラーになります。差し替え後は一度クエリして確認してください。

#### 30 日以内の差し替えを取り消す

バージョニングが有効なら、誤った上書きや削除を 30 日以内に復旧できます。

```bash
# 過去の版を一覧（末尾の #数字 が版の番号）
gcloud storage ls -a "gs://$BUCKET/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv"
# 指定した版で元に戻す
gcloud storage cp "gs://$BUCKET/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv#<版の番号>" "gs://$BUCKET/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv"
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
| BigQuery | クエリのたびに全 CSV を読む（1年で約120万行・数百MB）。無料枠（1TB/月スキャン）に対して十分小さい |
| Cloud Scheduler | 3ジョブまで無料 |
| Secret Manager | 無料枠内 |
| YouTube Data API | 無料（クォータ内） |

実質ほぼ 0 円で運用できます（予算アラートで想定外の課金を見張ります）。
