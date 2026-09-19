# -*- coding: utf-8 -*-
"""YouTube 動画統計情報取得（daily stats）"""

import functions_framework
from googleapiclient.discovery import build
from google.cloud import bigquery, storage
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
# 読み込み先の BigQuery テーブル（project.dataset.table）。プロジェクトIDはリポジトリに書かず、デプロイ時に渡す
BQ_TABLE = os.getenv("BQ_TABLE")
# 動画マスタ（タイトル・投稿日・Shorts 判定など）の公開URL。生成は別システムが行い、
# ここでは取得して GCS にコピーするだけ（YouTube API で二重に取得しない）。URL はデプロイ時に渡す
SNAPSHOT_URL = os.getenv("SNAPSHOT_URL")
SNAPSHOT_BLOB = "master/videos.json"

if not API_KEY:
    raise RuntimeError("YOUTUBE_API_KEY is not set")
if not BQ_TABLE:
    raise RuntimeError("BQ_TABLE is not set")
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
    """動画統計情報を取得"""
    stats = []
    for i in range(0, len(video_ids), 50):
        req = youtube.videos().list(
            part="statistics,snippet",
            id=",".join(video_ids[i:i + 50]),
        )
        res = req.execute()
        for item in res.get("items", []):
            s = item.get("statistics", {})
            sn = item.get("snippet", {})
            stats.append({
                "videoId": item["id"],
                "viewCount": s.get("viewCount", "0"),
                "likeCount": s.get("likeCount", "0"),
                "commentCount": s.get("commentCount", "0"),
                "videoURL": f"https://www.youtube.com/watch?v={item['id']}",
                "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url", ""),
            })
        time.sleep(0.1)
    return stats


def save_to_gcs(channel_name, filename, data):
    """GCS に CSV で保存"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)

    buf = StringIO()
    fields = ["videoId", "viewCount", "likeCount", "commentCount", "videoURL", "thumbnail", "view_date"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(data)

    blob = bucket.blob(f"{channel_name}/{filename}")
    blob.upload_from_string(buf.getvalue(), content_type="text/csv")
    logger.info(f"Saved to GCS: {channel_name}/{filename}")


def copy_snapshot_to_gcs():
    """動画マスタ（videos.json）を公開URLから取得し、GCS に上書き保存する。

    戻り値は動画マスタの updated_at。中身が動画マスタとして不正なら保存せずに例外を投げる
    （エラーページなどで GCS 上の正しいファイルを上書きしないため）。
    """
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=60) as res:
        body = res.read()
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list) or not data["videos"]:
        raise ValueError("snapshot has no videos")

    client = storage.Client()
    blob = client.bucket(GCS_BUCKET).blob(SNAPSHOT_BLOB)
    blob.upload_from_string(body, content_type="application/json")
    logger.info(f"Saved to GCS: {SNAPSHOT_BLOB} (updated_at={data.get('updated_at')})")
    return data.get("updated_at")


def fetch_channel_data(channel_id, channel_name, view_date):
    """チャンネルデータを取得してGCSに保存し、取得した行を返す（失敗時は None）"""
    try:
        logger.info(f"Processing {channel_name}...")
        playlist_id = get_channel_uploads_playlist_id(channel_id)
        video_ids = get_videos_from_playlist(playlist_id)
        logger.info(f"  Found {len(video_ids)} videos")

        video_stats = get_video_statistics(video_ids)
        for st in video_stats:
            st["view_date"] = view_date

        filename = f"{channel_name}_video_statistics_{view_date}.csv"
        save_to_gcs(channel_name, filename, video_stats)
        return video_stats
    except Exception as e:
        logger.error(f"Error processing {channel_name}: {str(e)}")
        return None


def to_bq_rows(channel_name, video_stats):
    """CSV 用の行（値は文字列）を BigQuery 用の行に変換する（channel を付け、数値と日付を型付け）"""
    return [
        {
            "channel": channel_name,
            "videoId": st["videoId"],
            "viewCount": int(st["viewCount"]),
            "likeCount": int(st["likeCount"]),
            "commentCount": int(st["commentCount"]),
            "videoURL": st["videoURL"],
            "thumbnail": st["thumbnail"],
            "view_date": datetime.datetime.strptime(st["view_date"], "%Y%m%d").date().isoformat(),
        }
        for st in video_stats
    ]


def load_to_bigquery(rows_by_channel, view_date):
    """取得できたチャンネルの view_date 分を BigQuery で置き換える。

    先に「その日・そのチャンネル」の行を消してから追加するので、リトライしても重複しない。
    失敗したチャンネルの行は触らない（前の試行で入った分が残る）。
    """
    client = bigquery.Client()
    day = datetime.datetime.strptime(view_date, "%Y%m%d").date()
    channels = list(rows_by_channel)

    client.query(
        f"DELETE FROM `{BQ_TABLE}` WHERE view_date = @day AND channel IN UNNEST(@channels)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("day", "DATE", day),
            bigquery.ArrayQueryParameter("channels", "STRING", channels),
        ]),
    ).result()

    rows = [r for ch in channels for r in to_bq_rows(ch, rows_by_channel[ch])]
    client.load_table_from_json(
        rows, BQ_TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_APPEND),
    ).result()
    logger.info(f"Loaded to BigQuery: {len(rows)} rows ({view_date}, {len(channels)} channels)")
    return len(rows)


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

        fetched = {}
        for cid, cname in channels:
            rows = fetch_channel_data(cid, cname, view_date)
            if rows is not None:
                fetched[cname] = rows
        results = {cname: cname in fetched for _, cname in channels}

        # 取得できたチャンネルだけ読み込む。失敗したら 500 を返し、Scheduler のリトライでやり直す
        bq_rows = 0
        bq_ok = True
        if fetched:
            try:
                bq_rows = load_to_bigquery(fetched, view_date)
            except Exception as e:
                logger.error(f"Error loading to BigQuery: {str(e)}")
                bq_ok = False

        # 動画マスタのコピーは統計とは独立。失敗しても統計の取得はやり直さない
        # （ステータスは統計の結果だけで決め、失敗は ERROR ログ → アラートで気づく）
        try:
            snapshot_updated_at = copy_snapshot_to_gcs()
        except Exception as e:
            logger.error(f"Error copying snapshot: {str(e)}")
            snapshot_updated_at = None

        ok = len(fetched)
        dur = (datetime.datetime.now() - start).total_seconds()
        logger.info(f"[END] {ok}/{len(channels)} channels, {bq_rows} rows to BigQuery ({dur:.2f}s)")

        status = 200 if ok == len(channels) and bq_ok else 500
        return {
            "message": f"Stats: {ok}/{len(channels)} completed",
            "results": results,
            "bq_rows": bq_rows,
            "snapshot_updated_at": snapshot_updated_at,
            "duration_seconds": dur,
        }, status
    except Exception as e:
        logger.error(f"[ERROR] {str(e)}", exc_info=True)
        return {"error": str(e)}, 500
