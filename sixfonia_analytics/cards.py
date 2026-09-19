"""X投稿用カード画像の生成（旧12の4バリアントを統合）。

render_video_cards() 1本で、ヘッダー文言・配色・セクション構成を引数で切替える。
日本語折り返しは全角/半角幅とハッシュタグを考慮した wrap_jp を採用（旧12最新版）。
"""

from __future__ import annotations

import unicodedata
from io import BytesIO

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import requests
from PIL import Image

# 配色プリセット
CARD_THEMES = {
    "blue": {  # こさめさん系（旧12デフォルト）
        "bg": "#ffffff", "card": "#eaf4f8", "accent": "#2980b9",
        "text": "#1a1a2e", "sub_text": "#607080",
    },
    "pink": {  # LAN系（旧12 cell2）
        "bg": "#ffffff", "card": "#fce4ec", "accent": "#e91e63",
        "text": "#1a1a1a", "sub_text": "#555555",
    },
}


# ============================================================
# 日本語折り返し
# ============================================================
def _char_units(c: str) -> float:
    """文字の表示幅。全角=1.0、半角≒0.55 で近似。"""
    return 1.0 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 0.55


def _tokenize(text: str) -> list[str]:
    """折り返しの最小単位に分割。#/@始まりの語と英数字連続は割らない。"""
    tokens, i, n = [], 0, len(text)
    wordset = "_-.'"
    while i < n:
        c = text[i]
        if c in "#@":
            j = i + 1
            while j < n and not text[j].isspace():
                j += 1
            tokens.append(text[i:j])
            i = j
        elif c.isascii() and (c.isalnum() or c in wordset):
            j = i
            while j < n and text[j].isascii() and (text[j].isalnum() or text[j] in wordset):
                j += 1
            tokens.append(text[i:j])
            i = j
        else:
            tokens.append(c)
            i += 1
    return tokens


def wrap_jp(text: str, max_units: float) -> str:
    """全角換算幅 max_units で折り返す。英単語・ハッシュタグは途中で割らない。"""
    def uw(s):
        return sum(_char_units(c) for c in s)

    out, cur, used = [], "", 0.0
    for tok in _tokenize(text):
        if tok == "\n":
            out.append(cur)
            cur, used = "", 0.0
            continue
        if tok == " " and cur == "":
            continue
        w = uw(tok)
        if w > max_units and len(tok) > 1:
            for ch in tok:
                cw = _char_units(ch)
                if cur and used + cw > max_units:
                    out.append(cur)
                    cur, used = "", 0.0
                cur += ch
                used += cw
            continue
        if cur and used + w > max_units:
            out.append(cur)
            if tok == " ":
                cur, used = "", 0.0
            else:
                cur, used = tok, w
        else:
            cur += tok
            used += w
    if cur:
        out.append(cur)
    return "\n".join(s.rstrip() for s in out) if out else text


# ============================================================
# サムネイル取得
# ============================================================
def _crop_16x9(img: Image.Image) -> Image.Image:
    """4:3黒帯サムネを中央基準で16:9に切り出す。"""
    w, h = img.size
    target = 16 / 9
    if w / h > target:
        nw = round(h * target)
        x = (w - nw) // 2
        img = img.crop((x, 0, x + nw, h))
    elif w / h < target:
        nh = round(w / target)
        y = (h - nh) // 2
        img = img.crop((0, y, w, y + nh))
    return img


def load_thumb(video_id: str) -> np.ndarray:
    """取得できた最大解像度のサムネを16:9で返す。失敗時はダミー画像。"""
    urls = [
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/sddefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
    ]
    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                img = Image.open(BytesIO(res.content)).convert("RGB")
                if img.size[0] >= 120:  # 取得失敗時の小さなプレースホルダを除外
                    return np.array(_crop_16x9(img))
        except Exception:
            continue
    dummy = np.full((720, 1280, 3), 60, dtype=np.uint8)
    dummy[:, :, 0] = 100
    return dummy


