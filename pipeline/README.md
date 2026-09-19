# YouTube データ連携自動化 設計書

## 1. システム概要

YouTube チャンネルの動画統計情報（再生数など）を毎日自動収集し、BigQuery に蓄積するパイプライン。
動画のタイトルなどの動画マスタは、別システムが毎日生成して公開URLで配信している `videos.json` を唯一の生成元とし、
このパイプラインはそのコピーを GCS に置くだけにする（同じ情報を2か所で API から取得しない）。

```text
YouTube Data API v3
      │
      └─ [youtube-data-fetch]   毎日の再生数・いいね数を取得（01:00 JST）
               │
               ├──→ GCS {channel}/*.csv          統計データ（channel列つき。ここが唯一のデータ保管場所）
               ├──→ GCS master/videos.json       動画マスタのコピー（毎日上書き）
               └──→ GCS master/videos.ndjson     動画マスタを1行1動画にした NDJSON（BigQuery 外部テーブル用）
                        ↑
               公開URL（SNAPSHOT_URL）の videos.json を取得（YouTube API は使わない）
                        ↑
               動画マスタの生成（別システム）

BigQuery（データは持たず GCS を直接読む）
  ├─ ext_video_statistics / ext_videos              外部テーブル（GCS の CSV・NDJSON をそのまま読む）
  └─ video_statistics / video_statistics_with_diff / videos
       スネークケースの列名に変換したビュー（video_statistics_with_diff は view_diff もその場で計算）
```

関数は GCS への書き込みだけを行い、BigQuery には一切触らない。BigQuery 側は外部テーブルとビューのみで、
CSV・NDJSON を差し替えれば次のクエリから反映される（移し替え処理・スケジュールクエリは無い）。

> Colab ノートブックで手動で行っている日次収集を自動化するためのもの。当面は既存の Colab 運用と並行して動かす。
> 既存の Colab 運用とは出力先も CSV の列も異なる（既存: 別バケット・6列 / 本パイプライン: `youtube-metrics-bucket`・`thumbnail` と `channel` を加えた8列。列名は既存と同じ）。
> 既存の分析をこちらの出力に切り替える方法は未決定（[docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) を参照）。

### リポジトリ構成

```text
pipeline/
├── README.md                            本書（設計書）
├── docs/
│   ├── SETUP.md                         GCP セットアップ手順書
│   ├── IMPROVEMENTS.md                  今後の改善点・未決事項
│   └── legacy_implementation_guide.md   旧実装ガイド（参照用）
├── sql/
│   ├── 01_create_external_tables.sql     GCS を直接読む外部テーブル作成 DDL
│   └── 02_create_views.sql               スネークケース変換・前日比（view_diff）のビュー作成 DDL
└── youtube-data-fetch/                  Cloud Function: 統計取得 ＋ 動画マスタのコピー
    ├── main.py
    ├── requirements.txt
    └── channels.json

テスト: リポジトリ直下の tests/test_pipeline.py（pip install -e ".[dev]" && pytest tests -q）
```

**セットアップは [docs/SETUP.md](docs/SETUP.md) の手順に従うこと。**

---

## 2. コンポーネント一覧

| コンポーネント | 種別 | 役割 |
|---|---|---|
| `youtube-data-fetch` | Cloud Functions (gen2) | 動画統計情報（再生数等）の日次取得・GCS保存、動画マスタのコピー（01:00 JST）。BigQuery には触れない |
| 動画マスタ（`videos.json`） | 別システム | 毎日生成され、公開URLで配信される。本パイプラインはコピーするだけ |
| GCS バケット | Cloud Storage | CSV/JSON/NDJSON の保管。唯一のデータ保管場所（BigQuery はここを直接読む） |
| `ext_video_statistics` / `ext_videos` | BigQuery 外部テーブル | GCS の CSV・NDJSON をそのまま読む（`sql/01`） |
| `video_statistics` / `videos` | BigQuery ビュー | 外部テーブルの列名をスネークケースに変換（`sql/02`） |
| `video_statistics_with_diff` | BigQuery ビュー | `video_statistics` に `view_diff`・`diff_days` を足したビュー。分析はこちらを読む（`sql/02`） |
| Cloud Scheduler | Cloud Scheduler | Function を OIDC 認証付き HTTP で起動 |

