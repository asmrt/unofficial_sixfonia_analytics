# unofficial_sixfonia_analytics 設計書

作成日: 2026-07-09
対象: Google Colab上に散在していたシクフォニ分析ノートブック14本の整理・統合

## 1. 背景と目的

Colab Notebooks フォルダに YouTube 統計の収集・分析・X投稿用画像生成のノートブックが
14本散在し、以下の問題があった。

- 同一機能の重複実装（combined_df構築・タイトル取得・サムネURL生成・日次差分計算が5〜6箇所）
- プロトタイプと完成版の並存（03/04 → 06、08 → 09、06 ≒ 10）
- Colab AI生成の試行錯誤セルの堆積（特に14は170セル超）
- APIキーのコード直書き（2種類の AIzaSy... が 04/06/10/14 に露出）
- データフォルダの二重管理（YouTube_Data フラット vs sixfonia_yt_analytics/チャンネル別）

本リポジトリは、共通処理を Python パッケージ `sixfonia_analytics` に集約し、
ノートブックを「パラメータ設定と実行・可視化」の薄いレイヤーとして再構成する。

## 2. 決定事項（2026-07-09 あさみさん確認済み）

| 論点 | 決定 |
|---|---|
| リポジトリ | `unofficial_sixfonia_analytics`（GitHub・非公開） |
| データパス | 当面は両方維持。最終的にはクリーニング済みデータが正となり、移行完了後に `YouTube_Data/` を廃止 |
| 共通化方式 | GitHub管理のパッケージを Colab から `pip install git+...`。ノートブックのインタラクティブ性は維持 |
| 日次ランキング指標 | 4指標すべて維持（単純差分／急増スコア平均ベース／急増スコア中央値ベース／3日差分変化） |
| 旧ノートブック | 全て退役可（アーカイブフォルダへ移動、削除しない） |

## 3. 旧ノートブック → 新構成の対応表

| 旧ノートブック | 処遇 | 移行先 |
|---|---|---|
| 202510_YouTube_Video_Stat (14) | 収集部のみ移植、分析セルは daily_ranking へ | `01_collect/daily_video_stats` |
| YouTube動画情報抽出 (02) | 統合 | `01_collect/video_master` |
| 投稿動画数の分析 (11) | 前半=収集/後半=分析に分割 | `01_collect/video_master` + `03_analyze/upload_count_analysis` |
| YouTube動画一覧取得_暇72 (01) | 退役（機能は video_master が上位互換） | - |
| 再生数CSVファイルクリーニング (13) | ほぼそのまま移植 | `02_maintain/csv_cleaning_and_upload` |
| 特定動画の再生数推移+月次ランキング_暇72 (06) | 統合のベース | `03_analyze/view_trend` |
| 同 のコピー (10) | ブログ用グラフ3種を plots.py へ移植し退役 | `04_x_content/quiz_graph` |
| 特定動画の再生数推移_暇72 (04) | 退役（06に完全包含） | - |
| 再生数推移_pretender (03) | 退役（初期プロトタイプ） | - |
| シクフォニ_再生数ランキング (07) | 14の分析群と統合 | `03_analyze/daily_ranking` |
| youtube_comment_analysis_colab (09) | 移植ベース | `03_analyze/comment_analysis` |
| おそ松さんコメント分析 (08) | 語彙リストをプリセット化して退役 | `config.VOCAB_PRESETS["osomatsu"]` |
| XXクイズ回答発表 (12) | 4バリアントを1実装+設定に統合 | `04_x_content/quiz_answer_card` |
| 再生数グラフ生成 (05) | 期間ズーム・補間ロジックを移植 | `04_x_content/quiz_graph` |

## 4. リポジトリ構成

