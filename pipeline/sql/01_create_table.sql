-- =============================================================
-- 01: 日次スナップショットのテーブル作成
-- youtube-data-fetch が毎日、前日分（view_date）を読み込む。
--   - 読み込みは「その日・そのチャンネルの行を消してから追加」なので、リトライしても重複しない
--   - view_date で日付パーティション。保存期間を区切る場合はパーティションの有効期限を設定する（SETUP.md 手順4）
-- YOUR_PROJECT_ID / YOUR_DATASET は実行時に置換する（SETUP.md 手順4 の run_sql を参照）。
-- 列名は main.py の to_bq_rows() と一致させる（tests/test_pipeline.py で検査）。
-- =============================================================

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT_ID.YOUR_DATASET.video_statistics` (
  channel      STRING NOT NULL OPTIONS (description = 'チャンネル名（channels.json の name）'),
  videoId      STRING NOT NULL OPTIONS (description = '動画ID'),
  viewCount    INT64           OPTIONS (description = '再生数'),
  likeCount    INT64           OPTIONS (description = '高評価数'),
  commentCount INT64           OPTIONS (description = 'コメント数'),
  videoURL     STRING          OPTIONS (description = '動画URL'),
  thumbnail    STRING          OPTIONS (description = 'サムネイルURL'),
  view_date    DATE   NOT NULL OPTIONS (description = '計測日（JST・関数実行日の前日）')
)
PARTITION BY view_date
CLUSTER BY channel, videoId
OPTIONS (
  description = 'YouTube 動画統計の日次スナップショット（youtube-data-fetch が読み込む）'
);
