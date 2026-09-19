"""pipeline/（GCP の日次収集パイプライン）のテスト。

Cloud Functions の main.py は import 時に YouTube API クライアントを作り、
google-cloud-storage / google-cloud-bigquery / functions-framework にも依存する。CI にはそれらが無いので、
import の間だけ偽物のモジュールに差し替えて読み込み、API・GCS・BigQuery は偽物で置き換える。
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
    bigquery = types.ModuleType("google.cloud.bigquery")
    bigquery.Client = mock.MagicMock(name="BigQueryClient")
    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    cloud.bigquery = bigquery
    google = types.ModuleType("google")
    google.cloud = cloud

    return {
        "functions_framework": ff,
        "googleapiclient": gapi,
        "googleapiclient.discovery": discovery,
        "google": google,
        "google.cloud": cloud,
        "google.cloud.storage": storage,
        "google.cloud.bigquery": bigquery,
    }


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path / "main.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, _stub_modules()), \
            mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": "dummy", "BQ_TABLE": "p.d.video_statistics",
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


def fake_bigquery():
    """実行されたクエリ・読み込みを記録する偽の BigQuery"""
    calls = []

    class Job:
        def result(self):
            return None

    class Client:
        def query(self, sql, job_config=None):
            calls.append(("query", sql, job_config.params))
            return Job()

        def load_table_from_json(self, rows, table, job_config=None):
            calls.append(("load", table, rows, job_config.write_disposition))
            return Job()

    class QueryJobConfig:
        def __init__(self, query_parameters):
            self.params = {p[0]: p[1:] for p in query_parameters}

    class LoadJobConfig:
        def __init__(self, write_disposition):
            self.write_disposition = write_disposition

    module = types.SimpleNamespace(
        Client=Client,
        QueryJobConfig=QueryJobConfig,
        LoadJobConfig=LoadJobConfig,
        ScalarQueryParameter=lambda name, typ, value: (name, typ, value),
        ArrayQueryParameter=lambda name, typ, values: (name, typ, values),
        WriteDisposition=types.SimpleNamespace(WRITE_APPEND="WRITE_APPEND"),
    )
    return module, calls


def stat_row(vid="v1", view_date="20260918"):
    return {"videoId": vid, "viewCount": "10", "likeCount": "2", "commentCount": "1",
            "videoURL": f"https://www.youtube.com/watch?v={vid}", "thumbnail": "t", "view_date": view_date}


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


def test_save_to_gcs_writes_csv_with_view_date_last(stats):
    storage, uploaded = fake_storage()
    stats.storage = storage
    row = {"videoId": "v1", "viewCount": "10", "likeCount": "2", "commentCount": "1",
           "videoURL": "https://www.youtube.com/watch?v=v1", "thumbnail": "t", "view_date": "20260918"}

    stats.save_to_gcs("lan", "lan_video_statistics_20260918.csv", [row])

    body = uploaded["lan/lan_video_statistics_20260918.csv"]
    header, first = list(csv.reader(io.StringIO(body)))
    assert header == ["videoId", "viewCount", "likeCount", "commentCount", "videoURL", "thumbnail", "view_date"]
    assert first[-1] == "20260918"


def test_bq_rows_match_table_columns(stats):
    """BigQuery に読み込む行の列と、テーブル定義（sql/01）の列がずれていない"""
    ddl = (PIPELINE / "sql" / "01_create_table.sql").read_text(encoding="utf-8")
    body = ddl[ddl.index("(") + 1:ddl.index(")\nPARTITION BY")]
    table_cols = re.findall(r"^\s*(\w+)\s+(?:STRING|INT64|DATE)", body, flags=re.M)

    row = stats.to_bq_rows("lan", [stat_row()])[0]

    assert list(row) == table_cols


def test_to_bq_rows_types_counts_and_date(stats):
    row = stats.to_bq_rows("lan", [stat_row(view_date="20260918")])[0]

    assert row["channel"] == "lan"
    assert (row["viewCount"], row["likeCount"], row["commentCount"]) == (10, 2, 1)
    assert row["view_date"] == "2026-09-18"


def test_load_to_bigquery_deletes_the_day_then_appends(stats):
    """その日・取得できたチャンネルの行だけを消してから追加する（リトライしても重複しない）"""
    bq, calls = fake_bigquery()
    stats.bigquery = bq

    n = stats.load_to_bigquery({"lan": [stat_row("v1"), stat_row("v2")], "hima72": [stat_row("v3")]}, "20260918")

    assert n == 3
    (kind, sql, params), (kind2, table, rows, disposition) = calls
    assert kind == "query" and sql.startswith("DELETE FROM `p.d.video_statistics`")
    assert params["day"] == ("DATE", datetime.date(2026, 9, 18))
    assert params["channels"] == ("STRING", ["lan", "hima72"])
    assert kind2 == "load" and table == "p.d.video_statistics" and disposition == "WRITE_APPEND"
    assert [r["videoId"] for r in rows] == ["v1", "v2", "v3"]


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
    loaded = []

    def fetch(cid, cname, view_date):
        seen.append(view_date)
        return [stat_row()] if cname == "ok" else None

    stats.fetch_channel_data = fetch
    stats.load_to_bigquery = lambda rows, view_date: loaded.append((sorted(rows), view_date)) or 1
    stats.copy_snapshot_to_gcs = lambda: "2026-09-19T02:21:52+09:00"

    body, status = stats.main(None)

    assert seen == ["20260918", "20260918"]
    assert status == 500
    assert body["results"] == {"ok": True, "ng": False}
    # 取得できたチャンネルだけは読み込む（リトライで残りが埋まる）
    assert loaded == [(["ok"], "20260918")]


def test_main_returns_500_when_bigquery_load_fails(stats):
    stats.load_channels = lambda: [("c1", "ok")]
    stats.fetch_channel_data = lambda cid, cname, view_date: [stat_row()]
    stats.copy_snapshot_to_gcs = lambda: None

    def boom(rows, view_date):
        raise RuntimeError("bq down")

    stats.load_to_bigquery = boom

    body, status = stats.main(None)

    assert status == 500
    assert body["results"] == {"ok": True}
    assert body["bq_rows"] == 0


def test_main_skips_bigquery_when_every_channel_fails(stats):
    """全チャンネル失敗なら BigQuery を触らない（前の試行で入った分を消さない）"""
    stats.load_channels = lambda: [("c1", "a"), ("c2", "b")]
    stats.fetch_channel_data = lambda cid, cname, view_date: None
    stats.copy_snapshot_to_gcs = lambda: None
    stats.load_to_bigquery = mock.MagicMock()

    body, status = stats.main(None)

    assert status == 500
    stats.load_to_bigquery.assert_not_called()


def test_fetch_channel_data_returns_none_on_api_error(stats):
    def boom(_):
        raise ValueError("No channel found")

    stats.get_channel_uploads_playlist_id = boom
    assert stats.fetch_channel_data("c", "lan", "20260918") is None


# ---- 動画マスタ（videos.json）のコピー --------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, stats, body):
    monkeypatch.setattr(stats.urllib.request, "urlopen", lambda url, timeout: FakeResponse(body))


def test_copy_snapshot_saves_body_as_is(stats, monkeypatch):
    body = json.dumps({"updated_at": "2026-09-19T02:21:52+09:00",
                       "videos": [{"videoId": "v1", "title": "タイトル"}]}, ensure_ascii=False).encode("utf-8")
    _serve(monkeypatch, stats, body)
    storage, uploaded = fake_storage()
    stats.storage = storage

    assert stats.copy_snapshot_to_gcs() == "2026-09-19T02:21:52+09:00"
    assert uploaded["master/videos.json"] == body


@pytest.mark.parametrize("body", [
    b"<html>error</html>",
    b'{"updated_at": "x", "videos": []}',
    b'{"updated_at": "x"}',
    b"[]",
])
def test_copy_snapshot_rejects_invalid_body_without_overwriting(stats, monkeypatch, body):
    _serve(monkeypatch, stats, body)
    storage, uploaded = fake_storage()
    stats.storage = storage

    with pytest.raises(ValueError):
        stats.copy_snapshot_to_gcs()
    assert uploaded == {}


def test_main_snapshot_failure_does_not_fail_stats(stats):
    stats.load_channels = lambda: [("c1", "ok")]
    stats.fetch_channel_data = lambda cid, cname, view_date: [stat_row()]
    stats.load_to_bigquery = lambda rows, view_date: 1

    def boom():
        raise OSError("network down")

    stats.copy_snapshot_to_gcs = boom

    body, status = stats.main(None)

    assert status == 200
    assert body["snapshot_updated_at"] is None
