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
               ├──→ GCS {channel}/*.csv      統計データ（日次・生データの保管）
               ├──→ BigQuery video_statistics 同じデータを読み込み（その日・そのチャンネル分を置き換え）
               └──→ GCS master/videos.json   動画マスタのコピー（毎日上書き）
                        ↑
               公開URL（SNAPSHOT_URL）の videos.json を取得（YouTube API は使わない）
                        ↑
               動画マスタの生成（別システム）

BigQuery
  ├─ video_statistics             日次スナップショット（view_date で日付パーティション）
  └─ video_statistics_with_diff   view_diff（直前の計測日との差）をその場で計算するビュー
```

取得から BigQuery への読み込みまでを 1 つの関数で行う。スケジュールクエリや外部テーブルは使わない
（別時刻に動く処理を持たないので、関数のリトライが何時に終わっても取りこぼさない）。

> Colab ノートブックで手動で行っている日次収集を自動化するためのもの。当面は既存の Colab 運用と並行して動かす。
> 既存の Colab 運用とは出力先も CSV の列も異なる（既存: 別バケット・6列 / 本パイプライン: `youtube-metrics-bucket`・`thumbnail` を加えた7列）。
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
│   ├── 01_create_table.sql              日次スナップショットのテーブル作成 DDL
│   └── 02_create_view.sql               前日比（view_diff）のビュー作成 DDL
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
| `youtube-data-fetch` | Cloud Functions (gen2) | 動画統計情報（再生数等）の日次取得・GCS保存・BigQuery 読み込み、動画マスタのコピー（01:00 JST） |
| 動画マスタ（`videos.json`） | 別システム | 毎日生成され、公開URLで配信される。本パイプラインはコピーするだけ |
| GCS バケット | Cloud Storage | CSV/JSON の保管（生データ。BigQuery を作り直すときの元にもなる） |
| `video_statistics` | BigQuery テーブル | 日次スナップショットの蓄積（`sql/01`） |
| `video_statistics_with_diff` | BigQuery ビュー | `view_diff`・`diff_days` を足したビュー。分析はこちらを読む（`sql/02`） |
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

チャンネルごとの全動画の再生数・いいね数・コメント数を毎日取得し、GCS に CSV として保存したうえで、BigQuery に読み込む。

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
  ├─ load_to_bigquery()            取得できたチャンネル分を BigQuery に読み込み（1件も取れなければ呼ばない）
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
| `fetch_channel_data(channel_id, channel_name, view_date)` | — | `list[dict] \| None` | チャンネル1件分の取得と CSV 保存。取得した行を返す（失敗時は None） |
| `to_bq_rows(channel_name, video_stats)` | — | `list[dict]` | CSV 用の行を BigQuery 用に変換（channel を付け、数値・日付を型付け） |
| `load_to_bigquery(rows_by_channel, view_date)` | `dict[str, list]`, str | int | その日・そのチャンネルの行を消してから追加。戻り値は読み込んだ行数 |
| `copy_snapshot_to_gcs()` | — | str | 動画マスタを公開URLから取得して GCS に上書き保存。戻り値は `updated_at` |
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

### BigQuery への読み込み

GCS に書いたのと同じ行に `channel` を付け、`viewCount` などを整数、`view_date` を DATE にして読み込む。

```text
取得できたチャンネルの行（メモリ上）
  ├─ DELETE  video_statistics WHERE view_date = 前日 AND channel IN (取得できたチャンネル)
  └─ 読み込みジョブ（追記）
```

- リトライしても重複しない（同じ日・同じチャンネルの行は先に消える）
- 取得に失敗したチャンネルの行は触らない。前の試行で入った分は残り、次のリトライで置き換わる
- 読み込みに失敗したら ERROR ログを出して 500 を返す（Scheduler がリトライする）
- DELETE と読み込みは別ジョブなので、その間に失敗すると一時的にそのチャンネルの行が消えた状態になる。500 を返すのでリトライで戻る

`view_diff`（前日比）は保存せず、ビュー `video_statistics_with_diff` が `LAG` で直前の計測日との差を計算する。
欠けた日があっても次の日の値が出る（`diff_days` が 2 以上になる）。

---

## 4. 動画マスタ（videos.json）のコピー

### 方針

動画のタイトル・投稿日・長さ・Shorts 判定・タグ・公開状態は、別システムが毎日生成する `videos.json` を唯一の生成元とする。
ほかの用途でも同じ `videos.json` が使われているため、生成元を GCP 側に増やすと内容が食い違う。

本パイプラインは、`youtube-data-fetch` の実行時に公開URL（環境変数 `SNAPSHOT_URL`）の `videos.json` を取得し、GCS にそのままコピーする。
BigQuery でランキングにタイトルを付けたい場合は、このコピーを読む（IMPROVEMENTS.md の #5）。

### 保存先

```text
gs://{GCS_BUCKET}/master/videos.json    （毎日上書き。中身は公開URLの videos.json と同一）
```

中身は `{"updated_at": ..., "channels": [...], "videos": [...]}` 形式の1つの JSON オブジェクト。

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
| `BQ_TABLE` | **必須** | — | 読み込み先テーブル（`project.dataset.video_statistics`）。プロジェクトIDをリポジトリに書かないため、デプロイ時に渡す。未設定時は起動時に `RuntimeError` |
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
| `YOUTUBE_API_KEY` / `BQ_TABLE` / `SNAPSHOT_URL` 未設定 | 起動時に `RuntimeError`（fail-fast） |
| `channels.json` 読み込み失敗（欠落/破損） | 例外を上位に伝播 → 500レスポンス |
| `channels.json` の `channels` が空配列 | `[]` 返却 → 400レスポンス |
| チャンネルIDが無効 | `ValueError` → チャンネルをスキップ、他チャンネルは継続 |
| 一部チャンネル失敗 | 取得できた分は BigQuery に読み込み、500レスポンス（スケジューラがリトライ） |
| 全チャンネル失敗 | BigQuery には触らず、500レスポンス |
| BigQuery への読み込み失敗 | ERROR ログ、500レスポンス（スケジューラがリトライ） |
| 動画マスタのコピー失敗 | ERROR ログのみ。GCS 上の前回分は残り、関数のステータスには影響しない |

### 依存ライブラリ

```text
functions-framework==3.10.2
google-cloud-bigquery==3.45.2
google-cloud-storage==3.10.1
google-api-python-client==2.196.0
protobuf>=6.33.5,<7.0
```
