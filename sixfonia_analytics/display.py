"""notebook内でのHTML表示（ランキングカード・テーブル・サムネグリッド）。"""

from __future__ import annotations

import pandas as pd
from IPython.display import HTML, display


def _first_valid(row, *cols):
    """行から最初の非欠損値を返す（NaNは真値扱いされるため or では判定できない）。"""
    for col in cols:
        val = row.get(col)
        if val is not None and pd.notna(val):
            return val
    return None


def display_ranking_cards(df: pd.DataFrame, metric_col: str, title: str,
                          top_n: int = 10) -> None:
    """サムネ付きカード形式のランキング（旧06）。"""
    ranked = df.sort_values(by=metric_col, ascending=False).head(top_n)
    label = metric_col.replace("_", " ").title()
    cards = "".join(
        f"""<div style='border:1px solid #ddd;padding:10px;border-radius:5px;
                        text-align:center;width:200px'>
                <p><b>{row.get('video_title', row['videoId'])}</b></p>
                <p style='font-size:.85em;color:#666'>{row['videoId']}</p>
                <p><b>{label}:</b> {row[metric_col]:,.0f}</p>
                <img src='{_first_valid(row, 'thumbnail_url', 'thumbnailURL')}'
                     width='160' height='90' style='border-radius:3px'>
            </div>"""
        for _, row in ranked.iterrows()
    )
    display(HTML(f"<h3>{title}</h3>"
                 f"<div style='display:flex;flex-wrap:wrap;gap:16px'>{cards}</div>"))


def display_ranking_table(df: pd.DataFrame, title: str, metric_col: str = "viewCount_difference",
                          top_n: int = 10, sort_col: str | None = None) -> None:
    """順位・サムネ・タイトルリンク付きのランキングテーブル（旧07）。

    metric_col: 表に数値表示する列。
    sort_col  : 順位付けに使う列（省略時は metric_col）。急増スコアのように
                「スコア順に並べつつ表示は増加数」の場合に指定する。
    """
    if df.empty:
        print(f"{title}: (該当なし)")
        return
    rank_df = (df.sort_values(sort_col or metric_col, ascending=False)
               .head(top_n).reset_index(drop=True))

    rows_html = ""
    for idx, row in rank_df.iterrows():
        thumb = _first_valid(row, "thumbnailURL", "thumbnail_url")
        thumb_html = f'<img src="{thumb}" width="80">' if thumb else ""
        vtitle = row.get("video_title") or row.get("videoTitle_latest") or row["videoId"]
        link = (f'<a href="https://www.youtube.com/watch?v={row["videoId"]}" target="_blank" '
                f'style="text-decoration:none;color:#1f77b4;font-weight:500;">{str(vtitle)[:55]}</a>')
        metric = f'<span style="color:#d62728;font-weight:bold;">{int(row[metric_col]):+,}</span>' \
            if pd.notna(row[metric_col]) else "N/A"
        vtype = row.get("video_type", "")
        badge = ""
        if vtype:
            color = "#ff6b6b" if vtype == "Shorts" else "#4ecdc4" if vtype == "Long-form" else "#999"
            badge = (f'<span style="background-color:{color};color:white;padding:4px 8px;'
                     f'border-radius:3px;font-size:11px;font-weight:bold;">{vtype}</span>')
        pub = row.get("publishedAt")
        pub_str = pd.Timestamp(pub).strftime("%Y-%m-%d") if pd.notna(pub) else ""
        bg = "#ffffff" if idx % 2 == 0 else "#f9f9f9"
        rows_html += f"""
            <tr style="background-color:{bg};border-bottom:1px solid #ddd;">
                <td style="padding:10px;text-align:center;font-weight:bold;">{idx + 1}</td>
                <td style="padding:10px;text-align:center;">{thumb_html}</td>
                <td style="padding:10px;max-width:300px;word-wrap:break-word;">{link}</td>
                <td style="padding:10px;text-align:right;">{metric}</td>
                <td style="padding:10px;text-align:center;">{badge}</td>
                <td style="padding:10px;text-align:center;">{pub_str}</td>
            </tr>"""

    html = f"""
    <div style="margin:20px 0;">
      <h3 style="color:#333;border-bottom:3px solid #1f77b4;padding-bottom:10px;">{title}</h3>
      <table style="border-collapse:collapse;width:100%;font-size:13px;">
        <thead>
          <tr style="background-color:#f0f0f0;border-bottom:2px solid #1f77b4;">
            <th style="padding:10px;width:5%;">順位</th>
            <th style="padding:10px;width:10%;">サムネイル</th>
            <th style="padding:10px;text-align:left;">動画タイトル</th>
            <th style="padding:10px;width:12%;color:#d62728;">増加数</th>
            <th style="padding:10px;width:8%;">タイプ</th>
            <th style="padding:10px;width:12%;">公開日</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>"""
    display(HTML(html))