# ============================================================
# カード画像生成本体
# ============================================================
def render_video_cards(*, header_title: str, header_sub: str,
                       sections: list[tuple[str, list[dict]]],
                       theme: str = "blue",
                       out_path: str = "video_cards.png",
                       fig_w: float = 9.0, card_h: float = 1.28,
                       title_fontsize: int = 15, out_dpi: int = 260) -> str:
    """サムネ+タイトルのカード一覧画像を生成する。

    sections: [(セクション名, [{"video_id":..., "title":...}, ...]), ...]
              空のセクションは自動でスキップ。
    theme   : CARD_THEMES のキー（"blue" / "pink"）
    """
    th = CARD_THEMES[theme]

    LEFT_MARGIN, RIGHT_MARGIN = 0.45, 0.45
    TOP_MARGIN, BOT_MARGIN = 0.30, 0.40
    HEADER_H = 1.25
    ROW_GAP, SECTION_GAP = 0.22, 0.45
    THUMB_PAD, THUMB_INSET = 0.12, 0.16
    TITLE_GAP, CARD_RIGHT_PAD = 0.32, 0.22
    LINE_SPACING, TEXT_VPAD = 1.2, 0.12

    live_sections = [(name, items) for name, items in sections if items]
    if not live_sections:
        raise ValueError("動画リストが空です。")

    thumb_h = card_h - 2 * THUMB_PAD
    thumb_w = thumb_h * 16 / 9

    title_x_inch = LEFT_MARGIN + THUMB_INSET + thumb_w + TITLE_GAP
    avail_inch = (fig_w - RIGHT_MARGIN - CARD_RIGHT_PAD) - title_x_inch
    max_units = avail_inch / (title_fontsize / 72.0)
    line_h = title_fontsize * LINE_SPACING / 72.0

    # 折り返し行数からカード高さを決定
    entries = []  # (section_idx, video, wrapped_text, card_height)
    for s_idx, (_, items) in enumerate(live_sections):
        for v in items:
            wt = wrap_jp(v["title"], max_units)
            n_lines = wt.count("\n") + 1
            ch = max(card_h, n_lines * line_h + 2 * TEXT_VPAD)
            entries.append((s_idx, v, wt, ch))

    content_h = sum(e[3] for e in entries)
    for i in range(1, len(entries)):
        content_h += SECTION_GAP if entries[i][0] != entries[i - 1][0] else ROW_GAP
    fig_h = TOP_MARGIN + HEADER_H + content_h + BOT_MARGIN

    fx = lambda inch: inch / fig_w
    fyb = lambda top, h: 1 - (top + h) / fig_h
    fyc = lambda top, h: 1 - (top + h / 2) / fig_h

    fig = plt.figure(figsize=(fig_w, fig_h))
    fig.patch.set_facecolor(th["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor(th["bg"])

    card_left = fx(LEFT_MARGIN)
    card_width = fx(fig_w - LEFT_MARGIN - RIGHT_MARGIN)

    ax.text(0.5, 1 - (TOP_MARGIN + 0.42) / fig_h, header_title, fontsize=22,
            fontweight="bold", color=th["text"], ha="center", va="center")
    ax.text(0.5, 1 - (TOP_MARGIN + 0.82) / fig_h, header_sub, fontsize=13,
            color=th["sub_text"], ha="center", va="center")
    line_y = 1 - (TOP_MARGIN + HEADER_H - 0.18) / fig_h
    ax.plot([card_left, card_left + card_width], [line_y, line_y],
            color=th["accent"], linewidth=2)

    title_x = fx(title_x_inch)
    t_left = fx(LEFT_MARGIN + THUMB_INSET)

    d = TOP_MARGIN + HEADER_H
    prev_s = None
    for s_idx, v, wt, ch in entries:
        if prev_s is not None:
            d += SECTION_GAP if s_idx != prev_s else ROW_GAP

        rect = patches.FancyBboxPatch(
            (card_left, fyb(d, ch)), card_width, ch / fig_h,
            boxstyle="round,pad=0.002,rounding_size=0.012",
            facecolor=th["card"], edgecolor=th["accent"], linewidth=0.8,
            transform=ax.transAxes, mutation_aspect=fig_h / fig_w,
        )
        ax.add_patch(rect)

        t_top = d + (ch - thumb_h) / 2
        t_ax = fig.add_axes([t_left, fyb(t_top, thumb_h), fx(thumb_w), thumb_h / fig_h])
        t_ax.imshow(load_thumb(v["video_id"]), interpolation="lanczos")
        t_ax.axis("off")

        ax.text(title_x, fyc(d, ch), wt, fontsize=title_fontsize, color=th["text"],
                va="center", ha="left", linespacing=LINE_SPACING)

        d += ch
        prev_s = s_idx

    plt.savefig(out_path, dpi=out_dpi, facecolor=th["bg"])
    plt.show()
    print(f"保存しました: {out_path}")
    return out_path
