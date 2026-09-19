# 旧ガイド: YouTube クイズ選定システム - 完全実装ガイド

> **これは参照用の旧資料です。このとおりに構築しないでください。** 構築は [SETUP.md](SETUP.md) に従います。
>
> 本パイプライン（`pipeline/`）の元になった初期の実装ガイドを、原文のまま保存しています。
> [IMPROVEMENTS.md](IMPROVEMENTS.md) の #6（可視化・通知）と #7（新着・削除の差分記録）が、ここにあるコードを参照しています。
> 変更点は、プロジェクトIDを `YOUR_PROJECT_ID` に置き換えたことと、原文全体をコードブロックで囲んだことだけです。
>
> 現行の実装とは次の点が異なり、そのまま使うと問題があります。
>
> - 関数を `--allow-unauthenticated`（認証なしで誰でも実行可能）でデプロイしている → 現行は認証必須
> - API キーを環境変数に平文で渡している → 現行は Secret Manager
> - 関数から BigQuery に直接 INSERT し、SQL を文字列連結で組み立てている（SQL インジェクションの余地） → 現行は GCS 書き出しのみで、スケジュールクエリの MERGE で投入
> - `view_date` が STRING で、日付が実行日そのもの → 現行は DATE 型で、実行日の前日
> - Streamlit ダッシュボードも `view_date` を STRING 前提で扱っている