def display_recent_gain_ranking(recent_df: pd.DataFrame, titles_df: pd.DataFrame | None = None,
                                top_n: int = 10) -> None:
    """直近上昇率ランキングのカード表示（旧06）。titles_df: videoId,video_title"""
    max_date = recent_df["view_date"].max()
    latest = recent_df[recent_df["view_date"] == max_date].copy()

    if titles_df is not None and "video_title" in titles_df.columns:
        latest = latest.merge(titles_df[["videoId", "video_title"]], on="videoId", how="left")
        latest["video_title"] = latest["video_title"].fillna(latest["videoId"])
    else:
        latest["video_title"] = latest["videoId"]

    ranked = latest.sort_values("diff_delta", ascending=False).head(top_n)

    def arrow(val):
        if pd.isna(val):
            return "—", "#999"
        if val > 0:
            return f"▲ +{val:.1f}%", "#2ecc71"
        if val < 0:
            return f"▼ {val:.1f}%", "#e74c3c"
        return f"→ {val:.1f}%", "#888"

    cards = ""
    for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
        symbol, color = arrow(row["diff_delta_pct"])
        diff_str = f"{row['viewCountDiff']:,.0f}" if pd.notna(row["viewCountDiff"]) else "—"
        delta_str = f"{row['diff_delta']:+,.0f}" if pd.notna(row["diff_delta"]) else "—"
        cards += f"""
        <div style='border:1px solid #ddd;padding:12px;border-radius:8px;
                    text-align:center;width:180px;position:relative'>
            <div style='position:absolute;top:6px;left:8px;font-size:1.1em;
                        font-weight:bold;color:#aaa'>#{rank}</div>
            <p style='font-size:.8em;color:#555;margin:16px 0 2px'>{row['video_title']}</p>
            <p style='font-size:.7em;color:#aaa;margin:0 0 6px'>{row['videoId']}</p>
            <img src='{row['thumbnail_url']}' width='150' height='84'
                 style='border-radius:4px;margin-bottom:8px'>
            <p style='margin:2px 0'><b>当日増加:</b> {diff_str}</p>
            <p style='margin:2px 0'><b>前日比差分:</b> {delta_str}</p>
            <p style='margin:4px 0;font-size:1.2em;font-weight:bold;color:{color}'>{symbol}</p>
        </div>"""

    date_str = max_date.strftime("%Y-%m-%d")
    display(HTML(
        f"<h3>📈 直近上昇率ランキング（{date_str} 時点） Top {top_n}</h3>"
        f"<div style='display:flex;flex-wrap:wrap;gap:12px'>{cards}</div>"
    ))


def display_thumbnail_grid(df: pd.DataFrame, title: str, cols: int = 3,
                           max_items: int = 9) -> None:
    """サムネイルのみのグリッド表示（旧14）。"""
    html = f"<h3>{title}</h3>"
    html += (f'<div style="display:grid;grid-template-columns:repeat({cols},1fr);'
             f'gap:10px;max-width:{cols * 110}px;">')
    for _, row in df.head(max_items).iterrows():
        thumb = row.get("thumbnailURL") or row.get("thumbnail_url") or ""
        url = row.get("videoURL") or f"https://www.youtube.com/watch?v={row['videoId']}"
        html += f"""
        <div style="text-align:center;">
          <a href="{url}" target="_blank">
            <img src="{thumb}" style="width:100%;height:auto;border:1px solid #ccc;
                 box-shadow:0 2px 5px rgba(0,0,0,0.2);">
          </a>
        </div>"""
    html += "</div>"
    display(HTML(html))
