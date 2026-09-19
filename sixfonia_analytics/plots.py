"""グラフ描画。テーマ切替でクイズ用/ブログ用を統合（旧05/06/10）。

THEMES:
  quiz  : ダークネイビー+シアン/オレンジ（Xクイズ出題用）
  blog_dark  : 黒+深紅（ブログ用ダーク）
  blog_light : グレー+白パネル+深紅（ブログ用ライト）
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd


def setup_japanese_font() -> None:
    """日本語フォントを有効化する（Colabでは事前に %pip install japanize-matplotlib）。"""
    import japanize_matplotlib  # noqa: F401

    plt.rcParams["axes.unicode_minus"] = False


THEMES = {
    "quiz": {
        "bg": "#0d0d1a", "panel": "#12122a", "main": "#00e5ff",
        "sub": "#ff6d00", "text": "#e0e0e0", "grid": "#2a2a4a",
    },
    "blog_dark": {
        "bg": "#111111", "panel": "#1a1a1a", "main": "#c0392b",
        "sub": "#8b1a1a", "text": "#e8d5d5", "grid": "#2e1a1a",
    },
    "blog_light": {
        "bg": "#4a4a4a", "panel": "#FFFFFF", "main": "#c0392b",
        "sub": "#8b1a1a", "text": "#FFFFFF", "grid": "#4a3030",
    },
}


def _style_axis(ax, theme: dict, fontsize: int = 10) -> None:
    ax.set_facecolor(theme["panel"])
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax.tick_params(colors=theme["text"], labelsize=fontsize)
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right",
             color=theme["text"], fontsize=fontsize)
    ax.grid(True, color=theme["grid"], linewidth=0.7)
    for spine in ax.spines.values():
        spine.set_edgecolor(theme["grid"])


# ============================================================
# 分析用: 累積+日次の2段グラフ（旧06 plot_view_trend）
# ============================================================
def plot_view_trend(df: pd.DataFrame, video_id: str,
                    y_lim: tuple[int, int] | None = None,
                    y_step: int | None = None) -> None:
    if df.empty:
        print(f"[WARN] {video_id} のデータが空です。")
        return
    fig, axes = plt.subplots(2, 1, figsize=(12, 10), sharex=True)

    axes[0].plot(df["view_date"], df["viewCount"], marker="o")
    axes[0].set_title(f"Cumulative View Count — {video_id}")
    axes[0].set_ylabel("Cumulative View Count")
    if y_lim:
        axes[0].set_ylim(*y_lim)
    if y_step:
        axes[0].yaxis.set_major_locator(mticker.MultipleLocator(y_step))
    axes[0].grid(True)

    axes[1].plot(df["view_date"], df["viewCountDiff"], marker="o", color="orange")
    axes[1].set_title("Daily View Count Difference")
    axes[1].set_ylabel("Daily Diff")
    axes[1].grid(True)

    plt.xticks(rotation=30)
    plt.tight_layout()
    plt.show()


# ============================================================
# クイズ出題用（旧06/10 plot_view_trend_sns: 9:16 / 4:5）
# ============================================================
def plot_quiz_graph(df: pd.DataFrame, *, layout: str = "4x5",
                    header_title: str = "シクフォニ再生数クイズ（非公式）",
                    header_question: str = "以下グラフの動画はなんでしょう？",
                    header_note: str = "対象：グループまたはメンバーさん個人チャンネルの長尺動画",
                    masked_title: str = "「【???????】?????????【????????】」",
                    footer_text: str = "正解は後日発表！",
                    ref_line: int | None = 100_000,
                    y_lim: tuple[int, int] | None = None,
                    y_step: int = 20_000,
                    save_path: str = "quiz_graph.png") -> None:
    """タイトルを伏せた出題用2段グラフ。layout: "9x16" or "4x5"（X投稿は4:5が安全）。"""
    if df.empty:
        print("[WARN] データが空です。")
        return
    th = THEMES["quiz"]
    figsize = (9, 16) if layout == "9x16" else (8, 10)
    fig = plt.figure(figsize=figsize, facecolor=th["bg"])
    gs = fig.add_gridspec(4, 1, height_ratios=[1.0, 3.2, 3.2, 0.7], hspace=0.32,
                          left=0.12, right=0.94, top=0.94, bottom=0.08)

    ax_header = fig.add_subplot(gs[0])
    ax_header.set_facecolor(th["bg"])
    ax_header.axis("off")
    ax_header.text(0.5, 0.88, header_title, ha="center", va="top", fontsize=20,
                   fontweight="bold", color=th["main"], transform=ax_header.transAxes)
    ax_header.text(0.5, 0.48, header_question, ha="center", va="top", fontsize=15,
                   color=th["text"], transform=ax_header.transAxes)
    ax_header.text(0.5, 0.12, header_note, ha="center", va="top", fontsize=11,
                   color=th["text"], transform=ax_header.transAxes)

    ax1 = fig.add_subplot(gs[1])
    ax1.plot(df["view_date"], df["viewCount"], color=th["main"], linewidth=2.2)
    if ref_line:
        ax1.axhline(y=ref_line, color=th["sub"], linestyle="--", linewidth=1.2, alpha=0.75)
        ax1.text(df["view_date"].iloc[0], ref_line * 1.015, f"{ref_line:,}",
                 color=th["sub"], fontsize=9, alpha=0.95)
    ax1.set_ylabel("累積再生数", color=th["text"], fontsize=11)
    ax1.set_title(f"{masked_title}累積再生数推移", color=th["main"], fontsize=13,
                  fontweight="bold", pad=8)
    if y_lim:
        ax1.set_ylim(*y_lim)
    ax1.yaxis.set_major_locator(mticker.MultipleLocator(y_step))
    _style_axis(ax1, th, fontsize=9)

    ax2 = fig.add_subplot(gs[2], sharex=ax1)
    ax2.plot(df["view_date"], df["viewCountDiff"], color=th["sub"], linewidth=2.0)
    ax2.set_ylabel("日次再生数", color=th["text"], fontsize=11)
    ax2.set_title(f"{masked_title}日次再生数推移", color=th["sub"], fontsize=13,
                  fontweight="bold", pad=8)
    _style_axis(ax2, th, fontsize=9)
    plt.setp(ax1.xaxis.get_majorticklabels(), visible=False)

    ax_footer = fig.add_subplot(gs[3])
    ax_footer.set_facecolor(th["bg"])
    ax_footer.axis("off")
    ax_footer.text(0.5, 0.55, footer_text, ha="center", va="center", fontsize=11,
                   color=th["text"], transform=ax_footer.transAxes)

    # X投稿で見切れないよう bbox_inches="tight" は付けない
    plt.savefig(save_path, dpi=180, facecolor=th["bg"])
    print(f"Saved: {save_path}")
    plt.show()


# ============================================================
# ブログ/回答用 単発グラフ（旧10）
# ============================================================
def plot_single_trend(df: pd.DataFrame, *, kind: str = "cumulative",
                      theme: str = "blog_dark",
                      date_range: tuple[str, str] | None = None,
                      ref_line: int | None = None,
                      y_lim: tuple[int, int] | None = None,
                      y_step: int | None = None,
                      save_path: str = "trend.png") -> None:
    """kind: "cumulative"（累積） or "daily"（日次）。date_range で期間ズーム。"""
    if df.empty:
        print("[WARN] データが空です。")
        return
    plot_df = df
    if date_range:
        start, end = date_range
        plot_df = df[(df["view_date"] >= start) & (df["view_date"] <= end)]
        if plot_df.empty:
            print(f"[WARN] 範囲 {start} 〜 {end} にデータがありません。")
            return

    th = THEMES[theme]
    col = "viewCount" if kind == "cumulative" else "viewCountDiff"
    fig, ax = plt.subplots(figsize=(16, 9), facecolor=th["bg"])
    ax.plot(plot_df["view_date"], plot_df[col], color=th["main"], linewidth=2.5,
            marker="o", markersize=3, markerfacecolor=th["main"])
    if ref_line:
        ax.axhline(y=ref_line, color=th["sub"], linestyle="--", linewidth=1.2, alpha=0.9)
        ax.text(plot_df["view_date"].iloc[0], ref_line * 1.018, f"{ref_line:,}",
                color=th["sub"], fontsize=12)
    if y_lim:
        ax.set_ylim(*y_lim)
    else:
        ax.set_ylim(bottom=0)
    if y_step:
        ax.yaxis.set_major_locator(mticker.MultipleLocator(y_step))
    _style_axis(ax, th, fontsize=13)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight", facecolor=th["bg"])
    print(f"Saved: {save_path}")
    plt.show()


# ============================================================
# 補間入り推移グラフ（旧14: actual/estimated 区別）
# ============================================================
def plot_interpolated_series(series_df: pd.DataFrame, value_col: str,
                             title: str = "") -> None:
    """metrics.prepare_continuous_series の結果を actual/estimated で塗り分けて描画。"""
    plt.figure(figsize=(14, 7))
    actual = series_df[series_df["data_type"] == "actual"]
    est = series_df[series_df["data_type"] == "estimated"]
    plt.plot(actual["view_date"], actual[value_col], marker="o", linestyle="-",
             color="blue", label="Actual")
    if not est.empty:
        plt.plot(est["view_date"], est[value_col], marker="x", linestyle="--",
                 color="red", label="Estimated")
    plt.title(title)
    plt.xlabel("Date")
    plt.ylabel(value_col)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.show()
