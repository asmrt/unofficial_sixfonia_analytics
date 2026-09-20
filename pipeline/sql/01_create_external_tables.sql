-- =============================================================
-- 01: 外部テーブルの作成
-- BigQuery はデータを持たず、GCS 上のファイルを直接読む。CSV・NDJSON を
-- 差し替えれば、次のクエリから内容が反映される（読み込み・スケジュールクエリは無い）。
-- YOUR_PROJECT_ID / YOUR_DATASET / YOUR_BUCKET は実行時に置換する（SETUP.md 手順4 の run_sql を参照）。
--
-- ext_video_statistics: CSV は列名の行を持つが、外部テーブルは位置で列を読む。
--   main.py の save_to_gcs() が書く列の並び（7列）と、この定義の並びを必ず一致させること。
--   サムネイルは持たない（動画マスタ側、ビュー videos の thumbnail_url を使う）。
-- ext_videos: 動画マスタ（videos.ndjson。main.py の copy_snapshot_to_gcs() が書く）を読む。
-- =============================================================

CREATE OR REPLACE EXTERNAL TABLE `YOUR_PROJECT_ID.YOUR_DATASET.ext_video_statistics` (
  videoId      STRING,
  viewCount    INT64,
  likeCount    INT64,
  commentCount INT64,
  videoURL     STRING,
  view_date    STRING,  -- YYYYMMDD
  channel      STRING
)
OPTIONS (
  format = 'CSV',
  uris = ['gs://YOUR_BUCKET/*.csv'],   -- master/ の JSON・NDJSON はマッチしない
  skip_leading_rows = 1
);

CREATE OR REPLACE EXTERNAL TABLE `YOUR_PROJECT_ID.YOUR_DATASET.ext_videos` (
  videoId     STRING,
  channel     STRING,
  title       STRING,
  publishedAt TIMESTAMP,
  durationSec INT64,
  isShort     BOOL,
  thumbnail   STRING,
  tags        ARRAY<STRING>,
  available   BOOL
)
OPTIONS (
  format = 'NEWLINE_DELIMITED_JSON',
  uris = ['gs://YOUR_BUCKET/master/videos.ndjson'],
  ignore_unknown_values = true
);
