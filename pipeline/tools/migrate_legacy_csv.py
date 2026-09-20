# -*- coding: utf-8 -*-
"""既存（Colab 運用）の統計 CSV を、本パイプラインの新バケットへ移行するツール。

移行元は旧バケットの `{prefix}/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv`
（6列: videoId, viewCount, likeCount, commentCount, videoURL, view_date）。
本パイプラインの CSV は `channel` 列を足した7列なので、変換は `channel` を足すだけでよい
（サムネイル列は元から無いので変換不要）。

デフォルトは dry run（何も書き込まない）。実行するには `--apply` を付ける。
移行先に同名のファイルが既にある場合は `--overwrite` を付けない限り上書きしない
（パイプライン稼働後の日付を移行で壊さないため）。

手順は docs/MIGRATION.md を参照。
"""

import argparse
import csv
import io
import re

from google.cloud import storage

LEGACY_COLUMNS = ["videoId", "viewCount", "likeCount", "commentCount", "videoURL", "view_date"]
NEW_COLUMNS = LEGACY_COLUMNS + ["channel"]

_INT_RE = re.compile(r"^\d+$")

# これらの問題が出たファイルは新形式にできていないので、移行先へは書き込まない
NOT_WRITABLE_PROBLEMS = ("列が想定と違う", "空のファイル")

# {prefix}/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv（prefix は無くてもよい）
_BLOB_PATH_RE = re.compile(r"^(?:(?P<prefix>.+)/)?(?P<folder>[^/]+)/(?P<filename>[^/]+)$")
_FILENAME_RE = re.compile(r"^(?P<channel>.+)_video_statistics_(?P<date>\d{8})\.csv$")


def convert_csv(text, channel, file_date):
    """レガシーCSV（テキスト）に channel 列を足した新形式に変換する。

    戻り値は (変換後のCSVテキスト, 問題メッセージのリスト)。
    行は削除しない（問題があっても報告するだけで変換は続ける）。
    すでに新形式（7列）のファイルは「変換済み」として、中身を変えずに返す。
    """
    problems = []
    rows = list(csv.reader(io.StringIO(text)))
    # 末尾の完全な空行は無視する
    while rows and rows[-1] == []:
        rows.pop()

    if not rows:
        return text, ["空のファイル"]

    header = rows[0]

    if header == NEW_COLUMNS:
        problems.append("変換済み")
        return text, problems

    if header != LEGACY_COLUMNS:
        problems.append(f"列が想定と違う: {header}")
        return text, problems

    # 行ごとの問題は、同じ内容を何千行分も並べないよう「件数と最初の例」にまとめる
    bad_dates = []
    bad_counts = {col: [] for col in ("viewCount", "likeCount", "commentCount")}

    out_rows = [NEW_COLUMNS]
    for row in rows[1:]:
        record = dict(zip(LEGACY_COLUMNS, row))

        view_date = record.get("view_date", "")
        if view_date != file_date:
            bad_dates.append(view_date)

        for col in ("viewCount", "likeCount", "commentCount"):
            value = record.get(col, "")
            if value != "" and not _INT_RE.match(value):
                bad_counts[col].append(value)

        out_rows.append(row + [channel])

    if bad_dates:
        problems.append(
            f"view_date がファイル名の日付（{file_date}）と違う行が {len(bad_dates)}行（例: {bad_dates[0]!r}）"
        )
    for col, values in bad_counts.items():
        if values:
            problems.append(f"{col} が整数の形式でない行が {len(values)}行（例: {values[0]!r}）")

    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(out_rows)
    return buf.getvalue(), problems


def parse_blob_name(name):
    """`{prefix}/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv` を (channel, YYYYMMDD) に分解する。

    形式に合わない場合、またはフォルダ名とファイル名の channel 部分が食い違う場合は None を返す。
    """
    path_match = _BLOB_PATH_RE.match(name)
    if not path_match:
        return None

    filename_match = _FILENAME_RE.match(path_match.group("filename"))
    if not filename_match:
        return None

    folder = path_match.group("folder")
    file_channel = filename_match.group("channel")
    if folder != file_channel:
        return None

    return folder, filename_match.group("date")


