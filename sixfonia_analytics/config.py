"""チャンネル定義・スキーマ・パス・語彙プリセットの一元管理。

チャンネル追加やフォルダ変更はこのファイルだけを編集する。
"""

from __future__ import annotations

import re
from pathlib import Path

# ============================================================
# チャンネル定義
# ============================================================
CHANNELS = [
    {"name": "hima72",   "display": "暇72",      "channel_id": "UCr24Ll7IT2hPquu-n11dNWQ", "handle": "hima72"},
    {"name": "sixfonia", "display": "シクフォニ",  "channel_id": "UCGMG8BNfA8gsH9Rn_d_yW2A", "handle": None},
    {"name": "kosame",   "display": "雨乃こさめ",  "channel_id": "UC1FByWnYWrAWcCmijBURkfg", "handle": None},
    {"name": "illuma",   "display": "いるま",     "channel_id": "UCs7eh4DZC6HXiizJXLj3_FA", "handle": None},
    {"name": "mikoto",   "display": "みこと",     "channel_id": "UCxP9lRliG6xyAbx8Dp3OR1w", "handle": None},
    {"name": "suchi",    "display": "すち",      "channel_id": "UCB8DR-ao7ZfoAwoYRXE3VJw", "handle": None},
    {"name": "lan",      "display": "LAN",       "channel_id": "UCEqBb37k-pKRQPG-_MvtAeA", "handle": None},
]

CHANNEL_NAMES = [c["name"] for c in CHANNELS]
CHANNEL_BY_NAME = {c["name"]: c for c in CHANNELS}


def channel_id_of(name: str) -> str:
    return CHANNEL_BY_NAME[name]["channel_id"]


# ============================================================
# CSVスキーマ
# ============================================================
# view_date は YYYYMMDD 文字列（クリーニング済みファイルと同一仕様）
MASTER_COLUMNS = ["videoId", "viewCount", "likeCount", "commentCount", "videoURL", "view_date"]

# ============================================================
# Google Drive パス
# ============================================================
DRIVE_ROOT = Path("/content/drive/MyDrive")

# 正: チャンネル別フォルダ
ANALYTICS_BASE = DRIVE_ROOT / "sixfonia_yt_analytics"

# 旧: フラットフォルダ（移行完了後に廃止予定）
LEGACY_DATA_DIR = DRIVE_ROOT / "YouTube_Data"

# 移行期は両方に書き込む。YouTube_Data 廃止時に False へ。
LEGACY_WRITE = True


def channel_dir(name: str) -> Path:
    return ANALYTICS_BASE / name


def stats_filename(channel_name: str, date_str: str) -> str:
    return f"{channel_name}_video_statistics_{date_str}.csv"


# ファイル名から (channel, YYYYMMDD) を取り出す
STATS_FILE_RE = re.compile(r"^(.+)_video_statistics_(\d{8})\.csv$")

# ============================================================
# GCS（既存の oshi-katsu バケット運用を踏襲）
# ============================================================
GCS_BUCKET = "oshi-katsu"
GCS_PREFIX = "youtube_stat"

# ============================================================
# コメント分析: 語彙プリセット
# ------------------------------------------------------------
# single_words: 形態素解析で分割されがちな複合語を1語として扱うリスト
# stop_words  : 集計から除外する語
# 旧08（おそ松さん分析）の stop_core にはカンマ抜けで
# "ホント"+"めっちゃ" が連結されるバグがあったため修正済み。
# ============================================================
VOCAB_PRESETS: dict[str, dict] = {
    # 汎用（旧09ベース）
    "default": {
        "single_words": [
            "こさめ", "雨乃こさめ", "暇72", "暇なつ", "ひまなつ", "なつくん", "なっちゃん",
            "いるま", "いるませんせー", "みこと", "すち", "LAN", "シクフォニ", "シクファミ",
            "人マニア", "以心伝心", "かっこいい", "かわいい", "歌ってみた", "声真似",
        ],
        "stop_words": {
            "の", "が", "こと", "ため", "よう", "そう", "もの", "とき", "ところ", "せい",
            "やつ", "まま", "これ", "それ", "ある", "する", "いる", "なる", "人", "方",
            "ヤツ", "あと", "いい", "ほんま", "ない", "なく", "とこ", "ほんと",
        },
        "synonym_map": {
            "かわいい": "可愛い",
            "かわい": "可愛い",
            "面白": "面白い",
            "嬉し": "嬉しい",
        },
    },
    # 旧08: 好きすぎて松（おそ松さん声真似）分析用
    "osomatsu": {
        "single_words": [
            "なつ", "暇72", "暇なつ", "ひまなつ", "うえのはら", "nohara", "uenohara",
            "もるでお", "無音", "ムチャ", "梓月", "み！るきーず", "シクフォニ", "シクファミ",
            "おそ松さん", "おそ松くん", "おそ松", "六つ子", "6つ子", "松野", "イヤミ",
            "イヤミラッパー", "シェー", "若葉松", "カラ松", "一松", "チョロ松", "十四松",
            "トド松", "トッティ", "マッスル", "ハッスル", "松",
            "神谷浩史", "中村悠一", "小野大輔", "櫻井孝宏",
            "好きすぎて松", "好きすぎて滅", "M!LK", "MILK", "ミルク", "Blessing",
            "マツLove", "LOVE1000%", "松LOVE1000％", "マジLOVE1000％", "松LOVE", "マジLOVE",
            "ノンフィクション", "フラッシュバック", "シュガーソングとビターステップ",
            "恋愛裁判", "逆転裁判", "声真似", "替え歌", "歌ってみた", "歌い手", "歌みた",
            "10年ぶり", "10年前", "10周年", "10年間", "令和", "令和版", "平成", "同窓会",
            "小学生", "中学生", "高校生", "大学生", "社会人", "誕生日", "3DS",
            "メンツ", "メンバー", "クオリティ", "解釈一致", "かっこいい", "かっこよすぎる",
            "おすすめ", "再生", "高評価", "チャンネル登録", "ハイプ", "サムネ", "リアタイ",
            "タイムスリップ", "タイムリープ", "おかえり", "おかえりなさい", "お世話になりました",
            "なつかしい", "大号泣", "号泣", "宇宙の果ての果て", "リア充撲滅委員会", "撲松委員会",
            "過去イチ", "パワーアップ", "バージョンアップ", "あのころ", "そばにいて",
        ],
        "stop_words": {
            "の", "ん", "こと", "もの", "ため", "よう", "そう", "やつ", "とき", "ところ",
            "これ", "それ", "あれ", "ここ", "そこ", "人", "方", "時", "今", "もう", "まだ",
            "本当", "本当に", "ほんと", "ほんとに", "ほんとう", "ほんま", "ホント",
            "めっちゃ", "めちゃ", "めちゃくちゃ", "すごく",
            "一気", "思い", "思う", "感じ", "動画",
        },
        "synonym_map": {},
    },
}
