"""pipeline/（GCP の日次収集パイプライン）のテスト。

Cloud Functions の main.py は import 時に YouTube API クライアントを作り、
google-cloud-storage / functions-framework にも依存する。CI にはそれらが無いので、
import の間だけ偽物のモジュールに差し替えて読み込み、API・GCS は偽物で置き換える。
"""

import csv
import datetime
import importlib.util
import io
import json
import re
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from sixfonia_analytics import config

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "pipeline"
STATS_DIR = PIPELINE / "youtube-data-fetch"


def _stub_modules():
    """main.py の import に必要な外部モジュールの偽物"""
    ff = types.ModuleType("functions_framework")
    ff.http = lambda f: f

    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = mock.MagicMock(name="build")
    gapi = types.ModuleType("googleapiclient")
    gapi.discovery = discovery

    storage = types.ModuleType("google.cloud.storage")
    storage.Client = mock.MagicMock(name="Client")
    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    google = types.ModuleType("google")
    google.cloud = cloud

    return {
        "functions_framework": ff,
        "googleapiclient": gapi,
        "googleapiclient.discovery": discovery,
        "google": google,
        "google.cloud": cloud,
        "google.cloud.storage": storage,
    }


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path / "main.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, _stub_modules()), \
            mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": "dummy",
                                 "SNAPSHOT_URL": "https://example.com/videos.json"}):
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def stats():
    return _load(STATS_DIR, "pipeline_stats_main")


class FakeRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class FakeYouTube:
    """videos().list(id=...) に、渡された ID ぶんの応答を返す偽の API クライアント"""

    def __init__(self, make_item):
        self.make_item = make_item
        self.calls = []

    def videos(self):
        return self

    def list(self, part, id):
        ids = id.split(",")
        self.calls.append(ids)
        return FakeRequest({"items": [self.make_item(v) for v in ids]})


def fake_storage():
    """upload_from_string された内容をパスごとに記録する偽の GCS"""
    uploaded = {}

    class Blob:
        def __init__(self, path):
            self.path = path

        def upload_from_string(self, data, content_type=None):
            uploaded[self.path] = data

    class Bucket:
        def blob(self, path):
            return Blob(path)

    class Client:
        def bucket(self, name):
            return Bucket()

    return types.SimpleNamespace(Client=Client), uploaded


def stat_row(vid="v1", view_date="20260918", channel="lan"):
    return {"videoId": vid, "viewCount": "10", "likeCount": "2", "commentCount": "1",
            "videoURL": f"https://www.youtube.com/watch?v={vid}", "thumbnail": "t",
            "view_date": view_date, "channel": channel}


def _parse_create_table_columns(sql_text, table_index=0):
    """01_create_external_tables.sql から table_index 番目の CREATE 文の列名一覧を取る"""
    starts = [m.start() for m in re.finditer(r"CREATE OR REPLACE EXTERNAL TABLE", sql_text)]
    start = starts[table_index]
    end = starts[table_index + 1] if table_index + 1 < len(starts) else len(sql_text)
    stmt = sql_text[start:end]
    body = stmt[stmt.index("(") + 1:stmt.index(")\nOPTIONS")]
    return re.findall(r"^\s*(\w+)\s+(?:STRING|INT64|BOOL|TIMESTAMP|ARRAY)", body, flags=re.M)


# ---- チャンネル定義 ------------------------------------------------------

def _channels_json(path):
    with open(path / "channels.json", encoding="utf-8") as f:
        return [(c["id"], c["name"]) for c in json.load(f)["channels"]]


def test_channels_json_matches_package_config():
    """チャンネル定義（config.py と channels.json）が一致している"""
    expected = [(c["channel_id"], c["name"]) for c in config.CHANNELS]
    assert _channels_json(STATS_DIR) == expected


def test_load_channels_reads_json_next_to_main(stats):
    assert stats.load_channels() == _channels_json(STATS_DIR)


# ---- youtube-data-fetch（統計） -----------------------------------------

def test_get_video_statistics_batches_by_50_and_defaults_missing_counts(stats):
    def item(vid):
        if vid == "v0":  # 統計が非公開の動画
            return {"id": vid}
        return {"id": vid, "statistics": {"viewCount": "10", "likeCount": "2", "commentCount": "1"}}

    yt = FakeYouTube(item)
    stats.youtube = yt
    stats.time = mock.MagicMock()  # sleep を飛ばす

    rows = stats.get_video_statistics([f"v{i}" for i in range(120)])

    assert [len(c) for c in yt.calls] == [50, 50, 20]
    assert len(rows) == 120
    assert rows[0]["viewCount"] == "0" and rows[0]["likeCount"] == "0"
    assert rows[1]["viewCount"] == "10"
    assert rows[1]["videoURL"] == "https://www.youtube.com/watch?v=v1"


def test_save_to_gcs_writes_csv_with_channel_last(stats):
    storage, uploaded = fake_storage()
    stats.storage = storage
    row = stat_row()

    stats.save_to_gcs("lan", "lan_video_statistics_20260918.csv", [row])

    body = uploaded["lan/lan_video_statistics_20260918.csv"]
    header, first = list(csv.reader(io.StringIO(body)))
    assert header == ["videoId", "viewCount", "likeCount", "commentCount", "videoURL",
                       "thumbnail", "view_date", "channel"]
    assert first[-1] == "lan"
    assert first[-2] == "20260918"


