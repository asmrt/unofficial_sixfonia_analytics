# -*- coding: utf-8 -*-
"""pipeline/tools/migrate_legacy_csv.py のテスト。

google-cloud-storage が CI に無いため、test_pipeline.py と同じ流儀で import 時だけ偽物に差し替える。
"""

import csv
import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "pipeline" / "tools" / "migrate_legacy_csv.py"

LEGACY_HEADER = "videoId,viewCount,likeCount,commentCount,videoURL,view_date\n"


def _stub_modules():
    storage = types.ModuleType("google.cloud.storage")
    storage.Client = mock.MagicMock(name="Client")
    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    google = types.ModuleType("google")
    google.cloud = cloud
    return {
        "google": google,
        "google.cloud": cloud,
        "google.cloud.storage": storage,
    }


def _load():
    spec = importlib.util.spec_from_file_location("migrate_legacy_csv", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, _stub_modules()):
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def mig():
    return _load()


# ---- 偽の GCS（list_blobs / exists / download_as_text 付き） --------------

class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name

    def download_as_text(self):
        return self.bucket.objects[self.name]

    def exists(self):
        return self.name in self.bucket.objects

    def upload_from_string(self, data, content_type=None):
        self.bucket.objects[self.name] = data


class FakeBucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        return FakeBlob(self, name)

    def list_blobs(self, prefix=None):
        for name in list(self.objects):
            if prefix is None or name.startswith(prefix):
                yield self.blob(name)


class FakeClient:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, FakeBucket())


# ---- convert_csv ----------------------------------------------------------

def test_convert_csv_appends_channel_and_keeps_column_order(mig):
    text = LEGACY_HEADER + "v1,10,2,1,https://x/v1,20260918\nv2,20,3,2,https://x/v2,20260918\n"

    converted, problems = mig.convert_csv(text, "lan", "20260918")

    assert problems == []
    rows = list(csv.reader(io.StringIO(converted)))
    assert rows[0] == mig.NEW_COLUMNS
    assert rows[1] == ["v1", "10", "2", "1", "https://x/v1", "20260918", "lan"]
    assert rows[2][-1] == "lan"


def test_convert_csv_reports_header_mismatch_and_leaves_text_unchanged(mig):
    text = "a,b,c\n1,2,3\n"

    converted, problems = mig.convert_csv(text, "lan", "20260918")

    assert converted == text
    assert any("列が想定と違う" in p for p in problems)


def test_convert_csv_reports_view_date_mismatch_but_keeps_row(mig):
    text = LEGACY_HEADER + "v1,10,2,1,https://x/v1,20260101\n"

    converted, problems = mig.convert_csv(text, "lan", "20260918")

    assert any("view_date" in p for p in problems)
    rows = list(csv.reader(io.StringIO(converted)))
    assert rows[1][-1] == "lan"
    assert rows[1][5] == "20260101"  # 元の view_date は書き換えない


def test_convert_csv_reports_float_like_count_but_keeps_row(mig):
    text = LEGACY_HEADER + "v1,123.0,2,1,https://x/v1,20260918\n"

    converted, problems = mig.convert_csv(text, "lan", "20260918")

    assert any("viewCount" in p for p in problems)
    rows = list(csv.reader(io.StringIO(converted)))
    assert rows[1][1] == "123.0"


def test_convert_csv_leaves_already_converted_file_unchanged(mig):
    text = ",".join(mig.NEW_COLUMNS) + "\nv1,10,2,1,https://x/v1,20260918,lan\n"

    converted, problems = mig.convert_csv(text, "lan", "20260918")

    assert converted == text
    assert problems == ["変換済み"]


# ---- parse_blob_name -------------------------------------------------------

def test_parse_blob_name_parses_valid_path(mig):
    assert mig.parse_blob_name("youtube_stat/lan/lan_video_statistics_20260918.csv") == ("lan", "20260918")


def test_parse_blob_name_rejects_folder_filename_mismatch(mig):
    assert mig.parse_blob_name("youtube_stat/lan/other_video_statistics_20260918.csv") is None


