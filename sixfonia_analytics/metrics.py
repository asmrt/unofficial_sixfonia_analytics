"""集計・指標計算。

daily_ranking の4指標（決定事項: すべて維持）:
  1. simple_daily_diff        単純差分（前日→当日）
  2. sudden_increase_scores(method="mean")    急増スコア 平均ベース
  3. sudden_increase_scores(method="median")  急増スコア 中央値ベース
  4. daily_increase_change    日次増加の変化（3日差分）
"""

from __future__ import annotations

import pandas as pd

from .enrich import thumbnail_url


# ============================================================
# 共通
# ============================================================
def add_diff_columns(df: pd.DataFrame) -> pd.DataFrame:
    """videoIdごとに日次差分列 (*Diff) を付与する。"""
    df = df.sort_values(["videoId", "view_date"]).reset_index(drop=True)
    for col in ["viewCount", "likeCount", "commentCount"]:
        if col in df.columns:
            df[f"{col}Diff"] = df.groupby("videoId")[col].diff()
    return df


# ============================================================
# 月次集計（旧06）
# ============================================================
def get_last_complete_month_range(df: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    """直前の完全月の (開始日, 終了日) を返す。"""
    max_date = df["view_date"].max()
    end = max_date.replace(day=1) - pd.Timedelta(days=1)
    start = end.replace(day=1)
    print(f"最新日付: {max_date.date()} → 集計対象月: {start.date()} 〜 {end.date()}")
    return start, end


def aggregate_monthly_gains(df: pd.DataFrame) -> pd.DataFrame:
    """直前の完全月の動画ごと月次増加量（再生/いいね/コメント）。"""
    start, end = get_last_complete_month_range(df)
    last_month = df[(df["view_date"] >= start) & (df["view_date"] <= end)]
    gains = (
        last_month.groupby("videoId")
        .agg(
            monthly_views_gain=("viewCountDiff", "sum"),
            monthly_likes_gain=("likeCountDiff", "sum"),
            monthly_comments_gain=("commentCountDiff", "sum"),
        )
        .reset_index()
    )
    gains["thumbnail_url"] = gains["videoId"].apply(thumbnail_url)
    gains["video_title"] = gains["videoId"]  # enrich.add_video_titles で上書き可
    return gains


# ============================================================
# 特定動画（旧06）
# ============================================================
def filter_video(df: pd.DataFrame, video_id: str, year: int | None = None,
                 target_views: int = 1_000_000) -> pd.DataFrame:
    """特定動画の推移を抽出。目標再生数までのカウントダウン列付き。"""
    result = df[df["videoId"] == video_id].copy()
    if year is not None:
        result = result[result["view_date"].dt.year == year]
    result = result.sort_values("view_date").reset_index(drop=True)
    result["views_to_target"] = target_views - result["viewCount"]
    result["thumbnail_url"] = thumbnail_url(video_id)
    return result


def prepare_continuous_series(df: pd.DataFrame, value_col: str = "viewCountDiff",
                              date_range: tuple[str, str] | None = None) -> pd.DataFrame:
    """欠測日を補間した連続系列を返す（旧05/14）。

    data_type 列で actual / estimated を区別する。
    date_range=(start, end) で期間ズーム（旧05の zoom_config 相当）。
    """
    sub = df.sort_values("view_date").set_index("view_date")
    full = pd.date_range(sub.index.min(), sub.index.max(), freq="D")
    out = sub[[value_col]].reindex(full)
    out.index.name = "view_date"
    out["data_type"] = "actual"
    out.loc[out[value_col].isna(), "data_type"] = "estimated"
    out[value_col] = out[value_col].interpolate(method="linear")
    out = out.reset_index()
    if date_range:
        start, end = date_range
        out = out[(out["view_date"] >= start) & (out["view_date"] <= end)]
    return out


# ============================================================
# 直近N日上昇率（旧06）
# ============================================================
def get_recent_daily_gains(df: pd.DataFrame, n_days: int = 3) -> pd.DataFrame:
    """全動画の直近n日分の日次増加と前日比差分・上昇率[%]を返す。"""
    max_date = df["view_date"].max()
    cutoff = max_date - pd.Timedelta(days=n_days - 1)

    recent = (
        df[df["view_date"] >= cutoff]
        [["videoId", "view_date", "viewCount", "viewCountDiff"]]
        .copy()
        .sort_values(["videoId", "view_date"])
        .reset_index(drop=True)
    )
    recent["prev_diff"] = recent.groupby("videoId")["viewCountDiff"].shift(1)
    recent["diff_delta"] = recent.groupby("videoId")["viewCountDiff"].diff()
    recent["diff_delta_pct"] = (
        recent["diff_delta"] / recent["prev_diff"].replace(0, float("nan")) * 100
    ).round(1)
    recent["thumbnail_url"] = recent["videoId"].apply(thumbnail_url)
    return recent.drop(columns=["prev_diff"])


# ============================================================
# 指標1: 単純差分（旧07/14）
# ============================================================
def simple_daily_diff(df_latest: pd.DataFrame, df_prev: pd.DataFrame,
                      channel_name: str) -> pd.DataFrame:
    """2日分の統計をマージし viewCount_difference を計算する。"""
    merged = pd.merge(
        df_latest[["videoId", "viewCount", "videoURL"]],
        df_prev[["videoId", "viewCount"]],
        on="videoId",
        suffixes=("_latest", "_prev"),
    )
    merged["viewCount_difference"] = merged["viewCount_latest"] - merged["viewCount_prev"]
    merged["channelName"] = channel_name
    merged["thumbnailURL"] = merged["videoId"].apply(thumbnail_url)
    return merged


# ============================================================
# 指標2/3: 急増スコア（旧14: 平均ベース / 中央値ベース）
# ============================================================
def baseline_daily_increase(history_df: pd.DataFrame, method: str = "median") -> pd.DataFrame:
    """動画ごとの日次増加のベースライン（正の増加のみ対象）。

    method: "median"（中央値ベース） or "mean"（平均ベース）
    """
    df = history_df.sort_values(["videoId", "view_date"]).copy()
    df["daily_diff"] = df.groupby("videoId")["viewCount"].diff()
    agg = (
        df.groupby("videoId")["daily_diff"]
        .apply(lambda x: getattr(x[x > 0], method)())
        .reset_index()
        .rename(columns={"daily_diff": f"{method}_daily_increase"})
    )
    agg[f"{method}_daily_increase"] = agg[f"{method}_daily_increase"].fillna(0)
    return agg


def sudden_increase_scores(latest_diff_df: pd.DataFrame, history_df: pd.DataFrame,
                           method: str = "median") -> pd.DataFrame:
    """急増スコア = 最新の日次増加 / (ベースライン日次増加 + 1)。

    動画のこれまでの伸び方を分母に取ることで、
    「普段より急に伸びた」動画を浮かび上がらせる。
    """
    base_col = f"{method}_daily_increase"
    base = baseline_daily_increase(history_df, method=method)
    merged = pd.merge(latest_diff_df, base, on="videoId", how="left")
    merged[base_col] = merged[base_col].fillna(0)
    merged["sudden_increase_score"] = (
        merged["viewCount_difference"] / (merged[base_col] + 1)
    )
    return merged


# ============================================================
# 指標4: 日次増加の変化（旧14: 3日差分）
# ============================================================
def daily_increase_change(df_current: pd.DataFrame, df_1d_prior: pd.DataFrame,
                          df_2d_prior: pd.DataFrame, channel_name: str) -> pd.DataFrame:
    """日次増加の加速/減速: (当日増加) - (前日増加)。"""
    merged = pd.merge(
        df_current[["videoId", "viewCount", "videoURL"]],
        df_1d_prior[["videoId", "viewCount"]],
        on="videoId",
        suffixes=("_current", "_1d_prior"),
    )
    merged = pd.merge(
        merged,
        df_2d_prior[["videoId", "viewCount"]].rename(columns={"viewCount": "viewCount_2d_prior"}),
        on="videoId",
    )
    merged["daily_increase_latest"] = merged["viewCount_current"] - merged["viewCount_1d_prior"]
    merged["daily_increase_previous"] = merged["viewCount_1d_prior"] - merged["viewCount_2d_prior"]
    merged["daily_increase_change"] = (
        merged["daily_increase_latest"] - merged["daily_increase_previous"]
    )
    merged["channelName"] = channel_name
    merged["thumbnailURL"] = merged["videoId"].apply(thumbnail_url)
    return merged


# ============================================================
# Shorts / 長尺の分類（旧07/11）
# ============================================================
def classify_video_type(duration_seconds) -> str:
    if pd.isna(duration_seconds):
        return "Unknown"
    return "Shorts" if duration_seconds <= 60 else "Long-form"
