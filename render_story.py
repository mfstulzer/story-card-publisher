#!/usr/bin/env python3
"""Render a tweet as a 1080x1920 Instagram story card (tweet-screenshot style).

Input: raw text (--text) or an x.com status URL (--url, fetched via X's free
no-auth oEmbed endpoint). Output: PNG in ~/Mind/03 Creative/story-cards/,
auto-opened unless --no-open. No X API keys anywhere.
"""
import argparse
import html
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

SKILL_DIR = Path(__file__).resolve().parent.parent
PROFILE_PATH = Path(__file__).parent / "profile.json"
def _first_existing(paths, env):
    import os
    if os.environ.get(env) and os.path.isfile(os.environ[env]):
        return os.environ[env]
    for p in paths:
        if os.path.isfile(p):
            return p
    return paths[-1]


# macOS first so local renders are unchanged; Linux fallbacks are for CI.
# Liberation Sans is the closest metric match to Helvetica available on Debian.
FONT_PATH = _first_existing([
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
], "STORY_FONT_PATH")
DEFAULT_OUT = Path.home() / "Mind" / "03 Creative" / "story-cards"

CANVAS = (1080, 1920)
AVATAR = 112
BG = (255, 255, 255)
INK = (15, 20, 25)
GRAY = (83, 100, 113)
MENU_GRAY = (150, 160, 168)
HAIRLINE = (207, 217, 222)
ACCENT = (29, 155, 240)
EMOJI_FONT_PATH = _first_existing([
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
], "STORY_EMOJI_FONT_PATH")

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U00002190-\U000021FF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF"
    "\U0000200D\U000023E9-\U000023FA\U000024C2\U00002122\U00002139]")
_ENTITY_RE = re.compile(r"(https?://\S+|[@#]\w+)")


def _parse_color(s):
    """Accept '#rrggbb' or 'rgb(r, g, b)'. Return an (r,g,b) tuple or None."""
    s = (s or "").strip()
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", s)
    if m:
        return tuple(int(x) for x in m.groups())
    s = s.lstrip("#")
    if len(s) == 6:
        try:
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None
    return None


def _font(size, bold=False):
    """Helvetica.ttc is a collection; scan indexes for the Regular/Bold face."""
    want = "Bold" if bold else "Regular"
    for i in range(8):
        try:
            f = ImageFont.truetype(FONT_PATH, size, index=i)
        except OSError:
            break
        if f.getname()[1] == want:
            return f
    return ImageFont.truetype(FONT_PATH, size)


def wrap_text(draw, text, font, max_width, emoji_px=0):
    """Greedy word wrap by rendered width. Preserves blank lines. Emoji count as
    ~emoji_px wide when emoji_px is given."""
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if not cur or _measure(draw, trial, font, emoji_px) <= max_width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def fetch_oembed(url):
    q = urllib.parse.urlencode({"url": url, "omit_script": "true", "dnt": "true"})
    req = urllib.request.Request(
        f"https://publish.twitter.com/oembed?{q}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def parse_oembed_html(oembed):
    """Extract (tweet_text, author_name, handle) from an oEmbed payload."""
    raw = oembed.get("html", "")
    m = re.search(r"<p[^>]*>(.*?)</p>", raw, re.S)
    text = m.group(1) if m else ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).strip()
    author = oembed.get("author_name", "")
    hm = re.search(r"(?:twitter|x)\.com/(\w+)", oembed.get("author_url", ""))
    handle = hm.group(1) if hm else ""
    return text, author, handle


def load_profile():
    with open(PROFILE_PATH) as f:
        p = json.load(f)
    p["avatar"] = str(Path(p.get("avatar", "")).expanduser())
    p["accent_rgb"] = _parse_color(p.get("accent")) or ACCENT
    return p


