"""再生リスト（プレイリスト）の取得と、それを使ったジャンル分類。

日次統計CSVにも動画マスタにもプレイリスト所属の情報がないため、
YouTube Data API v3 の playlists.list / playlistItems.list から
videoId → 所属プレイリスト の対応表を作る。

取得できるのは公開プレイリストのみ（APIキー方式のため）。
非公開・限定公開のプレイリストは対象外になる。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pandas as pd

from . import config

UNCLASSIFIED = "未分類"

# ============================================================
# ジャンル分類ルール
# ------------------------------------------------------------
# 上から順に評価し、最初にマッチしたジャンルを採用する（順序が優先度）。
# 判定対象は「その動画が入っているプレイリスト名を連結した文字列」。
# プレイリストに1つも入っていない動画は動画タイトルでフォールバック判定する。
# 実際のプレイリスト名を見て随時調整すること。
# ============================================================
GENRE_RULES: list[tuple[str, list[str]]] = [
    ("オリジナル曲/MV", [r"オリジナル", r"\bMV\b", r"music\s*video", r"楽曲", r"リリース"]),
    ("歌ってみた", [r"歌って", r"歌みた", r"歌ってみた", r"cover", r"カバー", r"アカペラ"]),
    ("踊ってみた", [r"踊って", r"ダンス", r"dance"]),
    ("ライブ/イベント", [r"ライブ", r"\blive\b", r"ツアー", r"イベント", r"リリイベ", r"ワンマン"]),
    ("ゲーム実況", [r"ゲーム", r"実況", r"マイクラ", r"minecraft", r"apex", r"マリオ", r"人狼"]),
    ("企画/検証", [r"企画", r"検証", r"チャレンジ", r"やってみた", r"ドッキリ", r"大会", r"対決"]),
    ("ラジオ/雑談", [r"ラジオ", r"雑談", r"トーク", r"\btalk\b", r"お便り", r"質問"]),
    ("ボイス/ASMR", [r"asmr", r"ボイス", r"囁", r"耳かき", r"寝落ち"]),
    ("Shorts/切り抜き", [r"shorts?", r"ショート", r"切り抜き", r"まとめ"]),
    ("メンバー個別", [r"暇72", r"雨乃こさめ", r"こさめ", r"いるま", r"みこと", r"すち", r"\bLAN\b"]),
]


# ============================================================
# 取得
# ============================================================
def fetch_channel_playlists(youtube, channel_id: str) -> list[dict]:
    """チャンネルの公開プレイリスト一覧を返す。"""
    rows: list[dict] = []
    page_token = None
    while True:
        res = youtube.playlists().list(
            part="snippet,contentDetails",
            channelId=channel_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in res.get("items", []):
            rows.append({
                "playlist_id": item["id"],
                "playlist_title": item["snippet"]["title"],
                "playlist_description": item["snippet"].get("description", ""),
                "item_count": item["contentDetails"]["itemCount"],
            })
        page_token = res.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.1)  # API制限対策
    return rows


def fetch_playlist_memberships(youtube, playlists: list[dict]) -> pd.DataFrame:
    """全プレイリストの収録動画を展開する。

    1本の動画が複数プレイリストに入っている場合は複数行になる。
    戻り値: video_id, playlist_id, playlist_title, position
    """
    from .collect import iter_playlist_items  # 循環import回避のため遅延

    rows: list[dict] = []
    for pl in playlists:
        try:
            items = list(iter_playlist_items(youtube, pl["playlist_id"], part="snippet,contentDetails"))
        except Exception as e:
            print(f"[WARN] {pl['playlist_title']}: 取得失敗 — {e}")
            continue
        for it in items:
            rows.append({
                "video_id": it["contentDetails"]["videoId"],
                "playlist_id": pl["playlist_id"],
                "playlist_title": pl["playlist_title"],
                "position": it["snippet"].get("position"),
            })
        print(f"  {pl['playlist_title']}: {len(items)} 本")
    return pd.DataFrame(rows, columns=["video_id", "playlist_id", "playlist_title", "position"])


# ============================================================
# 保存 / ロード（APIクォータ節約のためキャッシュする）
# ============================================================
def playlist_map_path(channel_name: str) -> Path:
    return config.channel_dir(channel_name) / f"{channel_name}_playlist_map.csv"


def build_playlist_map(youtube, channel_name: str, refresh: bool = False) -> pd.DataFrame:
    """videoId→プレイリストの対応表を作る。保存済みがあれば再利用する。

    refresh=True で強制的に再取得する。
    """
    path = playlist_map_path(channel_name)
    if path.exists() and not refresh:
        df = pd.read_csv(path)
        print(f"保存済みを使用: {path}  ({len(df)} 行 / "
              f"{df['playlist_title'].nunique()} プレイリスト)")
        return df

    channel_id = config.channel_id_of(channel_name)
    playlists = fetch_channel_playlists(youtube, channel_id)
    print(f"公開プレイリスト: {len(playlists)} 件")
    df = fetch_playlist_memberships(youtube, playlists)

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    print(f"保存: {path}  ({len(df)} 行)")
    return df


# ============================================================
# ジャンル分類
# ============================================================
def classify_genre(playlist_titles, fallback_text: str | None = None,
                   rules: list[tuple[str, list[str]]] | None = None) -> str:
    """プレイリスト名（複数可）からジャンル名を返す。

    playlist_titles: プレイリスト名のリスト。空ならタイトルでフォールバック判定。
    fallback_text  : プレイリスト未所属の動画に使う文字列（通常は動画タイトル）。
    """
    rules = rules or GENRE_RULES
    titles = [t for t in (playlist_titles or []) if isinstance(t, str) and t]
    haystack = " / ".join(titles) if titles else (fallback_text or "")
    if not haystack:
        return UNCLASSIFIED
    for genre, patterns in rules:
        if any(re.search(p, haystack, flags=re.IGNORECASE) for p in patterns):
            return genre
    return UNCLASSIFIED


def add_genre(df: pd.DataFrame, playlist_map: pd.DataFrame, id_col: str = "videoId",
              title_col: str = "video_title",
              rules: list[tuple[str, list[str]]] | None = None) -> pd.DataFrame:
    """DataFrame に playlists（所属プレイリスト名）と genre 列を付与する。

    df           : videoId 列を持つ任意の DataFrame
    playlist_map : build_playlist_map の戻り値（video_id, playlist_title）
    """
    grouped = (
        playlist_map.dropna(subset=["playlist_title"])
        .groupby("video_id")["playlist_title"]
        .apply(lambda s: sorted(set(s)))
    )
    out = df.copy()
    out["playlists"] = out[id_col].map(grouped)
    out["playlists"] = out["playlists"].apply(lambda v: v if isinstance(v, list) else [])
    out["playlists_str"] = out["playlists"].apply(lambda v: " / ".join(v))
    fallback = out[title_col] if title_col in out.columns else pd.Series("", index=out.index)
    out["genre"] = [
        classify_genre(pls, fallback_text=fb, rules=rules)
        for pls, fb in zip(out["playlists"], fallback)
    ]
    return out


def genre_summary(df: pd.DataFrame, metric_col: str = "views_gained") -> pd.DataFrame:
    """ジャンル別の本数・合計・平均と、シェア[%]を返す。"""
    summary = (
        df.groupby("genre")
        .agg(video_count=(metric_col, "size"),
             total=(metric_col, "sum"),
             mean=(metric_col, "mean"),
             max=(metric_col, "max"))
        .reset_index()
        .sort_values("total", ascending=False)
    )
    summary["share_pct"] = (summary["total"] / summary["total"].sum() * 100).round(1)
    summary["mean"] = summary["mean"].round(0)
    return summary.reset_index(drop=True)


def unclassified_report(df: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    """未分類の動画を確認してルール追加の材料にする。"""
    sub = df[df["genre"] == UNCLASSIFIED]
    cols = [c for c in ["videoId", "video_title", "playlists_str", "views_gained"]
            if c in sub.columns]
    print(f"未分類: {len(sub)} 件 / 全 {len(df)} 件")
    return sub[cols].head(top_n)
