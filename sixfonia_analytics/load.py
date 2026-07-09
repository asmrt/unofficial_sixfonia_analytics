"""日次CSV群のロードと combined_df 構築。

新スキーマ（view_date列あり）と旧スキーマ（列なし→ファイル名から補完）の両対応。
参照先は正（sixfonia_yt_analytics/<ch>/）を優先し、
見つからなければ旧 YouTube_Data/ にフォールバックする。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config, metrics


def _candidate_dirs(channel_name: str) -> list[Path]:
    return [config.channel_dir(channel_name), config.LEGACY_DATA_DIR]


def list_stats_files(channel_name: str, years: list[str] | None = None,
                     folder: Path | None = None) -> list[Path]:
    """チャンネルの日次統計CSVを日付昇順で返す。同一日付は正フォルダ優先。"""
    dirs = [folder] if folder else _candidate_dirs(channel_name)
    found: dict[str, Path] = {}  # date_str -> path（先勝ち=正フォルダ優先）
    for d in dirs:
        if d is None or not d.is_dir():
            continue
        for p in sorted(d.glob("*.csv")):
            m = config.STATS_FILE_RE.match(p.name)
            if not m or m.group(1) != channel_name:
                continue
            date_str = m.group(2)
            if years and not any(date_str.startswith(y) for y in years):
                continue
            found.setdefault(date_str, p)
    return [found[k] for k in sorted(found)]


def load_stats_csv(path: Path) -> pd.DataFrame | None:
    """1ファイル読み込み。view_date 列がなければファイル名から補完する。"""
    try:
        df = pd.read_csv(path)
    except Exception as e:  # 壊れたファイルはスキップ
        print(f"[WARN] 読み込み失敗: {path} — {e}")
        return None
    m = config.STATS_FILE_RE.match(path.name)
    if "view_date" not in df.columns and m:
        df["view_date"] = m.group(2)
    df["view_date"] = pd.to_datetime(df["view_date"].astype(str), format="%Y%m%d")
    for col in ["viewCount", "likeCount", "commentCount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build_combined_df(channel_name: str, years: list[str] | None = None,
                      folder: Path | None = None) -> pd.DataFrame:
    """チャンネルの全日次CSVを結合し *Diff 列を付与した DataFrame を返す。"""
    files = list_stats_files(channel_name, years=years, folder=folder)
    if not files:
        raise RuntimeError(f"対象ファイルが0件です: channel={channel_name}")
    print(f"対象ファイル数: {len(files)}  ({files[0].name} 〜 {files[-1].name})")

    frames = [df for f in files if (df := load_stats_csv(f)) is not None]
    combined = (
        pd.concat(frames, ignore_index=True)
        .sort_values(["videoId", "view_date"])
        .reset_index(drop=True)
    )
    combined = metrics.add_diff_columns(combined)
    print(f"combined_df shape: {combined.shape}")
    return combined


def available_dates(channel_names: list[str] | None = None) -> list[str]:
    """全チャンネル共通で存在する日付（YYYYMMDD）を昇順で返す。"""
    names = channel_names or config.CHANNEL_NAMES
    sets = []
    for name in names:
        dates = {config.STATS_FILE_RE.match(p.name).group(2)
                 for p in list_stats_files(name)}
        if dates:
            sets.append(dates)
    if not sets:
        return []
    common = set.intersection(*sets)
    return sorted(common)


def load_latest_days(n_days: int = 2, channel_names: list[str] | None = None
                     ) -> dict[str, dict[str, pd.DataFrame]]:
    """直近n日分を {date_str: {channel: df}} で返す（日付ハードコード廃止）。

    旧07/14では日付を手入力していたが、フォルダ内の最新日付を自動検出する。
    """
    names = channel_names or config.CHANNEL_NAMES
    dates = available_dates(names)[-n_days:]
    if len(dates) < n_days:
        raise RuntimeError(f"直近{n_days}日分のデータが揃っていません（検出: {dates}）")
    print(f"対象日付: {dates}")

    out: dict[str, dict[str, pd.DataFrame]] = {d: {} for d in dates}
    for name in names:
        by_date = {config.STATS_FILE_RE.match(p.name).group(2): p
                   for p in list_stats_files(name)}
        for d in dates:
            df = load_stats_csv(by_date[d])
            if df is not None:
                out[d][name] = df
    return out