def migrate(source_bucket, source_prefix, dest_bucket, *, channels=None, apply=False, overwrite=False, client=None):
    """旧バケットの統計CSVを新バケットへ移行する。

    デフォルト（apply=False）は dry run で、何も書き込まずに実施予定の内容を表示するだけ。
    移行先に同名のオブジェクトが既にある場合は overwrite=True でない限りスキップする
    （パイプライン稼働後に書かれた日付を移行で上書きしないため）。

    戻り値: {"files": 対象ファイル数, "rows": 変換した行数, "written": 実際に書き込んだ数,
             "skipped_existing": 移行先に既にあってスキップした数,
             "skipped_broken": 新形式にできずスキップした数, "problems": 問題メッセージのリスト}
    """
    if client is None:
        client = storage.Client()

    src_bucket = client.bucket(source_bucket)
    dst_bucket = client.bucket(dest_bucket)

    summary = {"files": 0, "rows": 0, "written": 0, "skipped_existing": 0,
               "skipped_broken": 0, "problems": []}

    for blob in src_bucket.list_blobs(prefix=source_prefix):
        parsed = parse_blob_name(blob.name)
        if parsed is None:
            summary["problems"].append(f"{blob.name}: ファイル名の形式が想定と違う")
            continue

        channel, file_date = parsed
        if channels and channel not in channels:
            continue

        summary["files"] += 1
        text = blob.download_as_text()
        converted, problems = convert_csv(text, channel, file_date)
        for problem in problems:
            summary["problems"].append(f"{blob.name}: {problem}")

        # 新形式にできなかったファイルは書き込まない。
        # 列の並びが違う CSV が移行先に混じると、外部テーブル経由のクエリ全体が失敗するため
        if any(p.startswith(prefix) for p in problems for prefix in NOT_WRITABLE_PROBLEMS):
            summary["skipped_broken"] += 1
            print(f"[SKIP] {blob.name} は新形式に変換できないため書き込みません")
            continue

        row_count = max(len(converted.splitlines()) - 1, 0)
        summary["rows"] += row_count

        filename = blob.name.rsplit("/", 1)[-1]
        dest_path = f"{channel}/{filename}"
        dest_blob = dst_bucket.blob(dest_path)

        if dest_blob.exists() and not overwrite:
            summary["skipped_existing"] += 1
            print(f"[SKIP] gs://{dest_bucket}/{dest_path} は既に存在します（既存を保護のため上書きしません）")
            continue

        if apply:
            dest_blob.upload_from_string(converted, content_type="text/csv")
            summary["written"] += 1
            print(f"[WRITE] gs://{dest_bucket}/{dest_path}（{row_count}行）")
        else:
            print(f"[DRY RUN] gs://{dest_bucket}/{dest_path} を書き込み予定（{row_count}行）")

    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="旧GCSバケットの統計CSVを新バケットへ移行する（channel列を追加するだけ）。手順は docs/MIGRATION.md を参照。",
    )
    parser.add_argument("--source-bucket", required=True, help="移行元バケット名")
    parser.add_argument("--source-prefix", default="youtube_stat", help="移行元のプレフィックス（デフォルト: youtube_stat）")
    parser.add_argument("--dest-bucket", required=True, help="移行先バケット名")
    parser.add_argument("--channel", action="append", dest="channels",
                         help="対象チャンネルを絞り込む（複数指定可。省略時は全チャンネル）")
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（省略時は dry run のみ）")
    parser.add_argument("--overwrite", action="store_true", help="移行先に同名ファイルがあっても上書きする")
    args = parser.parse_args(argv)

    summary = migrate(
        args.source_bucket,
        args.source_prefix,
        args.dest_bucket,
        channels=args.channels,
        apply=args.apply,
        overwrite=args.overwrite,
    )

    mode = "本番実行" if args.apply else "dry run（実際に書き込むには --apply を付けて再実行）"
    print()
    print(f"=== 移行結果（{mode}） ===")
    print(f"対象ファイル数: {summary['files']}")
    print(f"行数: {summary['rows']}")
    print(f"書き込み: {summary['written']}")
    print(f"スキップ（移行先に既存）: {summary['skipped_existing']}")
    print(f"スキップ（新形式にできない）: {summary['skipped_broken']}")
    if summary["problems"]:
        print(f"問題のあったファイル: {len(summary['problems'])}件")
        for problem in summary["problems"]:
            print(f"  - {problem}")
    else:
        print("問題: なし")


if __name__ == "__main__":
    main()
