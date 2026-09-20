# 既存データの移行手順

既存の Colab 運用が旧バケットに書いていた統計 CSV（6列）を、本パイプラインの新バケットへ移行する手順。
移行ツールは `pipeline/tools/migrate_legacy_csv.py`。

## 前提

- 移行元（旧バケットの CSV）が正。移行では書き換えない
- 移行先バケットは [SETUP.md](SETUP.md) 手順3で作成済みのもの（`$BUCKET`）を使う
- 実行するアカウントが、移行元・移行先の両方の GCS バケットに読み書きできること
  （Cloud Shell で `gcloud auth list` して権限を確認する）

変換内容は「`channel` 列を1つ足すだけ」。サムネイル列は元から無いので変換不要
（旧: `videoId, viewCount, likeCount, commentCount, videoURL, view_date` →
新: `videoId, viewCount, likeCount, commentCount, videoURL, view_date, channel`）。

## 手順

### 1. 確認だけ実行（dry run）

`--apply` を付けなければ何も書き込まず、実施予定の内容とファイルごとの問題点だけを表示する。

```bash
pip install google-cloud-storage   # Cloud Shell に無ければ

python pipeline/tools/migrate_legacy_csv.py \
  --source-bucket <移行元バケット> \
  --dest-bucket $BUCKET
```

例（移行元バケット名は一例）: `--source-bucket oshi-katsu`

出力の末尾に「対象ファイル数」「行数」「問題のあったファイル一覧」が表示される。

### 2. 問題のあったファイルを直すか除く

dry run の出力に問題（列が想定と違う、`view_date` がファイル名の日付と違う、カウントが整数の形式でない、など）
が出たファイルは、移行元のファイルを直すか、`--channel` で対象チャンネルを絞って一旦除外する。

```bash
# 特定チャンネルだけ移行する場合
python pipeline/tools/migrate_legacy_csv.py \
  --source-bucket <移行元バケット> \
  --dest-bucket $BUCKET \
  --channel lan --channel hima72
```

### 3. `--apply` で実行

問題が無い（または許容できる）ことを確認したら、実際に書き込む。

```bash
python pipeline/tools/migrate_legacy_csv.py \
  --source-bucket <移行元バケット> \
  --dest-bucket $BUCKET \
  --apply
```

新形式（7列）にできなかったファイル（列が想定と違う・空）は、`--apply` でも書き込まずにスキップする。
並びの違う CSV が移行先に混じると、外部テーブル経由のクエリ全体が失敗するため。

移行先に同名のファイルが既にある場合も自動的にスキップされる（後述の注意を参照）。
何らかの事情で明示的に上書きしたい場合だけ `--overwrite` を付ける。

### 4. BigQuery で検証

[SETUP.md](SETUP.md) 手順4で作成済みのビューにクエリを投げ、移行元の件数と突き合わせる。

## 検証クエリ

チャンネル別・月別の件数を、移行元のファイル数・行数と突き合わせる。

```bash
bq query --use_legacy_sql=false "
SELECT channel, FORMAT_DATE('%Y-%m', view_date) AS ym, COUNT(*) AS row_count
FROM \`$PROJECT_ID.$DATASET.video_statistics\`
GROUP BY channel, ym ORDER BY channel, ym"
```

`video_statistics_with_diff` の `view_diff` が、移行元と新パイプラインの継ぎ目で不自然な値
（極端に大きい・マイナスなど）になっていないかを確認する。

```bash
bq query --use_legacy_sql=false "
SELECT channel, video_id, view_date, view_count, view_diff, diff_days
FROM \`$PROJECT_ID.$DATASET.video_statistics_with_diff\`
WHERE view_diff < 0 OR diff_days > 2
ORDER BY channel, video_id, view_date"
```

## 注意

- パイプライン稼働後の日付を上書きしないよう、移行先に既に存在するファイルは常にスキップする
  （`--overwrite` を付けない限り）。移行の実行タイミングは気にしなくてよい
- 移行元のファイルは書き換えない。移行はコピー（変換つき）のみ
- Colab 運用を止めるのは、上記の検証クエリで移行結果に問題が無いことを確認したあとにする
