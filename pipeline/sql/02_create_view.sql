-- =============================================================
-- 02: 前日比のビュー作成
-- video_statistics の全列に、直前の計測日との差を足す。
--   - view_diff : 直前の計測日からの再生数の増分（その動画の最初の日は NULL）
--   - diff_days : 何日前との差か（通常は 1。欠けた日があると 2 以上）
-- 前日比を保存しないので、欠けた日があっても次の日の値が出る。計算方法を変えても過去分の作り直しは不要。
-- YOUR_PROJECT_ID / YOUR_DATASET は実行時に置換する（SETUP.md 手順4 の run_sql を参照）。
-- =============================================================

CREATE OR REPLACE VIEW `YOUR_PROJECT_ID.YOUR_DATASET.video_statistics_with_diff`
OPTIONS (
  description = 'video_statistics に前日比（view_diff）と比較対象までの日数（diff_days）を足したビュー'
)
AS
SELECT
  s.*,
  s.viewCount - LAG(s.viewCount) OVER w AS view_diff,
  DATE_DIFF(s.view_date, LAG(s.view_date) OVER w, DAY) AS diff_days
FROM `YOUR_PROJECT_ID.YOUR_DATASET.video_statistics` AS s
WINDOW w AS (PARTITION BY s.channel, s.videoId ORDER BY s.view_date);
