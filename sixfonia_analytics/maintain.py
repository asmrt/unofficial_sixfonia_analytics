"""過去CSVの棚卸し・スキーマ整形・整合性チェック・GCSアップロード（旧13）。

新収集分（collect.py）は最初から MASTER_COLUMNS で保存されるため、
このモジュールは主に過去データの一括整備と検証に使う。
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from . import config


def _target_files(folder: Path) -> list[Path]:
    return [p for p in sorted(folder.glob("*.csv"))
            if config.STATS_FILE_RE.match(p.name)
            and "column_inventory" not in p.name]


# ============================================================
# Phase 1: 列の棚卸し（読み取りのみ・破壊なし）
# ============================================================
def inventory_columns(folder: Path, encoding: str = "utf-8") -> None:
    """フォルダ内の統計CSVの列構成と変化履歴を表示し、column_inventory を出力する。"""
    files = _target_files(folder)
    print(f"\n{'=' * 60}\n📂 {folder.name}\n{'=' * 60}")
    print(f"対象ファイル数: {len(files)}")
    if not files:
        return

    all_cols_ordered: list[str] = []
    seen: set[str] = set()
    file_columns: dict[str, list[str]] = {}
    for f in files:
        try:
            with f.open(encoding=encoding, newline="") as fh:
                cols = [c.strip() for c in next(csv.reader(fh))]
        except Exception as e:
            print(f"  [ERROR] {f.name}: {e}")
            continue
        file_columns[f.name] = cols
        for c in cols:
            if c not in seen:
                seen.add(c)
                all_cols_ordered.append(c)

    sorted_files = sorted(file_columns)
    print("\n【列の変化履歴】")
    for c in all_cols_ordered:
        presence = [c in file_columns[f] for f in sorted_files]
        dates = [config.STATS_FILE_RE.match(f).group(2) for f in sorted_files]
        if all(presence):
            print(f"  ✅ {c:30s}  全期間に存在")
            continue
        events = []
        for i in range(len(presence)):
            if i == 0 and presence[i]:
                events.append(f"➕ 初出: {dates[i]}")
            elif i > 0 and presence[i] != presence[i - 1]:
                events.append(f"➕ 追加: {dates[i]}" if presence[i] else f"➖ 削除: {dates[i]}")
        status = "（現在あり）" if presence[-1] else "（現在なし）"
        print(f"  ⚠️  {c:30s}  {'  →  '.join(events)}  {status}")

    inv_path = folder / f"column_inventory_{folder.name}.csv"
    with inv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["filename", "date"] + all_cols_ordered)
        for fname in sorted_files:
            m = config.STATS_FILE_RE.match(fname)
            w.writerow([fname, m.group(2) if m else ""]
                       + ["1" if c in file_columns[fname] else "" for c in all_cols_ordered])
    print(f"\n✅ {inv_path.name} を出力しました")


# ============================================================
# Phase 2: スキーマ整形（バックアップ付き上書き）
# ============================================================
def align_schema(folder: Path, backup_dir: Path | None = None,
                 master: list[str] | None = None, dry_run: bool = True,
                 encoding: str = "utf-8") -> None:
    """MASTER_COLUMNS に列を揃える。dry_run=True では変更せず補完予定を表示。

    不足列の補完規則:
      videoURL  → videoId から生成
      view_date → ファイル名の日付を使用
      その他    → 空埋め
    """
    master = master or config.MASTER_COLUMNS
    files = _target_files(folder)
    print(f"\n📂 {folder.name}  ({len(files)} ファイル)  dry_run={dry_run}")
    print(f"   マスタースキーマ: {master}")

    if dry_run:
        missing_count = {c: 0 for c in master}
        for f in files:
            with f.open(encoding=encoding, newline="") as fh:
                header = [c.strip() for c in next(csv.reader(fh))]
            for c in master:
                if c not in header:
                    missing_count[c] += 1
        for c, cnt in missing_count.items():
            fill = ("（videoIdから自動生成）" if c == "videoURL"
                    else "（ファイル名の日付を使用）" if c == "view_date" else "（空埋め）")
            print(f"   {'⚠️ ' if cnt else '✅'} {c}: "
                  + (f"{cnt} ファイルで補完が必要 {fill}" if cnt else "全ファイルに存在"))
        print("\n問題なければ dry_run=False で再実行してください")
        return

    backup_dir = backup_dir or (folder.parent / f"{folder.name}_backup")
    backup_dir.mkdir(parents=True, exist_ok=True)

    ok, updated = 0, 0
    for f in files:
        try:
            shutil.copy2(f, backup_dir / f.name)  # 1. バックアップ
            with f.open(encoding=encoding, newline="") as fh:
                reader = csv.DictReader(fh)
                src_cols = [c.strip() for c in (reader.fieldnames or [])]
                rows = list(reader)

            missing = [c for c in master if c not in src_cols]
            if not missing and src_cols == master:
                ok += 1
                continue  # 既に正しい形式

            file_date = config.STATS_FILE_RE.match(f.name).group(2)
            for row in rows:
                for c in missing:
                    if c == "videoURL":
                        vid = row.get("videoId", "").strip()
                        row["videoURL"] = f"https://www.youtube.com/watch?v={vid}" if vid else ""
                    elif c == "view_date":
                        row["view_date"] = file_date
                    else:
                        row[c] = ""

            with f.open("w", encoding=encoding, newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=master, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            updated += 1
            ok += 1
        except Exception as e:
            print(f"  ❌ {f.name}: ERROR {e}")
    print(f"  ✅ 完了: {ok}/{len(files)} (上書き更新: {updated}, バックアップ: {backup_dir})")


# ============================================================
# 整合性チェック
# ============================================================
def verify_schema(folders: list[Path] | None = None,
                  master: list[str] | None = None, encoding: str = "utf-8") -> bool:
    """全フォルダの全統計CSVがマスタースキーマ通りかを確認する。"""
    master = master or config.MASTER_COLUMNS
    folders = folders or [config.channel_dir(n) for n in config.CHANNEL_NAMES]
    errors: list[str] = []

    print("🔍 列構成チェックを開始します...\n")
    for folder in folders:
        if not folder.exists():
            continue
        files = _target_files(folder)
        print(f"📂 {folder.name}: {len(files)} ファイルをチェック中...")
        for f in files:
            try:
                with f.open(encoding=encoding, newline="") as fh:
                    header = next(csv.reader(fh))
                if header != master:
                    errors.append(f"  ❌ {f.name}: 列不一致 {header}")
            except Exception as e:
                errors.append(f"  💀 {f.name}: 読み取りエラー {e}")

    print(f"\n{'=' * 50}")
    if errors:
        print(f"❌ {len(errors)} 件の不備があります:")
        for err in errors[:10]:
            print(err)
        return False
    print("✅ 全フォルダの全ファイルが正しい列構成です")
    return True


# ============================================================
# GCS アップロード
# ============================================================
def upload_to_gcs(channel_names: list[str] | None = None,
                  bucket_name: str | None = None, prefix: str | None = None) -> None:
    """チャンネル別フォルダの統計CSVを GCS へアップロードする。

    事前に notebook 側で auth.authenticate_user() を実行しておくこと。
    """
    from google.cloud import storage  # 遅延import

    bucket_name = bucket_name or config.GCS_BUCKET
    prefix = prefix or config.GCS_PREFIX
    names = channel_names or config.CHANNEL_NAMES

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    total, errors = 0, []
    for name in names:
        files = _target_files(config.channel_dir(name))
        print(f"\n📂 {name}  ({len(files)} ファイル)")
        for f in files:
            gcs_path = f"{prefix}/{name}/{f.name}"
            try:
                bucket.blob(gcs_path).upload_from_filename(str(f))
                total += 1
            except Exception as e:
                errors.append((gcs_path, str(e)))
                print(f"  ❌ {f.name}: {e}")
        print(f"  ✅ アップロード完了")

    print(f"\n{'=' * 50}\n完了: {total} ファイル")
    if errors:
        print(f"エラー: {len(errors)} 件")
        for path, msg in errors:
            print(f"  {path}: {msg}")
