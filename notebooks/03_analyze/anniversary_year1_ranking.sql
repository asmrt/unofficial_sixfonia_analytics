-- ============================================================
-- 4周年企画: 「今年よく見られた1年目の動画」ランキング（たたき台）
-- ============================================================
-- 対象  : 1年目 = 2022-08-12 〜 2023-08-11 に投稿された動画
-- 指標  : 2026年（4周年まで）にどれだけ再生数が伸びたか
-- 前提  : GCS(oshi-katsu/youtube_stat) を BigQuery に取り込んだ後に使う想定。
--         パイプライン稼働前は anniversary_year1.ipynb の pandas 版を使う。
--
-- テーブル名は環境に合わせて書き換えること:
--   `<project>.<dataset>.video_statistics`  日次統計（MASTER_COLUMNS + channel）
--   `<project>.<dataset>.video_master`      動画マスタ（video_master.ipynb の出力）
-- ============================================================

DECLARE year1_start  DATE DEFAULT DATE '2022-08-12';  -- チャンネル開設日
DECLARE year1_end    DATE DEFAULT DATE '2023-08-11';  -- 1周年前日
DECLARE window_start DATE DEFAULT DATE '2026-01-01';  -- 「今年」の開始
DECLARE window_end   DATE DEFAULT DATE '2026-08-11';  -- 4周年前日
DECLARE target_channel STRING DEFAULT 'sixfonia';

WITH stats AS (
  SELECT
    videoId AS video_id,
    SAFE_CAST(viewCount AS INT64) AS view_count,
    -- view_date は YYYYMMDD 文字列。DATE型で入っている場合も拾えるようにする
    COALESCE(
      SAFE.PARSE_DATE('%Y%m%d', CAST(view_date AS STRING)),
      SAFE_CAST(view_date AS DATE)
    ) AS view_date
  FROM `<project>.<dataset>.video_statistics`
  WHERE channel = target_channel
),

-- 「今年」の期間に入るスナップショットだけに絞る
window_stats AS (
  SELECT *
  FROM stats
  WHERE view_date BETWEEN window_start AND window_end
    AND view_count IS NOT NULL
),

-- 期間の両端スナップショットの差を取る。
-- 日次差分(Diff)の SUM ではなく端点差にするのは、収集が飛んだ日が
-- あっても期間全体の増加量を取りこぼさないため。
bounds AS (
  SELECT
    video_id,
    MIN(view_date) AS first_date,
    MAX(view_date) AS last_date,
    ARRAY_AGG(view_count ORDER BY view_date ASC  LIMIT 1)[OFFSET(0)] AS views_at_start,
    ARRAY_AGG(view_count ORDER BY view_date DESC LIMIT 1)[OFFSET(0)] AS views_at_end
  FROM window_stats
  GROUP BY video_id
),

-- 1年目に投稿された動画
year1_videos AS (
  SELECT
    video_id,
    title,
    DATE(published_at) AS published_date,
    duration_seconds,
    IF(duration_seconds <= 60, 'Shorts', 'Long-form') AS video_type
  FROM `<project>.<dataset>.video_master`
  WHERE DATE(published_at) BETWEEN year1_start AND year1_end
)

SELECT
  RANK() OVER (ORDER BY b.views_at_end - b.views_at_start DESC) AS rank_no,
  v.video_id,
  v.title,
  v.published_date,
  v.video_type,
  b.views_at_end - b.views_at_start           AS views_gained,   -- 今年の伸び（ランキング指標）
  b.views_at_end                              AS total_views,    -- 累計再生数（期間末時点）
  ROUND(SAFE_DIVIDE(b.views_at_end - b.views_at_start,
                    NULLIF(b.views_at_end, 0)) * 100, 1) AS gain_share_pct,  -- 累計に占める今年分の割合
  CONCAT('https://www.youtube.com/watch?v=', v.video_id) AS video_url,
  CONCAT('https://img.youtube.com/vi/', v.video_id, '/hqdefault.jpg') AS thumbnail_url,
  b.first_date,                               -- 実際に集計できた期間（欠測確認用）
  b.last_date,
  DATE_DIFF(b.last_date, b.first_date, DAY) AS days_covered
FROM year1_videos v
JOIN bounds b USING (video_id)
ORDER BY views_gained DESC
LIMIT 50;


-- ============================================================
-- おまけ1: ジャンル別集計
--   再生リスト所属テーブル（anniversary_year1.ipynb が出力する
--   <ch>_playlist_map.csv を BQ に載せたもの）と突き合わせる。
-- ============================================================
-- WITH ranked AS ( ...上のクエリ本体... ),
-- genre AS (
--   SELECT video_id, ANY_VALUE(genre) AS genre
--   FROM `<project>.<dataset>.playlist_map`
--   GROUP BY video_id
-- )
-- SELECT
--   COALESCE(g.genre, '未分類') AS genre,
--   COUNT(*)                    AS video_count,
--   SUM(r.views_gained)         AS views_gained_total,
--   ROUND(AVG(r.views_gained))  AS views_gained_avg,
--   ARRAY_AGG(r.title ORDER BY r.views_gained DESC LIMIT 3) AS top3_titles
-- FROM ranked r
-- LEFT JOIN genre g USING (video_id)
-- GROUP BY genre
-- ORDER BY views_gained_total DESC;


-- ============================================================
-- おまけ2: 「再発見された」動画の抽出
--   1年目の動画のうち、今年の伸びが累計に占める割合が高いもの。
--   = 当時より今のほうが見られている＝振り返り記事のネタになる。
-- ============================================================
-- 上の本体クエリの ORDER BY を gain_share_pct DESC に変え、
-- WHERE b.views_at_end >= 10000 などで母数の小さい動画を除くと安定する。
