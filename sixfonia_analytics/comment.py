"""コメント取得と単語抽出（旧09ベース、旧08のロジックを包含）。

nagisa / wordcloud は重いのでこのモジュール内で遅延importする。
"""

from __future__ import annotations

from collections import Counter

import pandas as pd


# ============================================================
# コメント取得（返信含む全件）
# ============================================================
def fetch_comments(youtube, video_id: str, max_comments: int = 0) -> pd.DataFrame:
    """指定動画のコメント（返信含む）を DataFrame で返す。max_comments=0 で無制限。"""
    rows: list[dict] = []
    page_token = None

    def _row(c, parent_id=None, is_reply=False):
        s = c["snippet"]
        return {
            "video_id": video_id,
            "comment_id": c["id"],
            "parent_id": parent_id,
            "author": s["authorDisplayName"],
            "text": s["textOriginal"],
            "like_count": s["likeCount"],
            "published_at": s["publishedAt"],
            "is_reply": is_reply,
        }

    try:
        while True:
            res = youtube.commentThreads().list(
                part="snippet,replies", videoId=video_id, maxResults=100,
                textFormat="plainText", order="time", pageToken=page_token,
            ).execute()
            for item in res.get("items", []):
                top = item["snippet"]["topLevelComment"]
                rows.append(_row(top))
                reply_count = item["snippet"].get("totalReplyCount", 0)
                if 0 < reply_count <= 5 and "replies" in item:
                    # 5件以下の返信は snippet.replies に同梱されている
                    for r in item["replies"]["comments"]:
                        rows.append(_row(r, parent_id=top["id"], is_reply=True))
                elif reply_count > 5:
                    # 6件以上はページングして全件取得
                    reply_token = None
                    while True:
                        rres = youtube.comments().list(
                            part="snippet", parentId=top["id"], maxResults=100,
                            textFormat="plainText", pageToken=reply_token,
                        ).execute()
                        for r in rres.get("items", []):
                            rows.append(_row(r, parent_id=top["id"], is_reply=True))
                        reply_token = rres.get("nextPageToken")
                        if not reply_token:
                            break
            if max_comments and len(rows) >= max_comments:
                rows = rows[:max_comments]
                break
            page_token = res.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        print(f"[WARN] コメント取得エラー ({video_id}): {e}")

    df = pd.DataFrame(rows)
    if not df.empty:
        df["published_at"] = pd.to_datetime(df["published_at"]).dt.tz_convert("Asia/Tokyo")
        print(f"{video_id}: {len(df)}件取得 "
              f"(top-level: {(~df['is_reply']).sum()} / replies: {df['is_reply'].sum()})")
    return df


def clean_text(series: pd.Series) -> pd.Series:
    """改行・連続空白を除去する。"""
    return (
        series.astype(str).fillna("")
        .str.replace(r"[\r\n]+", " ", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )


# ============================================================
# 単語抽出（SINGLE_WORDS結合・ストップワード・類義語対応）
# ============================================================
class WordExtractor:
    """nagisa による名詞抽出 + 複合語(SINGLE_WORDS)の結合マッチ。

    - single_words に登録した語は、形態素に分割されても連結して1語として数える
    - 1文字語は single_words に明示登録したものだけ残す
    - synonym_map で表記ゆれを統一（例: かわい → 可愛い）
    """

    def __init__(self, single_words: list[str] | None = None,
                 stop_words: set[str] | None = None,
                 synonym_map: dict[str, str] | None = None,
                 postags: list[str] | None = None):
        import nagisa  # 遅延import

        self._nagisa = nagisa
        self.single_words = list(single_words or [])
        self.stop_words = set(stop_words or set())
        self.synonym_map = dict(synonym_map or {})
        self.postags = postags or ["名詞"]
        # 長いトークン列から優先マッチさせる
        self._single_seqs = sorted(
            ((w, self._tokenize(w)) for w in self.single_words),
            key=lambda x: len(x[1]), reverse=True,
        )

    @classmethod
    def from_preset(cls, name: str, **overrides):
        from .config import VOCAB_PRESETS

        p = dict(VOCAB_PRESETS[name])
        p.update(overrides)
        return cls(single_words=p.get("single_words"),
                   stop_words=p.get("stop_words"),
                   synonym_map=p.get("synonym_map"))

    def _tokenize(self, text: str) -> list[str]:
        return self._nagisa.extract(text, extract_postags=self.postags).words

    def extract(self, text: str, use_synonyms: bool = True) -> list[str]:
        tokens = self._tokenize(text)
        out: list[str] = []
        i = 0
        while i < len(tokens):
            matched = None
            for word, seq in self._single_seqs:
                n = len(seq)
                if n and tokens[i : i + n] == seq:
                    matched, i = word, i + n
                    break
            if matched is None:
                matched, i = tokens[i], i + 1
            if use_synonyms and matched in self.synonym_map:
                matched = self.synonym_map[matched]
            if matched in self.stop_words:
                continue
            if len(matched) > 1 or matched in self.single_words:
                out.append(matched)
        return out

    def count(self, texts, use_synonyms: bool = True) -> Counter:
        words: list[str] = []
        for t in texts:
            words.extend(self.extract(str(t), use_synonyms=use_synonyms))
        return Counter(words)


def word_freq_df(counts: Counter) -> pd.DataFrame:
    return pd.DataFrame(counts.most_common(), columns=["word", "count"])


def keyword_daily_counts(df: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    """特定キーワードの日別言及数（旧08の令和/平成分析の汎用化）。"""
    tmp = df.copy()
    tmp["date"] = tmp["published_at"].dt.date
    return (
        pd.DataFrame({kw: tmp["text"].str.contains(kw, na=False) for kw in keywords})
        .assign(date=tmp["date"].values)
        .groupby("date")
        .sum()
    )