def test_csv_header_matches_ext_video_statistics_columns():
    """CSV の列順と、外部テーブル定義（sql/01, ext_video_statistics）の列がずれていない"""
    ddl = (PIPELINE / "sql" / "01_create_external_tables.sql").read_text(encoding="utf-8")
    table_cols = _parse_create_table_columns(ddl, table_index=0)

    assert table_cols == ["videoId", "viewCount", "likeCount", "commentCount", "videoURL",
                           "thumbnail", "view_date", "channel"]


def test_ext_videos_columns_are_keys_of_a_video_dict():
    """ext_videos（sql/01 の2つ目の CREATE）の全列が、動画マスタの動画1件分に含まれている"""
    ddl = (PIPELINE / "sql" / "01_create_external_tables.sql").read_text(encoding="utf-8")
    table_cols = _parse_create_table_columns(ddl, table_index=1)

    sample_video = {"videoId": "v1", "channel": "lan", "title": "タイトル",
                     "publishedAt": "2026-09-18T00:00:00Z", "durationSec": 120,
                     "isShort": False, "thumbnail": "t", "tags": ["a"], "available": True}

    assert table_cols
    for col in table_cols:
        assert col in sample_video


def test_main_uses_yesterday_in_jst_and_returns_500_on_partial_failure(stats):
    # 2026-09-19 00:30 JST（UTC ではまだ 9/18）に実行 → view_date は JST の前日 20260918
    fixed = datetime.datetime(2026, 9, 19, 0, 30, tzinfo=stats.JST)

    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    stats.datetime = types.SimpleNamespace(datetime=FixedDatetime, timedelta=datetime.timedelta)
    stats.load_channels = lambda: [("c1", "ok"), ("c2", "ng")]
    seen = []

    def fetch(cid, cname, view_date):
        seen.append(view_date)
        return cname == "ok"

    stats.fetch_channel_data = fetch
    stats.copy_snapshot_to_gcs = lambda: "2026-09-19T02:21:52+09:00"

    body, status = stats.main(None)

    assert seen == ["20260918", "20260918"]
    assert status == 500
    assert body["results"] == {"ok": True, "ng": False}


def test_fetch_channel_data_returns_false_on_api_error(stats):
    def boom(_):
        raise ValueError("No channel found")

    stats.get_channel_uploads_playlist_id = boom
    assert stats.fetch_channel_data("c", "lan", "20260918") is False


def test_fetch_channel_data_sets_channel_and_view_date_on_every_row(stats):
    stats.get_channel_uploads_playlist_id = lambda channel_id: "PL1"
    stats.get_videos_from_playlist = lambda playlist_id: ["v1", "v2"]
    stats.get_video_statistics = lambda video_ids: [
        {"videoId": vid, "viewCount": "1", "likeCount": "0", "commentCount": "0",
         "videoURL": f"https://www.youtube.com/watch?v={vid}", "thumbnail": ""}
        for vid in video_ids
    ]
    saved = {}
    stats.save_to_gcs = lambda channel_name, filename, data: saved.update(rows=data)

    assert stats.fetch_channel_data("c1", "lan", "20260918") is True
    assert len(saved["rows"]) == 2
    for row in saved["rows"]:
        assert row["channel"] == "lan"
        assert row["view_date"] == "20260918"


# ---- 動画マスタ（videos.json / videos.ndjson）のコピー --------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, stats, body):
    monkeypatch.setattr(stats.urllib.request, "urlopen", lambda url, timeout: FakeResponse(body))


def test_copy_snapshot_writes_json_as_is_and_ndjson_one_line_per_video(stats, monkeypatch):
    videos = [{"videoId": "v1", "title": "タイトル"}, {"videoId": "v2", "title": "その2"}]
    body = json.dumps({"updated_at": "2026-09-19T02:21:52+09:00", "videos": videos},
                       ensure_ascii=False).encode("utf-8")
    _serve(monkeypatch, stats, body)
    storage, uploaded = fake_storage()
    stats.storage = storage

    assert stats.copy_snapshot_to_gcs() == "2026-09-19T02:21:52+09:00"
    assert uploaded["master/videos.json"] == body

    ndjson_lines = uploaded["master/videos.ndjson"].splitlines()
    assert [json.loads(line) for line in ndjson_lines] == videos


@pytest.mark.parametrize("body", [
    b"<html>error</html>",
    b'{"updated_at": "x", "videos": []}',
    b'{"updated_at": "x"}',
    b"[]",
])
def test_copy_snapshot_rejects_invalid_body_without_writing_anything(stats, monkeypatch, body):
    _serve(monkeypatch, stats, body)
    storage, uploaded = fake_storage()
    stats.storage = storage

    with pytest.raises(ValueError):
        stats.copy_snapshot_to_gcs()
    assert uploaded == {}


def test_main_snapshot_failure_does_not_fail_stats(stats):
    stats.load_channels = lambda: [("c1", "ok")]
    stats.fetch_channel_data = lambda cid, cname, view_date: True

    def boom():
        raise OSError("network down")

    stats.copy_snapshot_to_gcs = boom

    body, status = stats.main(None)

    assert status == 200
    assert body["snapshot_updated_at"] is None
