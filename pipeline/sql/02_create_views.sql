-- =============================================================
-- 02: ビューの作成
-- 外部テーブル（sql/01）の列名（camelCase）を、BigQuery 側の呼び方であるスネークケースに変換する。
-- YOUR_PROJECT_ID / YOUR_DATASET は実行時に置換する（SETUP.md 手順4 の run_sql を参照）。
--
-- video_statistics: ext_video_statistics をスネークケースの列名にしたもの。
--   channel は CSV の列を優先し、無ければファイルパス（{channel}/...）から補う。
-- video_statistics_with_diff: video_statistics の全列に、直前の計測日との差を足す。
--   - view_diff : 直前の計測日からの再生数の増分（その動画の最初の日は NULL）
--   - diff_days : 何日前との差か（通常は 1。欠けた日があると 2 以上）
--   前日比を保存しないので、欠けた日があっても次の日の値が出る。計算方法を変えても過去分の作り直しは不要。
-- videos: ext_videos をスネークケースの列名にしたもの。
-- =============================================================

CREATE OR REPLACE VIEW `YOUR_PROJECT_ID.YOUR_DATASET.video_statistics` AS
SELECT
  COALESCE(channel, REGEXP_EXTRACT(_FILE_NAME, r'^gs://[^/]+/([^/]+)/')) AS channel,
  videoId      AS video_id,
  viewCount    AS view_count,
  likeCount    AS like_count,
  commentCount AS comment_count,
  videoURL     AS video_url,
  thumbnail    AS thumbnail_url,
  PARSE_DATE('%Y%m%d', view_date) AS view_date
FROM `YOUR_PROJECT_ID.YOUR_DATASET.ext_video_statistics`
-- 同じ日・同じ動画の行が複数ファイルにあった場合の防御（ファイル名が後のものを採用）
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY COALESCE(channel, REGEXP_EXTRACT(_FILE_NAME, r'^gs://[^/]+/([^/]+)/')), videoId, view_date
  ORDER BY _FILE_NAME DESC
) = 1;

CREATE OR REPLACE VIEW `YOUR_PROJECT_ID.YOUR_DATASET.video_statistics_with_diff` AS
SELECT
  s.*,
  s.view_count - LAG(s.view_count) OVER w AS view_diff,
  DATE_DIFF(s.view_date, LAG(s.view_date) OVER w, DAY) AS diff_days
FROM `YOUR_PROJECT_ID.YOUR_DATASET.video_statistics` AS s
WINDOW w AS (PARTITION BY s.channel, s.video_id ORDER BY s.view_date);

CREATE OR REPLACE VIEW `YOUR_PROJECT_ID.YOUR_DATASET.videos` AS
SELECT
  videoId     AS video_id,
  channel,
  title,
  publishedAt AS published_at,
  durationSec AS duration_sec,
  isShort     AS is_short,
  thumbnail   AS thumbnail_url,
  tags,
  available
FROM `YOUR_PROJECT_ID.YOUR_DATASET.ext_videos`;
