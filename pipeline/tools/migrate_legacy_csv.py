# -*- coding: utf-8 -*-
"""既存（Colab 運用）の統計 CSV を、本パイプラインの新バケットへ移行するツール。

移行元は旧バケットの `{prefix}/{channel}/{channel}_video_statistics_{YYYYMMDD}.csv`
（6列: videoId, viewCount, likeCount, commentCount, videoURL, view_date）。
本パイプラインの CSV は `channel` 列を足した7列なので、変換は `channel` を足すだけでよい
（サムネイル列は元から無いので変換不要）。

デフォルトは dry run（何も書き込まない）。実行するには `--apply` を付ける。
移行先に同名のファイルが既にある場合は `--overwrite` を付けない限り上書きしない
（パイプライン稼働後の日付を移行で壊さないため）。

怪しい行（view_date や件数列がおかしい行）は取り込みつつ、`--log-file`
（デフォルト: migration_suspicious_rows.csv）に1行ずつ書き出す。

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

    戻り値は (変換後のCSVテキスト, 問題メッセージのリスト, 怪しい行のリスト)。
    行は削除しない（問題があっても報告するだけで変換は続ける）。
    すでに新形式（7列）のファイルは「変換済み」として、中身を変えずに返す。
    怪しい行のリストは {"line": CSVの行番号（ヘッダーが1、最初のデータ行が2）,
    "column": 列名, "value": 元の値, "reason": 理由} の辞書のリスト。
    """
    problems = []
    rows = list(csv.reader(io.StringIO(text)))
    # 末尾の完全な空行は無視する
    while rows and rows[-1] == []:
        rows.pop()

    if not rows:
        return text, ["空のファイル"], []

    header = rows[0]

    if header == NEW_COLUMNS:
        problems.append("変換済み")
        return text, problems, []

    if header != LEGACY_COLUMNS:
        problems.append(f"列が想定と違う: {header}")
        return text, problems, []

    # 行ごとの問題は、同じ内容を何千行分も並べないよう「件数と最初の例」にまとめる
    bad_dates = []
    bad_counts = {col: [] for col in ("viewCount", "likeCount", "commentCount")}
    bad_widths = []
    suspicious = []

    out_rows = [NEW_COLUMNS]
    for line_no, row in enumerate(rows[1:], start=2):
        record = dict(zip(LEGACY_COLUMNS, row))

        # 列数が違う行は、移行先で外部テーブルが読めずクエリ全体がエラーになる
        if len(row) != len(LEGACY_COLUMNS):
            bad_widths.append(len(row))
            suspicious.append({
                "line": line_no,
                "column": "(列数)",
                "value": str(len(row)),
                "reason": f"列数が{len(LEGACY_COLUMNS)}でない",
            })

        view_date = record.get("view_date", "")
        if view_date != file_date:
            bad_dates.append(view_date)
            suspicious.append({
                "line": line_no,
                "column": "view_date",
                "value": view_date,
                "reason": f"ファイル名の日付（{file_date}）と違う",
            })

        for col in ("viewCount", "likeCount", "commentCount"):
            value = record.get(col, "")
            if value != "" and not _INT_RE.match(value):
                bad_counts[col].append(value)
                suspicious.append({
                    "line": line_no,
                    "column": col,
                    "value": value,
                    "reason": "整数の形式でない",
                })

        out_rows.append(row + [channel])

    if bad_widths:
        problems.append(f"列数が{len(LEGACY_COLUMNS)}でない行が {len(bad_widths)}行（例: {bad_widths[0]}列）")
    if bad_dates:
        problems.append(
            f"view_date がファイル名の日付（{file_date}）と違う行が {len(bad_dates)}行（例: {bad_dates[0]!r}）"
        )
    for col, values in bad_counts.items():
        if values:
            problems.append(f"{col} が整数の形式でない行が {len(values)}行（例: {values[0]!r}）")

    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(out_rows)
    return buf.getvalue(), problems, suspicious


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