def make_avatar(profile, size=AVATAR):
    """Circular avatar from profile['avatar'], or an initials circle fallback."""
    path = Path(profile["avatar"])
    if path.is_file():
        src = Image.open(path).convert("RGBA").resize((size, size))
    else:
        src = Image.new("RGBA", (size, size), ACCENT + (255,))
        d = ImageDraw.Draw(src)
        initials = "".join(w[0] for w in profile["display_name"].split()[:2]).upper()
        d.text((size / 2, size / 2), initials, font=_font(size // 2, bold=True),
               fill="white", anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size))
    out.paste(src, (0, 0), mask)
    return out


_EMOJI_FONT = None


def _emoji_font():
    global _EMOJI_FONT
    if _EMOJI_FONT is not None:
        return _EMOJI_FONT
    for s in (160, 137, 96, 48, 40, 20):
        try:
            _EMOJI_FONT = (ImageFont.truetype(EMOJI_FONT_PATH, s), s)
            return _EMOJI_FONT
        except OSError:
            continue
    _EMOJI_FONT = (None, None)
    return _EMOJI_FONT


def _runs(s):
    """Split a string into (is_emoji, substring) runs."""
    runs, cur, buf = [], None, ""
    for ch in s:
        e = bool(_EMOJI_RE.match(ch))
        if cur is None:
            cur, buf = e, ch
        elif e == cur:
            buf += ch
        else:
            runs.append((cur, buf))
            cur, buf = e, ch
    if cur is not None:
        runs.append((cur, buf))
    return runs


def _split_clusters(t):
    """Split an emoji run into grapheme clusters (joins ZWJ + variation selectors)."""
    clusters, cur = [], ""
    for ch in t:
        if ch in ("️", "‍") or cur.endswith("‍"):
            cur += ch
            continue
        if cur:
            clusters.append(cur)
        cur = ch
    if cur:
        clusters.append(cur)
    return clusters


def _measure(draw, s, font, emoji_px):
    w = 0
    for is_e, t in _runs(s):
        if is_e:
            w += len(_split_clusters(t)) * emoji_px if emoji_px else 0
        else:
            w += draw.textlength(t, font=font)
    return w


def _emoji_image(cluster, target_px):
    ef, strike = _emoji_font()
    if not ef:
        return None
    tmp = Image.new("RGBA", (strike * 3, strike * 2), (0, 0, 0, 0))
    try:
        ImageDraw.Draw(tmp).text((0, 0), cluster, font=ef, embedded_color=True)
    except Exception:
        return None
    bbox = tmp.getbbox()
    if not bbox:
        return None
    glyph = tmp.crop(bbox)
    scale = target_px / glyph.height
    return glyph.resize((max(1, int(glyph.width * scale)),
                         max(1, int(glyph.height * scale))), Image.LANCZOS)


def _draw_rich_line(img, draw, x, y, line, font, emoji_px, accent):
    """Draw one line: plain text in INK, @mentions/#tags/links in accent, color emoji."""
    cx = x
    ascent = font.getmetrics()[0]
    for is_e, t in _runs(line):
        if is_e:
            for cluster in _split_clusters(t):
                im = _emoji_image(cluster, emoji_px)
                if im:
                    ey = int(y + (ascent - im.height) / 2 + emoji_px * 0.12)
                    img.paste(im, (int(cx), ey), im)
                    cx += im.width + int(emoji_px * 0.06)
                else:
                    cx += emoji_px
        else:
            pos = 0
            for mo in _ENTITY_RE.finditer(t):
                pre = t[pos:mo.start()]
                if pre:
                    draw.text((cx, y), pre, font=font, fill=INK)
                    cx += draw.textlength(pre, font=font)
                ent = mo.group(0)
                draw.text((cx, y), ent, font=font, fill=accent)
                cx += draw.textlength(ent, font=font)
                pos = mo.end()
            rest = t[pos:]
            if rest:
                draw.text((cx, y), rest, font=font, fill=INK)
                cx += draw.textlength(rest, font=font)


def _now_stamp():
    now = datetime.now()
    hour = now.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{now.strftime('%M %p')} · {now.strftime('%b')} {now.day}, {now.year}"


# Subtle off-white page so the white tweet card floats.
PAGE_TOP = (245, 244, 241)
PAGE_BOTTOM = (235, 233, 228)