### 対象チャンネル

`channels.json` で管理。`sixfonia_analytics/config.py` と同じ内容であることを `tests/test_pipeline.py` で検査している。

```json
{
  "channels": [
    {"id": "UCr24Ll7IT2hPquu-n11dNWQ", "name": "hima72"},
    ...
  ]
}
```

---

## 3. youtube-data-fetch

### 概要

チャンネルごとの全動画の再生数・いいね数・コメント数を毎日取得し、GCS に CSV として保存する。BigQuery への読み込みは行わない。

### 処理フロー

```text
main(request)
  │
  ├─ view_date = 実行日(JST)の前日
  ├─ load_channels()               channels.json を読み込み
  ├─ fetch_channel_data() × チャンネル数
  │     │
  │     ├─ get_channel_uploads_playlist_id()   アップロードPLID取得
  │     ├─ get_videos_from_playlist()           動画ID一覧取得（ページネーション対応）
  │     ├─ get_video_statistics()               統計情報取得（50件ずつバッチ）
  │     └─ save_to_gcs()                        CSV保存
  └─ copy_snapshot_to_gcs()        動画マスタのコピー（失敗しても統計の結果には影響しない）
```

### 関数仕様

| 関数 | 引数 | 戻り値 | 説明 |
|---|---|---|---|
| `load_channels()` | — | `list[tuple[str, str]]` | channels.json からチャンネルID・名前を読み込む |
| `get_channel_uploads_playlist_id(channel_id)` | str | str | YouTube API でアップロードプレイリストIDを取得 |
| `get_videos_from_playlist(playlist_id)` | str | `list[str]` | プレイリストから全動画IDを取得（ページネーション） |
| `get_video_statistics(video_ids)` | `list[str]` | `list[dict]` | 動画統計情報を50件ずつバッチ取得 |
| `save_to_gcs(channel_name, filename, data)` | — | — | CSV形式でGCSに保存 |
| `fetch_channel_data(channel_id, channel_name, view_date)` | — | bool | チャンネル1件分の取得と CSV 保存。成功時 True、失敗時 False |
| `copy_snapshot_to_gcs()` | — | str | 動画マスタを公開URLから取得して `videos.json`・`videos.ndjson` を GCS に上書き保存。戻り値は `updated_at` |
| `main(request)` | HTTP Request | `dict, int` | Cloud Functionエントリーポイント |

### GCS 保存先

```text
gs://{GCS_BUCKET}/{channel_name}/{channel_name}_video_statistics_{YYYYMMDD}.csv
```

**CSV カラム:**

| カラム | 型 | 説明 |
|---|---|---|
| videoId | str | 動画ID |
| viewCount | str | 再生数 |
| likeCount | str | いいね数 |
| commentCount | str | コメント数 |
| videoURL | str | 動画URL |
| thumbnail | str | サムネイルURL |
| view_date | str | 計測日 (YYYYMMDD, JST, 実行日の前日) |
| channel | str | チャンネル名（channels.json の name） |

外部テーブル `ext_video_statistics`（`sql/01`）はこの列を**位置**で読むため、列の並びを変える場合は SQL 側も合わせて変更する。

### BigQuery（外部テーブル＋ビュー）

BigQuery はデータを持たず、GCS の CSV・NDJSON を外部テーブルとして直接読む。

- `ext_video_statistics`: `gs://{GCS_BUCKET}/*.csv` を読む外部テーブル（`master/` 配下の JSON・NDJSON はマッチしない）
- `ext_videos`: `gs://{GCS_BUCKET}/master/videos.ndjson` を読む外部テーブル
- `video_statistics` / `videos`: 上記2つの列名をスネークケース（`video_id`, `view_count` など）に変換するビュー
- `video_statistics_with_diff`: `video_statistics` に `view_diff`（直前の計測日との差）・`diff_days`（何日前との差か）を足したビュー。分析はこちらを読む