```text
YouTube クイズ選定システム - 完全実装ガイド

シクフォニファンの動画再生数データから、毎日クイズ選定候補を自動抽出・可視化するシステム

目次





プロジェクト概要



全体アーキテクチャ



環境構築



実装コード



デプロイ手順



運用・管理

概要

目的: 毎日 YouTube の再生数が多い動画を自動検知し、クイズ作成候補を効率化

対象: 7 シクフォニ関連チャンネル（合計 2000+ 動画）

結果:





毎日 1:30 に Slack で「今日のランキング」を通知



ブラウザで詳細分析（推移グラフ、期間分析）

アーキテクチャ

YouTube API
    ↓
┌─────────────────────────────────────────┐
│ Cloud Function 1: youtube-data-fetch    │
│ 毎日 1:00 実行（daily stats）             │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ Cloud Function 2:                       │
│ youtube-video-metadata-fetch            │
│ 毎日 1:15 実行（メタデータ）              │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ GCS (Google Cloud Storage)              │
│ gs://youtube-metrics-bucket/            │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ BigQuery                                │
│ YOUR_PROJECT_ID.youtube_stat  │
│ .all_video_statistics_summary           │
└─────────────────────────────────────────┘
    ↓
┌──────────────────┐  ┌──────────────────┐
│ Looker Studio    │  │ Streamlit        │
│ メール通知       │  │ 詳細分析         │
│ → Slack          │  │ → Cloud Run      │
└──────────────────┘  └──────────────────┘


環境構築

前提条件

# GCP プロジェクト ID
PROJECT_ID="YOUR_PROJECT_ID"

# gcloud CLI インストール & 認証
gcloud auth login
gcloud config set project $PROJECT_ID

# 必要な API を有効化
gcloud services enable cloudfunctions.googleapis.com
gcloud services enable cloudscheduler.googleapis.com
gcloud services enable storage-component.googleapis.com
gcloud services enable bigquery.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com


GCS バケット作成

gsutil mb gs://youtube-metrics-bucket


BigQuery データセット作成

bq mk --dataset \
  --location=asia-northeast1 \
  youtube_stat

# テーブル作成（スキーマ定義）
bq mk --table \
  youtube_stat.all_video_statistics_summary \
  channel:STRING,videoid:STRING,viewCount:INTEGER,likeCount:INTEGER,commentCount:INTEGER,videoURL:STRING,view_date:STRING,img:STRING,view_diff:INTEGER


実装コード

Part 1: Cloud Function - youtube-data-fetch

ファイル構成

youtube-data-fetch/
├── main.py
├── requirements.txt
└── channels.json


channels.json

{
  "channels": [
    {"id": "UCr24Ll7IT2hPquu-n11dNWQ", "name": "hima72"},
    {"id": "UCGMG8BNfA8gsH9Rn_d_yW2A", "name": "sixfonia"},
    {"id": "UC1FByWnYWrAWcCmijBURkfg", "name": "kosame"},
    {"id": "UCs7eh4DZC6HXiizJXLj3_FA", "name": "illuma"},
    {"id": "UCxP9lRliG6xyAbx8Dp3OR1w", "name": "mikoto"},
    {"id": "UCB8DR-ao7ZfoAwoYRXE3VJw", "name": "suchi"},
    {"id": "UCEqBb37k-pKRQPG-_MvtAeA", "name": "lan"}
  ]
}


requirements.txt

google-cloud-storage==2.10.0
google-api-python-client==2.100.0


main.py

# -*- coding: utf-8 -*-
"""YouTube 動画統計情報取得（daily stats）"""

from googleapiclient.discovery import build
from google.cloud import storage, bigquery
import time
import csv
import datetime
import os
import json
import logging
from io import StringIO

logger = logging.getLogger(__name__)

API_KEY = os.getenv("YOUTUBE_API_KEY")
GCS_BUCKET = os.getenv("GCS_BUCKET", "youtube-metrics-bucket")
PROJECT_ID = os.getenv("GCP_PROJECT", "YOUR_PROJECT_ID")

youtube = build("youtube", "v3", developerKey=API_KEY)
today_str = datetime.datetime.today().strftime("%Y%m%d")

def load_channels():
    """channels.json から読み込み"""
    try:
        with open("channels.json", "r", encoding="utf-8") as f:
            config = json.load(f)
            return [(ch["id"], ch["name"]) for ch in config["channels"]]
    except Exception as e:
        logger.error(f"Error loading channels.json: {str(e)}")
        return []

def get_channel_uploads_playlist_id(channel_id):
    """プレイリストID取得"""
    request = youtube.channels().list(
        part="contentDetails",
        id=channel_id
    )
    response = request.execute()
    return response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

def get_videos_from_playlist(playlist_id, max_results=50):
    """プレイリストから動画ID一覧取得"""
    video_ids = []
    next_page_token = None
    while True:
        request = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=max_results,
            pageToken=next_page_token
        )
        response = request.execute()
        video_ids.extend([item["contentDetails"]["videoId"] for item in response["items"]])
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break
        time.sleep(0.1)
    return video_ids

def get_video_statistics(video_ids):
    """動画統計情報を取得"""
    statistics = []
    for i in range(0, len(video_ids), 50):
        request = youtube.videos().list(
            part="statistics,snippet",
            id=",".join(video_ids[i:i + 50])
        )
        response = request.execute()
        for item in response["items"]:
            statistics.append({
                "videoId": item["id"],
                "viewCount": item["statistics"].get("viewCount", "0"),
                "likeCount": item["statistics"].get("likeCount", "0"),
                "commentCount": item["statistics"].get("commentCount", "0"),
                "videoURL": f"https://www.youtube.com/watch?v={item['id']}",
                "thumbnail": item.get("snippet", {}).get("thumbnails", {}).get("default", {}).get("url", "")
            })
        time.sleep(0.1)
    return statistics

def save_to_gcs(channel_name, filename, data):
    """GCS に CSV で保存"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    
    csv_buffer = StringIO()
    fieldnames = ["videoId", "viewCount", "likeCount", "commentCount", "videoURL", "thumbnail"]
    writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(data)
    
    blob_path = f"{channel_name}/{filename}"
    blob = bucket.blob(blob_path)
    blob.upload_from_string(csv_buffer.getvalue(), content_type="text/csv")
    logger.info(f"Saved to GCS: {blob_path}")

def save_to_bigquery(channel_name, video_stats):
    """BigQuery に直接挿入"""
    client = bigquery.Client(project=PROJECT_ID)
    table_id = f"{PROJECT_ID}.youtube_stat.all_video_statistics_summary"
    
    # 前日データを取得（前日比計算用）
    query = f"""
    SELECT videoId, viewCount
    FROM `{table_id}`
    WHERE channel = '{channel_name}' 
      AND view_date = DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 1 DAY)
    """
    
    try:
        previous_views = {}
        result = client.query(query)
        for row in result:
            previous_views[row['videoId']] = int(row['viewCount'])
    except:
        previous_views = {}
    
    # 行を準備
    rows_to_insert = []
    for stat in video_stats:
        prev_views = previous_views.get(stat['videoId'], 0)
        view_diff = int(stat['viewCount']) - prev_views
        
        rows_to_insert.append({
            "channel": channel_name,
            "videoid": stat['videoId'],
            "viewCount": int(stat['viewCount']),
            "likeCount": int(stat['likeCount']),
            "commentCount": int(stat['commentCount']),
            "videoURL": stat['videoURL'],
            "view_date": today_str,
            "img": stat['thumbnail'],
            "view_diff": view_diff
        })
    
    errors = client.insert_rows_json(table_id, rows_to_insert, skip_invalid_rows=True)
    if errors:
        logger.error(f"BigQuery insert errors: {errors}")
    else:
        logger.info(f"Inserted {len(rows_to_insert)} rows to BigQuery")

def fetch_channel_data(channel_id, channel_name):
    """チャンネルデータを取得"""
    try:
        logger.info(f"Processing {channel_name}...")
        
        playlist_id = get_channel_uploads_playlist_id(channel_id)
        video_ids = get_videos_from_playlist(playlist_id)
        logger.info(f"  Found {len(video_ids)} videos")
        
        video_stats = get_video_statistics(video_ids)
        
        # GCS に保存
        filename = f"{channel_name}_video_statistics_{today_str}.csv"
        save_to_gcs(channel_name, filename, video_stats)
        
        # BigQuery に保存
        save_to_bigquery(channel_name, video_stats)
        
        return True
    except Exception as e:
        logger.error(f"Error processing {channel_name}: {str(e)}")
        return False

def main(request):
    """Cloud Function エントリーポイント"""
    
    start_time = datetime.datetime.now()
    logger.info(f"[START] Daily stats fetch started at {today_str}")
    
    try:
        channels = load_channels()
        if not channels:
            return {"error": "No channels loaded"}, 400
        
        results = {}
        for channel_id, channel_name in channels:
            success = fetch_channel_data(channel_id, channel_name)
            results[channel_name] = success
        
        success_count = sum(1 for v in results.values() if v)
        duration = (datetime.datetime.now() - start_time).total_seconds()
        
        logger.info(f"[END] Completed: {success_count}/{len(channels)} channels ({duration:.2f}s)")
        
        return {
            "message": f"Stats: {success_count}/{len(channels)} completed",
            "results": results,
            "duration_seconds": duration
        }, 200
    
    except Exception as e:
        logger.error(f"[ERROR] {str(e)}", exc_info=True)
        return {"error": str(e)}, 500


Part 2: Cloud Function - youtube-video-metadata-fetch

ファイル構成

youtube-video-metadata-fetch/
├── main.py
├── requirements.txt
└── channels.json


requirements.txt

google-cloud-storage==2.10.0
google-api-python-client==2.100.0


main.py

# -*- coding: utf-8 -*-
"""YouTube 動画メタデータ取得"""

from googleapiclient.discovery import build
from google.cloud import storage
import time
import json
import datetime
import os
import logging
import re

logger = logging.getLogger(__name__)

API_KEY = os.getenv("YOUTUBE_API_KEY")
GCS_BUCKET = os.getenv("GCS_BUCKET", "youtube-metrics-bucket")

youtube = build("youtube", "v3", developerKey=API_KEY)
today_str = datetime.datetime.today().strftime("%Y%m%d")

def load_channels():
    """channels.json から読み込み"""
    try:
        with open("channels.json", "r", encoding="utf-8") as f:
            config = json.load(f)
            return [(ch["id"], ch["name"]) for ch in config["channels"]]
    except Exception as e:
        logger.error(f"Error loading channels.json: {str(e)}")
        return []

def get_channel_uploads_playlist_id(channel_id):
    """プレイリストID取得"""
    request = youtube.channels().list(
        part="contentDetails",
        id=channel_id
    )
    response = request.execute()
    return response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

def get_all_video_ids(playlist_id, max_results=50):
    """全動画ID取得"""
    video_ids = []
    next_page_token = None
    
    while True:
        request = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=max_results,
            pageToken=next_page_token
        )
        response = request.execute()
        video_ids.extend([
            item["contentDetails"]["videoId"] 
            for item in response["items"]
        ])
        
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break
        time.sleep(0.1)
    
    return video_ids

def parse_duration(duration_str):
    """ISO 8601 duration を秒に変換"""
    pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
    match = re.match(pattern, duration_str)
    
    if not match:
        return 0
    
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    
    return hours * 3600 + minutes * 60 + seconds

def get_video_metadata(video_ids):
    """メタデータ取得"""
    metadata = []
    
    for i in range(0, len(video_ids), 50):
        request = youtube.videos().list(
            part="snippet,contentDetails",
            id=",".join(video_ids[i:i + 50])
        )
        response = request.execute()
        
        for item in response["items"]:
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails", {})
            
            duration_str = content_details.get("duration", "PT0S")
            duration_seconds = parse_duration(duration_str)
            is_short = duration_seconds <= 60
            
            metadata.append({
                "videoId": item["id"],
                "title": snippet.get("title", "N/A"),
                "publishedAt": snippet.get("publishedAt", "N/A"),
                "duration": duration_str,
                "duration_seconds": duration_seconds,
                "is_short": is_short,
                "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url", "")
            })
        
        time.sleep(0.1)
    
    return metadata

def get_previous_metadata(channel_name):
    """前回のメタデータを取得"""
    try:
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(f"metadata/{channel_name}_video_metadata.json")
        
        if blob.exists():
            json_str = blob.download_as_string().decode("utf-8")
            data = json.loads(json_str)
            return {v["videoId"]: v for v in data.get("videos", [])}
        return {}
    except Exception as e:
        logger.warning(f"Could not load previous metadata: {str(e)}")
        return {}

def save_metadata_to_gcs(channel_name, metadata):
    """マスターメタデータを保存"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    
    data = {
        "channel_name": channel_name,
        "last_updated": datetime.datetime.utcnow().isoformat() + "Z",
        "videos": metadata
    }
    
    master_blob = bucket.blob(f"metadata/{channel_name}_video_metadata.json")
    master_blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    logger.info(f"Saved metadata master for {channel_name}")

def save_diff_to_gcs(channel_name, current_metadata, previous_metadata):
    """差分を保存"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    
    current_ids = {v["videoId"]: v for v in current_metadata}
    previous_ids = previous_metadata
    
    new_videos = [
        v for vid, v in current_ids.items()
        if vid not in previous_ids
    ]
    
    deleted_videos = [
        vid for vid in previous_ids
        if vid not in current_ids
    ]
    
    diff_data = {
        "channel_name": channel_name,
        "fetch_date": today_str,
        "new_videos": new_videos,
        "deleted_videos": deleted_videos,
        "statistics": {
            "total_videos": len(current_metadata),
            "new_count": len(new_videos),
            "deleted_count": len(deleted_videos),
            "shorts_count": sum(1 for v in current_metadata if v["is_short"])
        }
    }
    
    diff_blob = bucket.blob(f"metadata/{channel_name}_new_videos_{today_str}.json")
    diff_blob.upload_from_string(
        json.dumps(diff_data, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    
    logger.info(f"Saved diff: +{len(new_videos)} -{len(deleted_videos)}")
    
    return len(new_videos), len(deleted_videos)

def fetch_channel_metadata(channel_id, channel_name):
    """チャンネルメタデータを取得"""
    try:
        logger.info(f"Processing {channel_name}...")
        
        playlist_id = get_channel_uploads_playlist_id(channel_id)
        video_ids = get_all_video_ids(playlist_id)
        logger.info(f"  Found {len(video_ids)} videos")
        
        metadata = get_video_metadata(video_ids)
        
        previous_metadata = get_previous_metadata(channel_name)
        
        save_metadata_to_gcs(channel_name, metadata)
        
        new_count, deleted_count = save_diff_to_gcs(
            channel_name, 
            metadata, 
            previous_metadata
        )
        
        return True, new_count, deleted_count
    
    except Exception as e:
        logger.error(f"Error processing {channel_name}: {str(e)}")
        return False, 0, 0

def main(request):
    """Cloud Function エントリーポイント"""
    
    start_time = datetime.datetime.now()
    logger.info(f"[START] Video metadata fetch started at {today_str}")
    
    try:
        channels = load_channels()
        if not channels:
            return {"error": "No channels loaded"}, 400
        
        results = {}
        total_new = 0
        total_deleted = 0
        
        for channel_id, channel_name in channels:
            success, new_count, deleted_count = fetch_channel_metadata(channel_id, channel_name)
            results[channel_name] = {
                "success": success,
                "new_videos": new_count,
                "deleted_videos": deleted_count
            }
            total_new += new_count
            total_deleted += deleted_count
        
        success_count = sum(1 for r in results.values() if r["success"])
        duration = (datetime.datetime.now() - start_time).total_seconds()
        
        logger.info(f"[END] Completed: {success_count}/{len(channels)} channels ({duration:.2f}s)")
        
        return {
            "message": f"Metadata: {success_count}/{len(channels)} completed",
            "results": results,
            "total_new_videos": total_new,
            "total_deleted_videos": total_deleted,
            "duration_seconds": duration
        }, 200
    
    except Exception as e:
        logger.error(f"[ERROR] {str(e)}", exc_info=True)
        return {"error": str(e)}, 500


Part 3: Streamlit アプリ

ファイル構成

streamlit-dashboard/
├── app.py
├── requirements.txt
├── Dockerfile
└── .dockerignore


requirements.txt

streamlit==1.28.1
google-cloud-bigquery==3.12.0
google-cloud-storage==2.10.0
pandas==2.0.3
plotly==5.17.0


app.py

# -*- coding: utf-8 -*-
"""YouTube クイズ選定ダッシュボード"""

import streamlit as st
import pandas as pd
import plotly.express as px
from google.cloud import bigquery
from google.cloud import storage
import json
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 設定
PROJECT_ID = "YOUR_PROJECT_ID"
DATASET_ID = "youtube_stat"
TABLE_ID = "all_video_statistics_summary"
GCS_BUCKET = "youtube-metrics-bucket"

# キャッシュ
@st.cache_resource
def get_bq_client():
    return bigquery.Client(project=PROJECT_ID)

@st.cache_resource
def get_gcs_client():
    return storage.Client(project=PROJECT_ID)

@st.cache_data(ttl=3600)
def load_metadata_from_gcs(channel_name):
    """メタデータを読み込み"""
    try:
        client = get_gcs_client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(f"metadata/{channel_name}_video_metadata.json")
        
        if blob.exists():
            json_str = blob.download_as_string().decode("utf-8")
            data = json.loads(json_str)
            return {v["videoId"]: v["title"] for v in data.get("videos", [])}
    except Exception as e:
        logger.warning(f"Failed to load metadata: {str(e)}")
    
    return {}

@st.cache_data(ttl=3600)
def load_all_metadata():
    """全メタデータを読み込み"""
    channels = ["sixfonia", "hima72", "kosame", "illuma", "mikoto", "suchi", "lan"]
    all_metadata = {}
    
    for channel in channels:
        all_metadata.update(load_metadata_from_gcs(channel))
    
    return all_metadata

@st.cache_data(ttl=3600)
def get_video_data(start_date, end_date, channel=None):
    """BigQuery からデータを取得"""
    client = get_bq_client()
    
    query = f"""
    SELECT 
        channel,
        videoid,
        view_date,
        viewCount,
        likeCount,
        commentCount,
        view_diff,
        videoURL
    FROM `{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}`
    WHERE view_date >= '{start_date}' AND view_date <= '{end_date}'
    """
    
    if channel:
        query += f" AND channel = '{channel}'"
    
    query += " ORDER BY view_date DESC, view_diff DESC"
    
    df = client.query(query).to_dataframe()
    
    # title を追加
    metadata = load_all_metadata()
    df['title'] = df['videoid'].map(metadata)
    df['title'] = df['title'].fillna('Unknown')
    
    return df

def format_date(date_str):
    """日付フォーマット"""
    if isinstance(date_str, str) and len(date_str) == 8:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str

# ページ設定
st.set_page_config(
    page_title="クイズ選定ダッシュボード",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("🎬 クイズ選定ダッシュボード")

# タブ
tab1, tab2, tab3 = st.tabs(["📊 今日のランキング", "📈 動画推移", "📅 期間分析"])

# タブ 1
with tab1:
    st.header("今日のランキング（前日比）")
    
    col1, col2 = st.columns(2)
    
    with col1:
        today = datetime.now().strftime("%Y%m%d")
        st.metric("集計日", format_date(today))
    
    with col2:
        channels = ["全て"] + ["sixfonia", "hima72", "kosame", "illuma", "mikoto", "suchi", "lan"]
        selected_channel = st.selectbox("チャンネル選択", channels, key="tab1_channel")
    
    df = get_video_data(today, today, channel=None if selected_channel == "全て" else selected_channel)
    
    if not df.empty:
        df_ranked = df.sort_values("view_diff", ascending=False).head(20)
        
        display_df = df_ranked[[
            "channel", "title", "videoid", "viewCount", "view_diff", "likeCount", "commentCount"
        ]].copy()
        
        display_df.columns = ["チャンネル", "タイトル", "動画ID", "再生数", "前日比増分", "高評価", "コメント"]
        
        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True
        )
        
        csv = display_df.to_csv(index=False, encoding="utf-8-sig")
        st.download_button(
            label="📥 CSV ダウンロード",
            data=csv,
            file_name=f"ranking_{today}.csv",
            mime="text/csv",
            key="tab1_download"
        )
    else:
        st.warning("データがありません")

# タブ 2
with tab2:
    st.header("動画の再生数推移")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        start_date = st.date_input(
            "開始日",
            datetime.now() - timedelta(days=30),
            key="tab2_start"
        )
    
    with col2:
        end_date = st.date_input(
            "終了日",
            datetime.now(),
            key="tab2_end"
        )
    
    with col3:
        video_id = st.text_input("動画ID", key="tab2_video_id")
    
    if video_id:
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        
        df = get_video_data(start_str, end_str)
        df_filtered = df[df['videoid'] == video_id].sort_values("view_date")
        
        if not df_filtered.empty:
            fig = px.line(
                df_filtered,
                x="view_date",
                y="viewCount",
                markers=True,
                title=f"動画 {video_id} の再生数推移",
                labels={"view_date": "日付", "viewCount": "再生数"}
            )
            st.plotly_chart(fig, use_container_width=True)
            
            display_df = df_filtered[[
                "view_date", "viewCount", "view_diff", "likeCount", "commentCount"
            ]].copy()
            display_df.columns = ["日付", "再生数", "前日比増分", "高評価", "コメント"]
            
            st.dataframe(display_df, use_container_width=True, hide_index=True)
            
            csv = display_df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                label="📥 CSV ダウンロード",
                data=csv,
                file_name=f"video_{video_id}_{start_str}_{end_str}.csv",
                mime="text/csv",
                key="tab2_download"
            )
        else:
            st.warning(f"動画 {video_id} のデータが見つかりません")

# タブ 3
with tab3:
    st.header("期間別の分析")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        analysis_start = st.date_input(
            "分析開始日",
            datetime.now() - timedelta(days=7),
            key="tab3_start"
        )
    
    with col2:
        analysis_end = st.date_input(
            "分析終了日",
            datetime.now(),
            key="tab3_end"
        )
    
    with col3:
        period = st.selectbox(
            "集計単位",
            ["日別", "週別", "月別"],
            key="tab3_period"
        )
    
    with col4:
        all_channels = ["全て"] + ["sixfonia", "hima72", "kosame", "illuma", "mikoto", "suchi", "lan"]
        analysis_channel = st.selectbox(
            "チャンネル",
            all_channels,
            key="tab3_channel"
        )
    
    start_str = analysis_start.strftime("%Y%m%d")
    end_str = analysis_end.strftime("%Y%m%d")
    
    df = get_video_data(
        start_str,
        end_str,
        channel=None if analysis_channel == "全て" else analysis_channel
    )
    
    if not df.empty:
        if period == "日別":
            df_agg = df.groupby("view_date").agg({
                "viewCount": "sum",
                "view_diff": "sum",
                "likeCount": "sum",
                "commentCount": "sum"
            }).reset_index().sort_values("view_date")
        elif period == "週別":
            df["year_week"] = df["view_date"].str[:4] + "-W" + pd.to_datetime(df["view_date"], format="%Y%m%d").dt.isocalendar().week.astype(str)
            df_agg = df.groupby("year_week").agg({
                "viewCount": "sum",
                "view_diff": "sum",
                "likeCount": "sum",
                "commentCount": "sum"
            }).reset_index()
            df_agg.columns = ["view_date"] + df_agg.columns[1:].tolist()
        else:
            df["year_month"] = df["view_date"].str[:6]
            df_agg = df.groupby("year_month").agg({
                "viewCount": "sum",
                "view_diff": "sum",
                "likeCount": "sum",
                "commentCount": "sum"
            }).reset_index()
            df_agg.columns = ["view_date"] + df_agg.columns[1:].tolist()
        
        fig = px.bar(
            df_agg,
            x="view_date",
            y="view_diff",
            title=f"{period}の前日比増分",
            labels={"view_date": "期間", "view_diff": "前日比増分"}
        )
        st.plotly_chart(fig, use_container_width=True)
        
        display_df = df_agg.copy()
        display_df.columns = ["期間", "総再生数", "前日比増分", "高評価", "コメント"]
        st.dataframe(display_df, use_container_width=True, hide_index=True)
        
        csv = display_df.to_csv(index=False, encoding="utf-8-sig")
        st.download_button(
            label="📥 CSV ダウンロード",
            data=csv,
            file_name=f"analysis_{period}_{start_str}_{end_str}.csv",
            mime="text/csv",
            key="tab3_download"
        )
    else:
        st.warning("指定期間にデータがありません")


Dockerfile

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p ~/.streamlit
RUN echo '[server]\nheadless = true\nport = 8080\nenableXsrfProtection = false\n' > ~/.streamlit/config.toml

EXPOSE 8080

CMD ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0"]


.dockerignore

*.pyc
__pycache__
.git
.gitignore


デプロイ

Cloud Function 1 デプロイ

cd youtube-data-fetch

gcloud functions deploy youtube-data-fetch \
  --runtime python311 \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point main \
  --set-env-vars YOUTUBE_API_KEY=$YOUTUBE_API_KEY,GCS_BUCKET=$GCS_BUCKET,GCP_PROJECT=$PROJECT_ID \
  --timeout 540 \
  --memory 512MB \
  --region asia-northeast1


Cloud Function 2 デプロイ

cd ../youtube-video-metadata-fetch

gcloud functions deploy youtube-video-metadata-fetch \
  --runtime python311 \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point main \
  --set-env-vars YOUTUBE_API_KEY=$YOUTUBE_API_KEY,GCS_BUCKET=$GCS_BUCKET \
  --timeout 540 \
  --memory 512MB \
  --region asia-northeast1


Cloud Scheduler 設定

# Scheduler 1
gcloud scheduler jobs create http youtube-daily-stats \
  --schedule "0 1 * * *" \
  --timezone "Asia/Tokyo" \
  --uri https://asia-northeast1-$PROJECT_ID.cloudfunctions.net/youtube-data-fetch \
  --http-method GET

# Scheduler 2
gcloud scheduler jobs create http youtube-daily-metadata \
  --schedule "15 1 * * *" \
  --timezone "Asia/Tokyo" \
  --uri https://asia-northeast1-$PROJECT_ID.cloudfunctions.net/youtube-video-metadata-fetch \
  --http-method GET


Streamlit - Cloud Run デプロイ

cd ../streamlit-dashboard

gcloud run deploy quiz-dashboard \
  --source . \
  --platform managed \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --timeout 3600 \
  --max-instances 5


運用

毎日のフロー

1:00  → Cloud Function 1 実行（stats 取得）
1:15  → Cloud Function 2 実行（メタデータ取得）
1:30  → Looker Studio メール送信
       → Slack チャンネルに自動投稿
朝    → Slack で「今日のランキング」を確認
       → 詳細分析が必要なら Streamlit を開く


ログ確認

# Cloud Function ログ
gcloud functions logs read youtube-data-fetch --limit 50
gcloud functions logs read youtube-video-metadata-fetch --limit 50

# Cloud Run ログ
gcloud run logs read quiz-dashboard --limit 50 --region asia-northeast1


BigQuery データ確認

# 今日のデータを確認
bq query --use_legacy_sql=false "
SELECT 
  channel, 
  COUNT(*) as count,
  MAX(view_date) as latest_date
FROM \`$PROJECT_ID.youtube_stat.all_video_statistics_summary\`
GROUP BY channel
ORDER BY latest_date DESC
"


まとめ

このドキュメント 1 つで、YouTube クイズ選定システム全体を実装できます。





Cloud Function: データ取得パイプライン



BigQuery: データウェアハウス



Looker Studio: 毎日のメール通知



Streamlit: 詳細分析ツール



Slack: 通知先

全て自動化され、毎日の朝には「昨日のランキング」が Slack に届きます。
```
