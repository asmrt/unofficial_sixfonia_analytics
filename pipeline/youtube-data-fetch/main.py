# -*- coding: utf-8 -*-
"""YouTube 動画統計情報取得（daily stats）"""

import functions_framework
from googleapiclient.discovery import build
from google.cloud import storage
import time
import csv
import datetime
import os
import json
import logging
import sys
import urllib.request
from io import StringIO


class CloudLoggingFormatter(logging.Formatter):
    """Cloud Logging が severity を認識できる構造化JSONで出力する（ログベースアラート用）"""

    def format(self, record):
        entry = {"severity": record.levelname, "message": record.getMessage()}
        if record.exc_info:
            entry["message"] += "\n" + self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(CloudLoggingFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)

API_KEY = os.getenv("YOUTUBE_API_KEY")
GCS_BUCKET = os.getenv("GCS_BUCKET", "youtube-metrics-bucket")
# 動画マスタ（タイトル・投稿日・Shorts 判定など）の公開URL。生成は別システムが行い、
# ここでは取得して GCS にコピーするだけ（YouTube API で二重に取得しない）。URL はデプロイ時に渡す
SNAPSHOT_URL = os.getenv("SNAPSHOT_URL")
SNAPSHOT_BLOB = "master/videos.json"
SNAPSHOT_NDJSON_BLOB = "master/videos.ndjson"

if not API_KEY:
    raise RuntimeError("YOUTUBE_API_KEY is not set")
if not SNAPSHOT_URL:
    raise RuntimeError("SNAPSHOT_URL is not set")

youtube = build("youtube", "v3", developerKey=API_KEY)

JST = datetime.timezone(datetime.timedelta(hours=9))


def load_channels():
    """channels.json から読み込み"""
    config_path = os.path.join(os.path.dirname(__file__), "channels.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return [(ch["id"], ch["name"]) for ch in config["channels"]]


def get_channel_uploads_playlist_id(channel_id):
    """プレイリストID取得"""
    req = youtube.channels().list(part="contentDetails", id=channel_id)
    res = req.execute()
    items = res.get("items")
    if not items:
        raise ValueError(f"No channel found for ID: {channel_id}")
    uploads = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads:
        raise ValueError(f"No uploads playlist found for channel ID: {channel_id}")
    return uploads


def get_videos_from_playlist(playlist_id, max_results=50):
    """プレイリストから動画ID一覧取得"""
    video_ids = []
    next_page = None
    while True:
        req = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=max_results,
            pageToken=next_page,
        )
        res = req.execute()
        video_ids.extend([
            item["contentDetails"]["videoId"] for item in res.get("items", [])
        ])
        next_page = res.get("nextPageToken")
        if not next_page:
            break
        time.sleep(0.1)
    return video_ids


def get_video_statistics(video_ids):
    """動画統計情報を取得（サムネイルは動画マスタ側で持つのでここでは取得しない）"""
    stats = []
    for i in range(0, len(video_ids), 50):
        req = youtube.videos().list(
            part="statistics",
            id=",".join(video_ids[i:i + 50]),
        )
        res = req.execute()
        for item in res.get("items", []):
            s = item.get("statistics", {})
            stats.append({
                "videoId": item["id"],
                "viewCount": s.get("viewCount", "0"),
                "likeCount": s.get("likeCount", "0"),
                "commentCount": s.get("commentCount", "0"),
                "videoURL": f"https://www.youtube.com/watch?v={item['id']}",
            })
        time.sleep(0.1)
    return stats


def save_to_gcs(channel_name, filename, data):
    """GCS に CSV で保存"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)

    buf = StringIO()
    fields = ["videoId", "viewCount", "likeCount", "commentCount", "videoURL", "view_date", "channel"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(data)

    blob = bucket.blob(f"{channel_name}/{filename}")
    blob.upload_from_string(buf.getvalue(), content_type="text/csv")
    logger.info(f"Saved to GCS: {channel_name}/{filename}")


def copy_snapshot_to_gcs():
    """動画マスタ（videos.json）を公開URLから取得し、GCS に上書き保存する。

    videos.json をそのまま保存するのに加えて、BigQuery の外部テーブル（NEWLINE_DELIMITED_JSON）が
    直接読めるよう、videos 配列を1行1動画の NDJSON に変換した videos.ndjson も保存する
    （キーの変換はしない。スネークケースへのリネームは BigQuery 側のビューで行う）。

    戻り値は動画マスタの updated_at。中身が動画マスタとして不正なら何も保存せずに例外を投げる
    （エラーページなどで GCS 上の正しいファイルを上書きしないため）。
    """
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=60) as res:
        body = res.read()
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list) or not data["videos"]:
        raise ValueError("snapshot has no videos")

    ndjson = "\n".join(json.dumps(video, ensure_ascii=False) for video in data["videos"]) + "\n"

    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    bucket.blob(SNAPSHOT_BLOB).upload_from_string(body, content_type="application/json")
    bucket.blob(SNAPSHOT_NDJSON_BLOB).upload_from_string(ndjson, content_type="application/x-ndjson")
    logger.info(f"Saved to GCS: {SNAPSHOT_BLOB}, {SNAPSHOT_NDJSON_BLOB} (updated_at={data.get('updated_at')})")
    return data.get("updated_at")


def fetch_channel_data(channel_id, channel_name, view_date):
    """チャンネルデータを取得してGCSに保存する（成功時 True、失敗時 False）"""
    try:
        logger.info(f"Processing {channel_name}...")
        playlist_id = get_channel_uploads_playlist_id(channel_id)
        video_ids = get_videos_from_playlist(playlist_id)
        logger.info(f"  Found {len(video_ids)} videos")

        video_stats = get_video_statistics(video_ids)
        for st in video_stats:
            st["view_date"] = view_date
            st["channel"] = channel_name

        filename = f"{channel_name}_video_statistics_{view_date}.csv"
        save_to_gcs(channel_name, filename, video_stats)
        return True
    except Exception as e:
        logger.error(f"Error processing {channel_name}: {str(e)}")
        return False


@functions_framework.http
def main(request):
    """Cloud Function エントリーポイント"""
    view_date = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y%m%d")
    start = datetime.datetime.now()
    logger.info(f"[START] Daily stats fetch started for {view_date}")

    try:
        channels = load_channels()
        if not channels:
            return {"error": "No channels loaded"}, 400

        results = {}
        for cid, cname in channels:
            results[cname] = fetch_channel_data(cid, cname, view_date)

        # 動画マスタのコピーは統計とは独立。失敗しても統計の取得はやり直さない
        # （ステータスは統計の結果だけで決め、失敗は ERROR ログ → アラートで気づく）
        try:
            snapshot_updated_at = copy_snapshot_to_gcs()
        except Exception as e:
            logger.error(f"Error copying snapshot: {str(e)}")
            snapshot_updated_at = None

        ok = sum(1 for v in results.values() if v)
        dur = (datetime.datetime.now() - start).total_seconds()
        logger.info(f"[END] {ok}/{len(channels)} channels ({dur:.2f}s)")

        status = 200 if ok == len(channels) else 500
        return {
            "message": f"Stats: {ok}/{len(channels)} completed",
            "results": results,
            "snapshot_updated_at": snapshot_updated_at,
            "duration_seconds": dur,
        }, status
    except Exception as e:
        logger.error(f"[ERROR] {str(e)}", exc_info=True)
        return {"error": str(e)}, 500
