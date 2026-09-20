# 今後の改善点

現状は「最低限動く構成」です。運用しながら必要になった順に着手することを推奨します。
優先度は運用リスクの高い順に付けています。

## 構築前に確認・判断が必要なこと

### A. YouTube API 規約（データの保存期間）— 確認中

このパイプラインは、API で取得した再生数を BigQuery に期限なしで蓄積する設計になっている。
YouTube API の規約（API Services Terms / Developer Policies）では、API で取得したデータの保存期間に
制限がある。規約が更新されたため、現行の条文でこの設計が認められるかを確認中。
結果によっては、保存期間の制限や集計値だけを残す形などへの設計変更が必要になる。
構築から30日の猶予がある認識のため、先に構築する（2026-09-19）。
保存期間を区切ることになった場合は、GCS のライフサイクルルールで古い CSV を消すだけでよい（BigQuery は外部テーブルで GCS を直接読むので、自動でそろう）。

### B. 既存の Colab 運用からの切り替え方法 — 決定（2026-09-20）

既存の Colab 運用とは、出力先のバケットも CSV の列も異なる
（既存: 別バケット・6列 `videoId, viewCount, likeCount, commentCount, videoURL, view_date` /
本パイプライン: `youtube-metrics-bucket`・`channel` を加えた7列。列名は既存と同じ camelCase）。
移行は `channel` 列を足すだけでよい。手順・ツールの詳細は [docs/MIGRATION.md](MIGRATION.md) を参照。

サムネイルは動画マスタ側（ビュー `videos` の `thumbnail_url`）で持つので、統計 CSV では持たない
（動画IDからサムネイルURLを組み立てることもできるため、統計側で二重に持つ必要が無い）。

### C. 動画マスタの生成元 — 決定（2026-09-19）: 外部で生成される `videos.json` に一本化

当初あった `youtube-video-metadata-fetch`（GCP で全動画のタイトル・投稿日・長さ・Shorts 判定を取得する関数）は、
別システムが毎日生成している `videos.json` と取得内容が重複していたため削除した。

- 生成元は1か所だけ。GCP 側は `youtube-data-fetch` が公開URL（`SNAPSHOT_URL`）の `videos.json` を取得し、`master/videos.json` にコピーするだけ

残るリスク:

- 生成側の更新が遅れたり止まったりすると、コピーも古いままになる（コピー自体は成功するので、エラー通知は来ない）
- YouTube API キーが生成側と GCP Secret Manager の2か所に分かれる

### D. チャンネル定義が2か所にある

`sixfonia_analytics/config.py` と `youtube-data-fetch/channels.json` に同じ7チャンネルが定義されている。
中身が一致していることは `tests/test_pipeline.py` で検査している（チャンネル追加時に直し忘れるとテストが落ちる）。

## 優先度: 高

### 1. YouTube API キーの Secret Manager 管理 ✅ 対応済み（2026-07-07）

SETUP.md に取り込み済み（手順2でシークレット作成、手順5-1で権限付与、手順6のデプロイは `--set-secrets` 方式）。
コード変更は不要（環境変数名 `YOUTUBE_API_KEY` のまま Secret Manager からマウントされる）。

### 2. 失敗時の通知 ✅ 対応済み（2026-07-07）

SETUP.md 手順9に取り込み済み（ログベースアラート → メール通知）。BigQuery への読み込みも関数内で行うため、通知はこの1系統で足りる。
想定外の課金に備えた予算アラートも手順10に追加した（2026-09-19）。
両関数のログを severity 付き構造化JSON（`CloudLoggingFormatter`）に変更し、
Cloud Logging で `severity>=ERROR` のフィルタが確実に効くようにした。
Slack 通知への拡張は #6 を参照。

## 優先度: 中

### 3. view_diff の欠測日対応 ✅ 対応済み（2026-09-19）

view_diff をテーブルに保存するのをやめ、ビュー `video_statistics_with_diff` で「直前の計測日との差」を計算する形にした。
欠けた日があっても次の日の値が出る。何日分の差かは `diff_days` で分かる（1日あたりにしたい場合は `view_diff / diff_days`）。

### 4. 外部テーブルのスキャン量