def migrate(source_bucket, source_prefix, dest_bucket, *, channels=None, apply=False, overwrite=False,
            limit=None, client=None):
    """旧バケットの統計CSVを新バケットへ移行する。

    デフォルト（apply=False）は dry run で、何も書き込まずに実施予定の内容を表示するだけ。
    移行先に同名のオブジェクトが既にある場合は overwrite=True でない限りスキップする
    （パイプライン稼働後に書かれた日付を移行で上書きしないため）。

    limit を指定すると、対象ファイル数（summary["files"]）がその件数に達した時点で処理を打ち切る。
    ファイル名の形式が想定と違うものや --channel で絞り込まれて対象外になったものは
    対象ファイル数に数えないので limit も消費しない。少量で試すときに使う。

    戻り値: {"files": 対象ファイル数, "rows": 変換した行数, "written": 実際に書き込んだ数,
             "skipped_existing": 移行先に既にあってスキップした数,
             "skipped_broken": 新形式にできずスキップした数, "problems": 問題メッセージのリスト,
             "suspicious_rows": 怪しい行のリスト（各要素に "file" キーを追加）}
    """
    if client is None:
        client = storage.Client()

    src_bucket = client.bucket(source_bucket)
    dst_bucket = client.bucket(dest_bucket)

    summary = {"files": 0, "rows": 0, "written": 0, "skipped_existing": 0,
               "skipped_broken": 0, "problems": [], "suspicious_rows": []}

    for blob in src_bucket.list_blobs(prefix=source_prefix):
        parsed = parse_blob_name(blob.name)
        if parsed is None:
            summary["problems"].append(f"{blob.name}: ファイル名の形式が想定と違う")
            continue

        channel, file_date = parsed
        if channels and channel not in channels:
            continue

        # スキップ（既存・変換不可）で continue する場合も打ち切れるよう、数える前に判定する
        if limit is not None and summary["files"] >= limit:
            break
        summary["files"] += 1
        text = blob.download_as_text()
        converted, problems, suspicious = convert_csv(text, channel, file_date)
        for problem in problems:
            summary["problems"].append(f"{blob.name}: {problem}")

        # 新形式にできなかったファイルは書き込まない。
        # 列の並びが違う CSV が移行先に混じると、外部テーブル経由のクエリ全体が失敗するため
        if any(p.startswith(prefix) for p in problems for prefix in NOT_WRITABLE_PROBLEMS):
            summary["skipped_broken"] += 1
            print(f"[SKIP] {blob.name} は新形式に変換できないため書き込みません")
            continue

        for row in suspicious:
            summary["suspicious_rows"].append({**row, "file": blob.name})

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


def write_suspicious_rows_csv(suspicious_rows, path):
    """怪しい行のリストを CSV（file,line,column,value,reason）として書き出す。

    リストが空の場合は何もしない（空のログファイルを作らない）。
    """
    if not suspicious_rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "line", "column", "value", "reason"])
        writer.writeheader()
        writer.writerows(suspicious_rows)


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
    parser.add_argument("--limit", type=int, default=None,
                         help="対象ファイル数がこの件数に達したら打ち切る（--channel と併用可。少量で試すとき用）")
    parser.add_argument("--log-file", default="migration_suspicious_rows.csv",
                         help="怪しい行の一覧を書き出すCSVのパス（デフォルト: migration_suspicious_rows.csv）")
    args = parser.parse_args(argv)

    summary = migrate(
        args.source_bucket,
        args.source_prefix,
        args.dest_bucket,
        channels=args.channels,
        apply=args.apply,
        overwrite=args.overwrite,
        limit=args.limit,
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

    suspicious_rows = summary["suspicious_rows"]
    if suspicious_rows:
        write_suspicious_rows_csv(suspicious_rows, args.log_file)
        if args.apply:
            note = "（取り込みは行っています。BigQuery でエラーになる場合はこの一覧で直す）"
        else:
            note = "（--apply 時もそのまま書き込みます）"
        print(f"怪しい行: {len(suspicious_rows)}件 → {args.log_file}{note}")
    else:
        print("怪しい行: なし")


if __name__ == "__main__":
    main()
