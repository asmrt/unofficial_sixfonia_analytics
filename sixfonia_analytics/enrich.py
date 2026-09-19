"""YouTube APIによるメタデータ付与（タイトル・投稿日・動画長）とサムネURL。"""

from __future__ import annotations

import time

import pandas as pd


def thumbnail_url(video_id: str, quality: str = "hqdefault") -> str | None:
    """quality: default / mqdefault / hqdefault / sddefault / maxresdefault"""
    if not isinstance(video_id, str) or not video_id:
        return None
    return f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"


def fetch_video_details(youtube, video_ids: list[str]) -> dict[str, dict]:
    """videoId -> {title, published_at, duration_iso} を50件バッチで取得。"""
    from .collect import parse_duration_seconds

    details: dict[str, dict] = {}
    ids = [v for v in dict.fromkeys(video_ids) if isinstance(v, str) and v]
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        try:
            res = youtube.videos().list(
                part="snippet,contentDetails", id=",".join(batch)
            ).execute()
            for item in res.get("items", []):
                details[item["id"]] = {
                    "title": item["snippet"]["title"],
                    "published_at": item["snippet"]["publishedAt"],
                    "duration_seconds": parse_duration_seconds(
                        item["contentDetails"].get("duration")
                    ),
                }
        except Exception as e:
            print(f"[WARN] 詳細取得失敗 (batch {i}): {e}")
        time.sleep(0.05)
    return details


def fetch_video_titles(youtube, video_ids: list[str]) -> dict[str, str]:
    return {vid: d["title"] for vid, d in fetch_video_details(youtube, video_ids).items()}


def add_video_details(df: pd.DataFrame, youtube, id_col: str = "videoId") -> pd.DataFrame:
    """DataFrame に video_title / publishedAt / duration_seconds / video_type を付与。"""
    from .metrics import classify_video_type

    details = fetch_video_details(youtube, df[id_col].dropna().unique().tolist())
    df = df.copy()
    df["video_title"] = df[id_col].map(lambda v: details.get(v, {}).get("title")).fillna(df[id_col])
    df["publishedAt"] = pd.to_datetime(
        df[id_col].map(lambda v: details.get(v, {}).get("published_at")), errors="coerce"
    )
    df["duration_seconds"] = df[id_col].map(lambda v: details.get(v, {}).get("duration_seconds"))
    df["video_type"] = df["duration_seconds"].apply(classify_video_type)
    return df