CSV・NDJSON を GCS 上で差し替えれば、次にビューへクエリを投げたときから内容が反映される（読み込みジョブ・スケジュールクエリは無い）。
`view_diff`（前日比）は保存せず、ビューが `LAG` で直前の計測日との差を計算する。欠けた日があっても次の日の値が出る（`diff_days` が 2 以上になる）。

---

## 4. 動画マスタ（videos.json）のコピー

### 方針

動画のタイトル・投稿日・長さ・Shorts 判定・タグ・公開状態は、別システムが毎日生成する `videos.json` を唯一の生成元とする。
ほかの用途でも同じ `videos.json` が使われているため、生成元を GCP 側に増やすと内容が食い違う。

本パイプラインは、`youtube-data-fetch` の実行時に公開URL（環境変数 `SNAPSHOT_URL`）の `videos.json` を取得し、GCS にそのままコピーする。
あわせて、`videos` 配列を1行1動画の NDJSON に変換した `videos.ndjson` も書く（BigQuery の外部テーブル `ext_videos` が読む形式のため）。
BigQuery でランキングにタイトルを付けたい場合は、ビュー `videos` を読む。

### 保存先

```text
gs://{GCS_BUCKET}/master/videos.json      （毎日上書き。中身は公開URLの videos.json と同一）
gs://{GCS_BUCKET}/master/videos.ndjson    （毎日上書き。videos 配列を1行1動画にしたもの。キーは変換しない）
```

`videos.json` の中身は `{"updated_at": ..., "channels": [...], "videos": [...]}` 形式の1つの JSON オブジェクト。

### 注意点

- 生成側の更新が遅れると、01:00 JST のコピー時点では前日版の `videos.json` になる。前日夜の新着動画は、翌日のコピーで入る
- 取得した内容が動画マスタとして不正（JSON でない、`videos` が空など）なら保存しない。
  エラーページなどで GCS 上の正しいファイルを上書きしないため
- コピーの失敗は ERROR ログを出すだけで、関数のステータスは統計の結果だけで決める（統計の再取得を起こさないため）

---

## 5. 共通仕様

### 環境変数

| 変数名 | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `YOUTUBE_API_KEY` | **必須** | — | YouTube Data API v3 キー。Secret Manager（`youtube-api-key`）から `--set-secrets` でマウント。未設定時は起動時に `RuntimeError` |
| `GCS_BUCKET` | 任意 | `youtube-metrics-bucket` | GCSバケット名 |
| `SNAPSHOT_URL` | **必須** | — | 動画マスタ（`videos.json`）の公開URL。デプロイ時に渡す。未設定時は起動時に `RuntimeError` |

### ログ出力

severity 付き構造化JSON（`CloudLoggingFormatter`）で stdout に出力する。
Cloud Logging が severity を認識するため、`severity>=ERROR` のログベースアラート（SETUP.md 手順9）で失敗通知できる。

### タイムゾーン

すべての日付処理は **JST (UTC+9)** 基準。

```python
JST = datetime.timezone(datetime.timedelta(hours=9))
view_date = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y%m%d")
```

### YouTube API レート制限対策

50件ずつバッチリクエストし、リクエスト間に `time.sleep(0.1)` を挿入。

### エラーハンドリング方針

| ケース | 挙動 |
|---|---|
| `YOUTUBE_API_KEY` / `SNAPSHOT_URL` 未設定 | 起動時に `RuntimeError`（fail-fast） |
| `channels.json` 読み込み失敗（欠落/破損） | 例外を上位に伝播 → 500レスポンス |
| `channels.json` の `channels` が空配列 | `[]` 返却 → 400レスポンス |
| チャンネルIDが無効 | `ValueError` → チャンネルをスキップ、他チャンネルは継続 |
| 一部チャンネル失敗 | 取得できたチャンネルの CSV だけ保存済み、500レスポンス（スケジューラがリトライ） |
| 動画マスタのコピー失敗 | ERROR ログのみ。GCS 上の前回分は残り、関数のステータスには影響しない |

### 依存ライブラリ

```text
functions-framework==3.10.2
google-cloud-storage==3.10.1
google-api-python-client==2.196.0
protobuf>=6.33.5,<7.0
```