def _page_bg():
    """Whisper-soft off-white vertical gradient."""
    img = Image.new("RGB", CANVAS, PAGE_TOP)
    d = ImageDraw.Draw(img)
    for y in range(CANVAS[1]):
        t = y / CANVAS[1]
        row = tuple(int(PAGE_TOP[c] + (PAGE_BOTTOM[c] - PAGE_TOP[c]) * t) for c in range(3))
        d.line([(0, y), (CANVAS[0], y)], fill=row)
    return img


def render_card(text, profile, out_dir, open_after=True):
    """Native X light-mode tweet floating as a white card on the brand-dark canvas."""
    accent = profile.get("accent_rgb", ACCENT)
    card_w, pad, av_sz = 920, 56, 96
    inner_w = card_w - 2 * pad
    name_font = _font(40, bold=True)
    handle_font = _font(34)
    ts_font = _font(32)
    gap1, gap2 = 36, 30

    # Step the body font down until the card fits comfortably on the canvas.
    # Line height matches native X (~1.28); blank lines between paragraphs are a
    # smaller gap, not a full empty line, so the card is not tall and airy.
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    for size in (48, 44, 40, 36, 32):
        body_font = _font(size)
        emoji_px = int(size * 1.05)
        lines = wrap_text(probe, text, body_font, inner_w, emoji_px)
        line_h = int(size * 1.28)
        para_gap = int(size * 0.62)
        body_h = sum(line_h if ln else para_gap for ln in lines)
        card_h = pad + av_sz + gap1 + body_h + gap2 + ts_font.size + 12 + pad
        if card_h <= 1680:
            break

    img = _page_bg().convert("RGBA")
    x0 = (CANVAS[0] - card_w) // 2
    y0 = max((CANVAS[1] - card_h) // 2, 90)

    # soft neutral drop shadow so the card floats on the off-white page
    shadow = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (x0 - 4, y0 + 18, x0 + card_w + 4, y0 + card_h + 30),
        radius=48, fill=(60, 62, 74, 70))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(28)))
    draw = ImageDraw.Draw(img)

    # white tweet card
    draw.rounded_rectangle((x0, y0, x0 + card_w, y0 + card_h), radius=40, fill=BG)

    # header
    hx, hy = x0 + pad, y0 + pad
    av = make_avatar(profile, av_sz)
    img.paste(av, (hx, hy), av)
    tx = hx + av_sz + 22
    draw.text((tx, hy + 6), profile["display_name"], font=name_font, fill=INK)
    draw.text((tx, hy + 52), "@" + profile["handle"], font=handle_font, fill=GRAY)
    for i in range(3):  # horizontal "..." menu, top-right
        cxd = x0 + card_w - pad - (2 - i) * 20
        draw.ellipse((cxd - 4, hy + 10, cxd + 4, hy + 18), fill=MENU_GRAY)

    by = hy + av_sz + gap1
    for ln in lines:
        if ln:
            _draw_rich_line(img, draw, hx, by, ln, body_font, emoji_px, accent)
            by += line_h
        else:
            by += para_gap

    ts = profile.get("timestamp") or _now_stamp()
    draw.text((hx, by + gap2), ts, font=ts_font, fill=GRAY)

    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"story-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png"
    img.convert("RGB").save(out)
    if open_after:
        subprocess.run(["open", str(out)], check=False)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="raw tweet text")
    g.add_argument("--url", help="x.com/twitter.com status URL")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    profile = load_profile()
    text = a.text
    if a.url:
        try:
            text, author, handle = parse_oembed_html(fetch_oembed(a.url))
        except Exception as e:
            sys.exit(f"oEmbed fetch failed ({e}). Paste the tweet text with --text instead.")
        # keep the branded display name from profile.json ("Mark Stulzer");
        # only the handle is taken live from the tweet
        if handle:
            profile["handle"] = handle
    if not text or not text.strip():
        sys.exit("No tweet text found.")

    out = render_card(text.strip(), profile, a.out, open_after=not a.no_open)
    print(out)


if __name__ == "__main__":
    main()