```
unofficial_sixfonia_analytics/
├── pyproject.toml
├── README.md
├── docs/DESIGN.md                     ← 本書
├── sixfonia_analytics/                ← 共通パッケージ
│   ├── config.py     チャンネル定義(7ch)・MASTER_COLUMNS・パス・語彙プリセット
│   ├── auth.py       APIキー取得(Colab Secrets)・YouTubeクライアント構築
│   ├── collect.py    動画統計/動画マスタの取得とCSV保存（デュアルライト対応）
│   ├── load.py       日次CSV群のロード・combined_df構築（新旧スキーマ両対応）
│   ├── metrics.py    Diff列・月次集計・直近N日・急増スコア(平均/中央値)・3日差分変化
│   ├── enrich.py     タイトル/投稿日/動画長の取得・サムネURL・Shorts判定
│   ├── comment.py    コメント全件取得・nagisa単語抽出(SINGLE_WORDS/類義語)
│   ├── display.py    HTMLランキングカード/テーブル/サムネグリッド
│   ├── plots.py      日本語フォント・推移グラフ・テーマ(quiz/blog)・補間系列
│   ├── cards.py      X投稿用カード画像（回答発表/ピックアップ）
│   └── maintain.py   列棚卸し・スキーマ整形・整合性チェック・GCSアップロード
└── notebooks/
    ├── 01_collect/daily_video_stats.ipynb      毎日実行（唯一の定常実行notebook）
    ├── 01_collect/video_master.ipynb           動画一覧・メタデータ（随時）
    ├── 02_maintain/csv_cleaning_and_upload.ipynb 過去データ整備・GCS
    ├── 03_analyze/view_trend.ipynb             特定動画推移・月次ランキング
    ├── 03_analyze/daily_ranking.ipynb          日次ランキング4指標
    ├── 03_analyze/comment_analysis.ipynb       コメント分析（汎用）
    ├── 03_analyze/upload_count_analysis.ipynb  投稿本数分析
    ├── 04_x_content/quiz_graph.ipynb           クイズ/ブログ用グラフ
    └── 04_x_content/quiz_answer_card.ipynb     回答発表カード画像
```

## 5. データフローと移行ステップ

```
現状:   収集 → YouTube_Data(生データ)
        手動コピー → sixfonia_yt_analytics/<ch>/ → クリーニング → GCS(oshi-katsu)

移行期: daily_video_stats が最初から MASTER_COLUMNS スキーマで
        sixfonia_yt_analytics/<ch>/ と YouTube_Data/ の両方に保存（デュアルライト）
        分析ノートブックは順次 sixfonia_yt_analytics 参照へ切替

完了後: sixfonia_yt_analytics のみに保存。YouTube_Data は廃止
```

- MASTER_COLUMNS: `videoId, viewCount, likeCount, commentCount, videoURL, view_date`
- `view_date` は `YYYYMMDD` 文字列（クリーニング済みファイルと同一仕様）
- 旧ファイル（view_date列なし）は load.py がファイル名から自動補完して読む

移行手順:
1. Google Cloud Console で露出済みAPIキー2種を無効化 → 再発行
2. Colab Secrets に `YOUTUBE_API_KEY` と `GITHUB_TOKEN`（非公開リポジトリinstall用）を登録
3. 本リポジトリを GitHub に push、各ノートブックを Colab で動作確認（1本ずつ）
4. デュアルライト運用開始、分析ノートブックの参照切替
5. 旧14本を `Colab Notebooks/archive_20260709/` へ移動
6. 安定後、YouTube_Data への書き込みを停止（config の LEGACY_WRITE を False に）

## 6. セキュリティ / 既知の修正点

- APIキーは全ノートブックで Colab Secrets `YOUTUBE_API_KEY` に統一
  （旧08のタイポSecret `YOUTUBE_APY_KEY` は廃止。auth.py は互換のため両方読む）
- 旧08 の stop_core リストにカンマ抜けバグあり（`"ホント" "めっちゃ"` が
  `"ホントめっちゃ"` に連結されていた）→ プリセット化時に修正済み
- 旧07/14 の日付ハードコードを「フォルダ内の最新N日を自動検出」に変更
- GCSバケット: `oshi-katsu` / プレフィックス `youtube_stat`（既存踏襲）

## 7. 将来の接続先

`load_youtube_data`（YouTube統計パイプライン、GCP設定待ち）が稼働したら、
収集系ノートブック（01_collect）はパイプラインに置き換わり、
本リポジトリは分析・X投稿コンテンツ生成（03/04）に縮退する想定。
