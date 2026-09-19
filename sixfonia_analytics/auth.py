"""APIキーの取得と YouTube Data API クライアント構築。

キーは Colab Secrets（YOUTUBE_API_KEY）または環境変数から読む。
コードへの直書きは禁止（過去にキー露出事故があったため）。
"""

from __future__ import annotations

import os

_SECRET_NAMES = ("YOUTUBE_API_KEY", "YOUTUBE_APY_KEY")  # 後者は旧タイポ名（互換用・廃止予定）


def get_api_key() -> str:
    """Colab Secrets → 環境変数の順に YouTube APIキーを探す。"""
    try:
        from google.colab import userdata  # type: ignore

        for name in _SECRET_NAMES:
            try:
                key = userdata.get(name)
                if key:
                    if name != "YOUTUBE_API_KEY":
                        print(f"[WARN] 旧Secret名 {name} を使用中。YOUTUBE_API_KEY への移行を推奨")
                    return key
            except Exception:
                continue
    except ImportError:
        pass

    for name in _SECRET_NAMES:
        key = os.environ.get(name)
        if key:
            return key

    raise RuntimeError(
        "YouTube APIキーが見つかりません。Colab の Secrets(🔑) に "
        "YOUTUBE_API_KEY を登録してください。"
    )


def build_youtube(api_key: str | None = None):
    """YouTube Data API v3 のサービスオブジェクトを返す。"""
    from googleapiclient.discovery import build

    return build("youtube", "v3", developerKey=api_key or get_api_key())