def test_parse_blob_name_rejects_unrelated_names(mig):
    assert mig.parse_blob_name("youtube_stat/lan/lan_stats.csv") is None
    assert mig.parse_blob_name("not_enough_parts.csv") is None


# ---- migrate ----------------------------------------------------------------

def test_migrate_dry_run_writes_nothing(mig):
    client = FakeClient()
    src = client.bucket("src")
    src.objects["youtube_stat/lan/lan_video_statistics_20260918.csv"] = (
        LEGACY_HEADER + "v1,10,2,1,https://x/v1,20260918\n"
    )

    summary = mig.migrate("src", "youtube_stat", "dst", client=client)

    assert summary["files"] == 1
    assert summary["rows"] == 1
    assert summary["written"] == 0
    assert client.bucket("dst").objects == {}


def test_migrate_apply_writes_expected_blob_names_and_skips_existing(mig):
    client = FakeClient()
    src = client.bucket("src")
    src.objects["youtube_stat/lan/lan_video_statistics_20260918.csv"] = (
        LEGACY_HEADER + "v1,10,2,1,https://x/v1,20260918\n"
    )
    src.objects["youtube_stat/han/han_video_statistics_20260919.csv"] = (
        LEGACY_HEADER + "v2,20,3,2,https://x/v2,20260919\n"
    )
    dst = client.bucket("dst")
    dst.objects["han/han_video_statistics_20260919.csv"] = "既存の内容（上書きされない）"

    summary = mig.migrate("src", "youtube_stat", "dst", apply=True, client=client)

    assert summary["written"] == 1
    assert summary["skipped_existing"] == 1
    assert "lan/lan_video_statistics_20260918.csv" in dst.objects
    written = dst.objects["lan/lan_video_statistics_20260918.csv"]
    assert list(csv.reader(io.StringIO(written)))[0] == mig.NEW_COLUMNS
    # 既存ファイルは触っていない
    assert dst.objects["han/han_video_statistics_20260919.csv"] == "既存の内容（上書きされない）"


def test_migrate_reports_unparseable_blob_names_as_problems(mig):
    client = FakeClient()
    src = client.bucket("src")
    src.objects["youtube_stat/lan/lan_stats.csv"] = "junk"

    summary = mig.migrate("src", "youtube_stat", "dst", client=client)

    assert summary["files"] == 0
    assert any("lan_stats.csv" in p for p in summary["problems"])


def test_migrate_does_not_write_files_it_cannot_convert(mig):
    """列が想定と違う・空のファイルは、apply でも移行先に書き込まない。

    並びの違う CSV が移行先に混じると、外部テーブル経由のクエリ全体が失敗するため。
    """
    client = FakeClient()
    src = client.bucket("src")
    src.objects["youtube_stat/lan/lan_video_statistics_20260918.csv"] = (
        "videoId,viewCount\nv1,10\n"
    )
    src.objects["youtube_stat/han/han_video_statistics_20260919.csv"] = ""

    summary = mig.migrate("src", "youtube_stat", "dst", apply=True, client=client)

    assert summary["files"] == 2
    assert summary["written"] == 0
    assert summary["skipped_broken"] == 2
    assert client.bucket("dst").objects == {}
    assert any("列が想定と違う" in p for p in summary["problems"])
    assert any("空のファイル" in p for p in summary["problems"])


def test_migrate_writes_already_converted_file_as_is(mig):
    """すでに7列のファイルは、変換せずそのまま書き込む"""
    client = FakeClient()
    src = client.bucket("src")
    body = ",".join(mig.NEW_COLUMNS) + "\nv1,10,2,1,https://x/v1,20260918,lan\n"
    src.objects["youtube_stat/lan/lan_video_statistics_20260918.csv"] = body

    summary = mig.migrate("src", "youtube_stat", "dst", apply=True, client=client)

    assert summary["written"] == 1
    assert summary["skipped_broken"] == 0
    assert client.bucket("dst").objects["lan/lan_video_statistics_20260918.csv"] == body