BigQuery は GCS の CSV を外部テーブルとして直接読むため、クエリのたびに全 CSV を読む
（前日比のビューも、動画ごとに日付順で並べるので全期間を読む）。
1年で約2,500ファイル・約120万行（数百MB）の見込みで、無料枠（1TB/月）に対しては十分小さい。
数年分たまって重くなったら、次のどちらかを検討する:

- 外部テーブルの中身を日次でネイティブテーブルに書き出し、分析はそちらを読む
- CSV の保存パスを `stats/dt=YYYYMMDD/{channel}.csv` 形式にして Hive パーティション化（読む日付を絞れる）

### 4-2. BigQuery 側の構成の経緯（2026-09-19 決定）

1. 最初の設計: 関数が GCS に CSV を書く → 02:00 のスケジュールクエリが外部テーブル経由でネイティブテーブルに MERGE。
   関数のリトライが 02:00 を過ぎるとその日の分が入らない、失敗通知が2系統に分かれる、という問題があった
2. 次の案: 関数が BigQuery のネイティブテーブルに直接読み込む。時刻の問題は消えたが、CSV を差し替えても BigQuery に反映されない
3. 決定: **外部テーブル＋ビューのみ**。BigQuery はデータを持たず GCS を直接読むので、CSV の差し替えが次のクエリから反映され、
   移し替えの処理が無いので時刻の問題も起きない。問題の原因は外部テーブルではなく、時刻で動く移し替え（スケジュールクエリ）だった

### 5. 動画マスタの BigQuery 連携 ✅ 対応済み（2026-09-19）

関数が `master/videos.json` に加えて、1行1動画の `master/videos.ndjson` を書く。
BigQuery は外部テーブル `ext_videos` でこれを読み、ビュー `videos`（`video_id`, `title`, `is_short` など）として使える。
ランキングにタイトルを付けたり、Shorts を除いて集計したりが BigQuery・Looker Studio だけでできる。

### 6. 可視化・通知（旧ガイドのスコープ）

[旧ガイド](legacy_implementation_guide.md)にあった以下は今回未実装です:

- **Looker Studio**: ビュー `video_statistics_with_diff` を直接データソースにすれば追加開発なしで作成可能。
  メール配信スケジュール機能で毎朝の定期配信も可能
- **Slack 通知**: Cloud Functions をもう1本（ランキングクエリ → Slack Webhook POST）追加するのが最小
- **Streamlit ダッシュボード（Cloud Run）**: [旧ガイド](legacy_implementation_guide.md)に全コードあり。
  ただし旧ガイドのコードは view_date が STRING 前提のため、DATE 型に合わせた修正が必要

## 優先度: 低

### 7. 新着・削除動画の差分記録

新着・削除の差分は日次では記録していません（`master/videos.json` は毎日上書き）。
動画マスタには公開状態（`available`）が入っているので、コピーを日付付きでも保存すれば
（例: `master/history/videos_{YYYYMMDD}.json`）、差分は後から計算できます。
[旧ガイド](legacy_implementation_guide.md)の `save_diff_to_gcs()` も参考になります。

### 8. チャンネル処理の並列化・リトライ強化

現状は7チャンネル直列処理（約数分）で実用上問題ありませんが、チャンネル数が
増えたら `concurrent.futures` での並列化や、YouTube API 呼び出しへの
指数バックオフリトライ（`googleapiclient` の `num_retries` 引数など)を検討。

### 9. Infrastructure as Code 化

セットアップ手順（バケット・SA・関数・スケジューラ・BQ）を Terraform 化すると、
プロジェクト再構築や構成変更の履歴管理ができます。個人運用のうちは手順書で十分です。

### 10. テストコード ✅ 対応済み（2026-09-19）

`tests/test_pipeline.py` に追加（`pip install -e ".[dev]" && pytest tests -q`）。YouTube API・GCS・公開URLは偽物に差し替えて、
50件ずつのバッチ処理、CSV の列順（7列・最後が channel）と外部テーブル `ext_video_statistics` の列定義の一致、
各行に channel と view_date が入ること、view_date が JST の前日になること、一部チャンネル失敗時に 500 を返すこと、
動画マスタのコピー（videos.json をそのまま、videos.ndjson を1行1動画で書くこと・不正な内容では何も書かないこと・失敗しても統計に影響しないこと）、
外部テーブル `ext_videos` の列が動画マスタのキーにあること、チャンネル定義の一致を検査している。
