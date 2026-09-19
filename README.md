# unofficial_sixfonia_analytics

シクフォニ（非公式）YouTube統計の収集・分析・X投稿用画像生成ツール。
Google Colab 上に散在していたノートブック14本を、共通パッケージ + 目的別ノートブック9本に整理したもの。

設計の経緯・旧ノートブックとの対応は [docs/DESIGN.md](docs/DESIGN.md) を参照。

## セットアップ（Colab）

1. Colab の Secrets（🔑）に以下を登録
   - `YOUTUBE_API_KEY` — YouTube Data API v3 キー
   - `GITHUB_TOKEN` — 本リポジトリ（非公開）を pip install するための Personal Access Token（repo read権限）
2. 各ノートブックの先頭セルを実行（パッケージのinstallとDriveマウント）

```python
from google.colab import userdata
token = userdata.get("GITHUB_TOKEN")
%pip install -q "git+https://{token}@github.com/<OWNER>/unofficial_sixfonia_analytics.git"
```

## ノートブック一覧

| ノートブック | 用途 | 実行頻度 |
|---|---|---|
| `01_collect/daily_video_stats` | 7チャンネルの動画統計を日次収集しCSV保存 | 毎日 |
| `01_collect/video_master` | 動画一覧・投稿日時・動画長・Shorts判定の取得 | 随時 |
| `02_maintain/csv_cleaning_and_upload` | 過去CSVの棚卸し・スキーマ統一・GCSアップロード | 随時 |
| `03_analyze/view_trend` | 特定動画の再生数推移・月次ランキング・直近3日上昇率 | 随時 |
| `03_analyze/daily_ranking` | 日次差分ランキング（4指標、Shorts/長尺別） | 随時 |
| `03_analyze/comment_analysis` | コメント取得・単語頻度・ワードクラウド | 随時 |
| `03_analyze/upload_count_analysis` | 投稿本数・ショート比率・投稿カレンダー | 随時 |
| `04_x_content/quiz_graph` | Xクイズ出題/回答用・ブログ用グラフ画像 | 随時 |
| `04_x_content/quiz_answer_card` | サムネ付き回答発表/ピックアップカード画像 | 随時 |

### 企画・単発の分析

| ノートブック | 用途 |
|---|---|
| `03_analyze/anniversary_year1` | 周年企画: 1年目に投稿された動画の、指定年の再生数の伸びランキングと再生リスト別のジャンル集計（CSV出力）。BigQuery 版のたたき台は `anniversary_year1_ranking.sql` |
| `03_analyze/video_playlist_map` | 1チャンネル分の「動画 × 再生リスト」対応表（TSV出力）。パッケージに依存しない単体ノートブック |
| `04_x_content/ranking_top10_image` | 再生リストごとの再生数の伸び TOP10 を X 投稿用画像にして ZIP でまとめて出力。仕様は `ranking_top10_spec.md` |

## パッケージ構成（`sixfonia_analytics/`）

| モジュール | 役割 |
|---|---|
| `config` | チャンネル定義・パス・ファイル名規則 |
| `auth` | API キーの読み込み（Colab Secrets / 環境変数） |
| `collect` / `maintain` | 日次統計の収集、過去 CSV の整備 |
| `load` / `enrich` | CSV の読み込み、動画タイトルなどの付与 |
| `metrics` | 差分・月次増加量・期間の増加量（`period_gains`）・上昇率 |
| `playlists` | 公開再生リストの取得と、再生リスト名によるジャンル分類 |
| `display` / `plots` / `cards` | ランキング表示、グラフ、カード画像 |
| `comment` | コメント取得・単語頻度 |

## データ配置（Google Drive）

- 正: `MyDrive/sixfonia_yt_analytics/<channel>/<channel>_video_statistics_YYYYMMDD.csv`
- 移行期のみ: `MyDrive/YouTube_Data/`（旧フラット構成）にもデュアルライト
- スキーマ: `videoId, viewCount, likeCount, commentCount, videoURL, view_date`

## 日次収集の自動化（GCP）

Colab で手動実行している日次収集を GCP で自動化するパイプラインを [`pipeline/`](pipeline/README.md) に置いている
（Cloud Scheduler → Cloud Function → GCS・BigQuery）。構築手順は [pipeline/docs/SETUP.md](pipeline/docs/SETUP.md)。
当面は Colab 運用と並行する。未決事項は [pipeline/docs/IMPROVEMENTS.md](pipeline/docs/IMPROVEMENTS.md) を参照。

## テスト

```bash
pip install -e ".[dev]" && pytest tests -q
```

## 関連

推し活ツール（新着一覧・未視聴チェック・画像生成）は別リポジトリに移行済み。
全体の概要は [docs/oshikatsu_apps_overview.md](docs/oshikatsu_apps_overview.md) を参照。

## 開発メモ

- ノートブックはロジックを持たず、`sixfonia_analytics` パッケージの関数を呼ぶだけにする
- チャンネル追加・パス変更は `sixfonia_analytics/config.py` のみ編集
- Colab で編集したノートブックは「ファイル → GitHub にコピーを保存」で本リポジトリへ同期
- APIキーはコードに書かない（`auth.get_api_key` が Secrets / 環境変数から読む）
