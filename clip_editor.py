"""
clip_editor.py — turns a stream clip into a TikTok-ready "white background" video:

    ┌──────────────────────┐
    │                      │  white 1080x1920 canvas
    │  BIG BOLD CAPTION    │  <- auto-generated hook caption (black text)
    │  ON TOP OF THE CLIP  │
    │ ┌──────────────────┐ │
    │ │                  │ │
    │ │    the clip      │ │  <- original 16:9 clip, full width, centered
    │ │                  │ │
    │ └──────────────────┘ │
    │      @streamer       │  <- small gray credit
    │                      │
    └──────────────────────┘

Usage (CLI):
    python clip_editor.py input.mp4 output.mp4 --streamer xqc --title "he did NOT see that coming"
    python clip_editor.py input.mp4 output.mp4 --streamer xqc --caption "Your own caption"

Usage (from your app):
    from clip_editor import make_white_bg_video, generate_caption
    caption = generate_caption("input.mp4", clip_title="omg", streamer="xqc")
    make_white_bg_video("input.mp4", "output.mp4", caption, streamer="xqc")

Caption modes (env CAPTION_MODE):
    "ai"    -> transcribes the clip's audio and writes a hook caption with OpenAI
               (needs OPENAI_API_KEY). Falls back to "title" on any error.
    "title" -> cleans up the Twitch clip title (free, no API).

Needs: ffmpeg on the machine, Pillow (pip install pillow). No other packages.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import uuid

from PIL import Image, ImageDraw, ImageFont

# ---------- layout settings (tweak to taste) ----------
W, H = 1080, 1920            # TikTok 9:16
BG_COLOR = "white"
SIDE_MARGIN = 0              # px of white on each side of the clip (0 = full width)
CAPTION_MAX_WIDTH = 960      # caption wraps inside this width
CAPTION_FONT_SIZE = 68       # starting size; shrinks automatically for long captions
CAPTION_MIN_FONT = 44
CAPTION_MAX_LINES = 4
CAPTION_GAP = 40             # space between caption and clip
CREDIT_FONT_SIZE = 38
CREDIT_COLOR = (120, 120, 120)
TEXT_COLOR = (0, 0, 0)

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.environ.get("CAPTION_FONT", os.path.join(HERE, "fonts", "Poppins-Bold.ttf"))


# ======================================================================
#  Caption generation
# ======================================================================

def _clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "")).strip()
    t = re.sub(r"(?i)\b(clip|lol|lmao)\b$", "", t).strip()
    return t[:1].upper() + t[1:] if t else "Wait for it..."


def _openai_request(url, headers, body, is_multipart=False, timeout=120):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _transcribe(video_path: str, api_key: str) -> str:
    """Extract audio with ffmpeg and send it to OpenAI's transcription endpoint."""
    with tempfile.TemporaryDirectory() as td:
        audio = os.path.join(td, "a.mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
             "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", audio],
            check=True,
        )
        with open(audio, "rb") as f:
            audio_bytes = f.read()

    boundary = uuid.uuid4().hex
    model = os.environ.get("TRANSCRIBE_MODEL", "whisper-1")
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n'
         f"Content-Type: audio/mpeg\r\n\r\n").encode() + audio_bytes + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    data = _openai_request(
        "https://api.openai.com/v1/audio/transcriptions",
        {"Authorization": f"Bearer {api_key}",
         "Content-Type": f"multipart/form-data; boundary={boundary}"},
        b"".join(parts),
    )
    return data.get("text", "").strip()


def _write_hook(transcript: str, clip_title: str, streamer: str, api_key: str) -> str:
    model = os.environ.get("CAPTION_MODEL", "gpt-4o-mini")
    prompt = (
        "You write the caption that sits on top of a TikTok stream clip "
        "(white background meme format). Write ONE short hook, max 12 words, "
        "that makes people watch to the end. Plain text, no hashtags, no quotes, "
        "no emojis, don't spoil the ending.\n\n"
        f"Streamer: {streamer or 'unknown'}\n"
        f"Twitch clip title: {clip_title or '(none)'}\n"
        f"What was said in the clip: {transcript[:2000] or '(no speech)'}"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 60,
        "temperature": 0.9,
    }).encode()
    data = _openai_request(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body,
    )
    text = data["choices"][0]["message"]["content"].strip().strip('"').strip()
    return text


