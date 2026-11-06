"""Pure helpers for yt.py: no network, no Google libraries.

SRT parsing, HTML-to-text, title normalization, chapters, links, timestamps.
Everything here is unit-tested offline (tests/test_validate.py).
"""
from __future__ import annotations

import difflib
import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------- timestamps

def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp that carries a timezone, return it in UTC.

    Python 3.10's fromisoformat() rejects a trailing 'Z', so swap it first.
    Naive timestamps are refused.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty timestamp")
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    # 3.10 only accepts 3 or 6 fractional digits; normalize anything else to 6.
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$", v)
    if m:
        frac = (m.group(2) + "000000")[:6]
        v = f"{m.group(1)}.{frac}{m.group(3)}"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return dt.astimezone(timezone.utc)


def iso_duration_seconds(value: str) -> float:
    """YouTube contentDetails.duration (e.g. PT55M27S, P0D, PT1H2M3.5S) to seconds."""
    if not value or value in ("P0D", "PT0S"):
        return 0.0
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$", value)
    if not m:
        raise ValueError(f"bad ISO duration: {value!r}")
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def fmt_hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600:d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

# ---------------------------------------------------------------------- SRT

_TS = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
_TIMING = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")


def _ts_to_s(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int((ms + "00")[:3]) / 1000


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str


@dataclass
class SrtReport:
    path: str
    cues: list[Cue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sha256: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def first_start(self) -> float:
        return self.cues[0].start if self.cues else 0.0

    @property
    def last_end(self) -> float:
        return self.cues[-1].end if self.cues else 0.0


def parse_srt_text(text: str) -> tuple[list[Cue], list[str]]:
    """Parse SRT text. Returns (cues, errors). Tolerates BOM and CRLF."""
    errors: list[str] = []
    cues: list[Cue] = []
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    for n, block in enumerate(blocks, 1):
        lines = block.split("\n")
        # index line is optional in practice; find the timing line
        ti = next((i for i, ln in enumerate(lines[:3]) if _TIMING.match(ln)), None)
        if ti is None:
            errors.append(f"block {n}: no timing line ({lines[0][:40]!r})")
            continue
        m = _TIMING.match(lines[ti])
        g = m.groups()
        start, end = _ts_to_s(*g[:4]), _ts_to_s(*g[4:])
        body = "\n".join(lines[ti + 1:]).strip()
        if end <= start:
            errors.append(f"cue {n}: ends at or before it starts ({lines[ti].strip()})")
        if not body:
            errors.append(f"cue {n}: empty text")
        cues.append(Cue(n, start, end, body))
    for a, b in zip(cues, cues[1:]):
        if b.start < a.start:
            errors.append(f"cue {b.index}: starts before cue {a.index} (out of order)")
    if not cues:
        errors.append("no cues found")
    return cues, errors


def parse_and_validate_srt(srt_path: Path | str, video_duration_sec: float | None = None,
                           grace_sec: float = 2.0) -> SrtReport:
    p = Path(srt_path)
    rep = SrtReport(path=str(p))
    try:
        raw = p.read_bytes()
    except OSError as e:
        rep.errors.append(f"can't read SRT: {e}")
        return rep
    rep.sha256 = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        rep.errors.append("SRT is not UTF-8")
        return rep
    rep.cues, errs = parse_srt_text(text)
    rep.errors.extend(errs)
    if video_duration_sec and rep.cues:
        if rep.last_end > video_duration_sec + grace_sec:
            rep.errors.append(
                f"last cue ends at {fmt_hms(rep.last_end)}, past the video ({fmt_hms(video_duration_sec)})")
    return rep


def caption_words(text: str) -> list[str]:
    """Words of an SRT (or plain text) for comparing two caption files loosely."""
    cues, _ = parse_srt_text(text)
    joined = " ".join(c.text for c in cues) if cues else text
    joined = html.unescape(re.sub(r"<[^>]+>", " ", joined))
    joined = normalize_text(joined).lower()
    return re.findall(r"[a-z0-9']+", joined)


def captions_match(srt_a: str, srt_b: str, threshold: float = 0.97) -> tuple[bool, float]:
    a, b = caption_words(srt_a), caption_words(srt_b)
    if not a or not b:
        return False, 0.0
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return ratio >= threshold, ratio

# -------------------------------------------------------------- normalizing

_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
                         "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2032": "'",
                         "\u00a0": " ", "\u2013": "-", "\u2014": "-"})


def normalize_text(s: str) -> str:
    s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s).translate(_QUOTES)
    return s


def normalize_title(s: str) -> str:
    s = normalize_text(s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def title_problems(title: str) -> list[str]:
    out = []
    if len(title) > 100:
        out.append(f"title is {len(title)} characters (YouTube max 100)")
    if "<" in title or ">" in title:
        out.append("title contains < or > (YouTube rejects these)")
    return out

# ----------------------------------------------------------- HTML to text

def html_to_youtube_text(description_html: str) -> str:
    """Convert Riverside's description HTML to the plain text YouTube shows.

    Riverside replaces each link with its href, so <a href="X">label</a> -> X.
    Paragraphs become blank-line separated blocks.
    """
    s = description_html.replace("\r\n", "\n")
    s = re.sub(r"<a\b[^>]*\bhref\s*=\s*\"([^\"]*)\"[^>]*>.*?</a>", r"\1", s, flags=re.I | re.S)
    s = re.sub(r"<a\b[^>]*\bhref\s*=\s*'([^']*)'[^>]*>.*?</a>", r"\1", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h\d)>", "\n\n", s, flags=re.I)
    s = re.sub(r"<li\b[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    lines = [ln.rstrip() for ln in s.split("\n")]
    s = "\n".join(lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def content_lines(text: str) -> list[str]:
    """Non-empty, whitespace-collapsed, normalized lines, for a blank-line-insensitive diff."""
    out = []
    for ln in normalize_text(text).replace("\r\n", "\n").split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            out.append(ln)
    return out


def description_diff(expected_text: str, actual_text: str) -> list[str]:
    a, b = content_lines(expected_text), content_lines(actual_text)
    if a == b:
        return []
    return list(difflib.unified_diff(a, b, "riverside (expected)", "youtube (actual)", lineterm="", n=1))

# ----------------------------------------------------------------- chapters

_CHAPTER = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b\s*(.*)$")


def parse_chapters(description_text: str) -> list[tuple[float, str]]:
    out = []
    for ln in description_text.split("\n"):
        m = _CHAPTER.match(ln)
        if m:
            h, mi, s, label = m.groups()
            out.append((int(h or 0) * 3600 + int(mi) * 60 + int(s), label.strip()))
    return out


def validate_chapters(description_text: str, video_duration_sec: float | None) -> tuple[int, list[str]]:
    """Returns (chapter_count, problems). YouTube's rules: first at 0:00, at least 3,
    ascending, each chapter at least 10 seconds (including the last)."""
    ch = parse_chapters(description_text)
    problems: list[str] = []
    if not ch:
        return 0, ["no chapters found"]
    if ch[0][0] != 0:
        problems.append(f"first chapter starts at {fmt_hms(ch[0][0])}, must be 0:00")
    if len(ch) < 3:
        problems.append(f"only {len(ch)} chapters, YouTube needs at least 3")
    for (t1, l1), (t2, l2) in zip(ch, ch[1:]):
        if t2 <= t1:
            problems.append(f"{fmt_hms(t2)} {l2!r} is not after {fmt_hms(t1)}")
        elif t2 - t1 < 10:
            problems.append(f"chapter {l1!r} is {t2 - t1:.0f}s long, minimum 10s")
    if video_duration_sec:
        last_t, last_l = ch[-1]
        if video_duration_sec - last_t < 10:
            problems.append(f"last chapter {last_l!r} is under 10s (video ends at {fmt_hms(video_duration_sec)})")
    return len(ch), problems

# -------------------------------------------------------------------- links

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def find_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?") for u in _URL.findall(text)]


def canon_url(u: str) -> str:
    p = urlparse(u.strip())
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}"


def validate_links(description_text: str, post_url: str | None, config: dict) -> list[str]:
    """Problems with the links in a YouTube description (empty list = pass)."""
    urls = find_urls(description_text)
    canon = {canon_url(u) for u in urls}
    problems: list[str] = []
    writeup = [post_url] if post_url else list(config.get("writeup_home_urls", []))
    if not any(canon_url(w) in canon for w in writeup):
        want = post_url or (writeup[0] if writeup else "(none configured)")
        problems.append(f"write-up link missing (want {want})")
    support = config.get("support_url")
    if support and canon_url(support) not in canon:
        problems.append(f"support link missing ({support})")
    for u in urls:
        for pat in config.get("flag_patterns", []):
            if re.search(pat, u, re.I):
                problems.append(f"sends viewers off-site to Riverside: {u}")
                break
    return problems

# --------------------------------------------------------------------- misc

def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


EN_LANG = re.compile(r"^en(-[A-Za-z]+)?$", re.I)


def pick_english_track(items: list[dict]) -> tuple[str, dict | None, list[dict]]:
    """Decide what `captions` should do with a captions.list result.

    Returns (action, track, english_tracks):
      ("update", track, ...)  exactly one standard English track
      ("insert", None, ...)   no standard English track (none, or only ASR)
      ("ambiguous", None, ...) two or more standard English tracks
    """
    english = [t for t in items if EN_LANG.match(t.get("snippet", {}).get("language", ""))]
    standard = [t for t in english if t["snippet"].get("trackKind", "").lower() == "standard"]
    if len(standard) == 1:
        return "update", standard[0], english
    if not standard:
        return "insert", None, english
    return "ambiguous", None, english