def generate_caption(video_path: str, clip_title: str = "", streamer: str = "") -> str:
    mode = os.environ.get("CAPTION_MODE", "ai").lower()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if mode == "ai" and api_key:
        try:
            transcript = _transcribe(video_path, api_key)
            caption = _write_hook(transcript, clip_title, streamer, api_key)
            if caption:
                return caption
        except Exception as e:  # never let captioning kill a post
            print(f"[clip_editor] AI caption failed, using title: {e}")
    return _clean_title(clip_title)


# ======================================================================
#  Rendering
# ======================================================================

def _probe_size(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", path],
        check=True, capture_output=True, text=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_caption(text):
    """Pick the biggest font size where the caption fits in CAPTION_MAX_LINES lines."""
    dummy = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    size = CAPTION_FONT_SIZE
    while True:
        font = ImageFont.truetype(FONT_PATH, size)
        lines = _wrap(dummy, text, font, CAPTION_MAX_WIDTH)
        if len(lines) <= CAPTION_MAX_LINES or size <= CAPTION_MIN_FONT:
            return font, lines[:CAPTION_MAX_LINES]
        size -= 4


def _build_overlay(caption, streamer, clip_w, clip_h, out_png):
    """Draw the white canvas + caption + credit as one PNG. Returns clip (x, y)."""
    font, lines = _fit_caption(caption)
    line_h = int(font.size * 1.22)
    caption_h = line_h * len(lines)

    credit_font = ImageFont.truetype(FONT_PATH, CREDIT_FONT_SIZE)
    credit = f"@{streamer.lstrip('@')}" if streamer else ""
    credit_h = (CREDIT_FONT_SIZE + 24) if credit else 0

    # Center the whole block (caption + gap + clip + credit) vertically.
    block_h = caption_h + CAPTION_GAP + clip_h + credit_h
    top = max(80, (H - block_h) // 2)
    clip_x = (W - clip_w) // 2
    clip_y = top + caption_h + CAPTION_GAP

    img = Image.new("RGB", (W, H), BG_COLOR)
    d = ImageDraw.Draw(img)
    y = top
    for line in lines:
        lw = d.textlength(line, font=font)
        d.text(((W - lw) / 2, y), line, font=font, fill=TEXT_COLOR)
        y += line_h
    if credit:
        cw = d.textlength(credit, font=credit_font)
        d.text(((W - cw) / 2, clip_y + clip_h + 20), credit, font=credit_font, fill=CREDIT_COLOR)
    img.save(out_png)
    return clip_x, clip_y


_KEEP = set("\u2018\u2019\u201c\u201d\u2026\u2013\u2014")


def _printable(text):
    """Drop emoji and other symbols the caption font can't draw (they'd show up as boxes)."""
    out = "".join(ch for ch in text if ord(ch) < 0x2000 or ch in _KEEP)
    return re.sub(r"\s+", " ", out).strip()


def make_white_bg_video(input_path, output_path, caption, streamer=""):
    caption = _printable(caption) or "Wait for it..."
    src_w, src_h = _probe_size(input_path)
    clip_w = W - 2 * SIDE_MARGIN
    clip_h = int(round(src_h * clip_w / src_w / 2) * 2)  # keep aspect, even number
    # very tall sources (already vertical) — cap the height so the caption still fits
    max_clip_h = H - 520
    if clip_h > max_clip_h:
        clip_h = max_clip_h
        clip_w = int(round(src_w * clip_h / src_h / 2) * 2)

    with tempfile.TemporaryDirectory() as td:
        bg = os.path.join(td, "bg.png")
        x, y = _build_overlay(caption, streamer, clip_w, clip_h, bg)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-i", bg,
            "-i", input_path,
            "-filter_complex",
            f"[1:v]scale={clip_w}:{clip_h}:flags=lanczos,setsar=1[v];"
            f"[0:v][v]overlay={x}:{y}:shortest=1,format=yuv420p,fps=30[out]",
            "-map", "[out]", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            output_path,
        ]
        subprocess.run(cmd, check=True)
    return output_path


def edit_clip(input_path, output_path, clip_title="", streamer="", caption=None):
    """One call: generate caption (unless given) and render. Returns the caption used."""
    caption = caption or generate_caption(input_path, clip_title, streamer)
    make_white_bg_video(input_path, output_path, caption, streamer)
    return caption


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--streamer", default="")
    p.add_argument("--title", default="", help="Twitch clip title (used for the caption)")
    p.add_argument("--caption", default=None, help="Skip auto-caption and use this text")
    a = p.parse_args()
    used = edit_clip(a.input, a.output, a.title, a.streamer, a.caption)
    print(f"Done -> {a.output}\nCaption: {used}")
