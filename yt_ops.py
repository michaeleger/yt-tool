"""The yt.py commands: find, status, captions, check. (link is deferred, spec 5.3.)"""
from __future__ import annotations

import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yt_validate as v
from yt_api import TOOL_DIR, CheckError, YtError, execute, log, EXIT_OK, EXIT_CHECK

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone.utc

EP_DIR = TOOL_DIR / "episodes"
VIDEO_PARTS = "snippet,status,contentDetails,processingDetails"
REQUIRED_EP = ("slug", "riverside_episode_id", "title", "description_html",
               "publication_date", "srt_path", "post_url")

# ------------------------------------------------------------ episode/state

def load_episode(path: str | Path) -> dict:
    p = Path(path).expanduser()
    try:
        ep = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise YtError(f"can't read episode file {p}: {e}") from None
    missing = [k for k in REQUIRED_EP if k not in ep]
    if missing:
        raise YtError(f"episode file missing: {', '.join(missing)}")
    if not re.fullmatch(r"[a-z0-9-]+", ep["slug"] or ""):
        raise YtError("episode slug must be lowercase letters, digits and hyphens")
    for prob in v.title_problems(ep["title"]):
        print(f"[WARN] {prob}")
    try:
        v.parse_iso_utc(ep["publication_date"])
    except ValueError as e:
        raise YtError(f"publication_date: {e}") from None
    pu = ep.get("post_url")
    if pu and not re.match(r"^https://(www\.)?eagertobehealthy\.com/", pu):
        raise YtError("post_url must be on https://www.eagertobehealthy.com/")
    ep["_path"] = str(p)
    return ep


def state_path(slug: str) -> Path:
    return EP_DIR / f"{slug}.json"


def load_state(slug: str) -> dict:
    p = state_path(slug)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"slug": slug, "channel_id": None, "video_id": None, "matched_title": None,
            "caption_track_id": None, "caption_track_kind": None, "caption_last_updated": None,
            "riverside_caption_backup_path": None, "srt_sha256_uploaded": None,
            "post_url_linked": None, "history": []}


def save_state(state: dict, command: str, units: int, summary: str) -> None:
    EP_DIR.mkdir(exist_ok=True)
    state["history"].append({"timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "command": command, "quota_units_est": units, "summary": summary})
    p = state_path(state["slug"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    log.info("state %s: %s", state["slug"], summary)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

# ------------------------------------------------------------------ helpers

def get_video(youtube: Any, video_id: str) -> dict:
    res = execute(youtube.videos().list(part=VIDEO_PARTS, id=video_id), 1, "videos.list")
    items = res.get("items", [])
    if not items:
        raise CheckError(f"video {video_id} not found on this channel (or not visible to this token)")
    return items[0]


def video_summary(vid: dict) -> dict:
    st, cd = vid.get("status", {}), vid.get("contentDetails", {})
    pd = vid.get("processingDetails", {})
    dur = v.iso_duration_seconds(cd.get("duration", "PT0S"))
    return {"id": vid["id"], "title": vid["snippet"]["title"], "privacy": st.get("privacyStatus"),
            "publishAt": st.get("publishAt"), "duration": dur,
            "processing": pd.get("processingStatus", "unknown")}


def fmt_when(iso: str | None) -> str:
    if not iso:
        return "(none)"
    dt = v.parse_iso_utc(iso)
    return f"{dt.strftime('%Y-%m-%d %H:%M UTC')} = {dt.astimezone(ET).strftime('%a %b %d %I:%M %p ET')}"


def require_video_id(state: dict, override: str | None) -> str:
    vid = override or state.get("video_id")
    if not vid:
        raise CheckError("no video ID yet. Run: yt.py find --episode <file> --apply (or pass --video-id)")
    return vid


def list_captions(youtube: Any, video_id: str) -> list[dict]:
    return execute(youtube.captions().list(part="snippet", videoId=video_id), 50, "captions.list").get("items", [])


def print_tracks(tracks: list[dict]) -> None:
    if not tracks:
        print("  (no English tracks)")
    for t in tracks:
        sn = t["snippet"]
        print(f"  {t['id']}  lang={sn.get('language')}  kind={sn.get('trackKind')}  "
              f"name={sn.get('name')!r}  status={sn.get('status')}  draft={sn.get('isDraft')}  "
              f"lastUpdated={sn.get('lastUpdated')}")


def download_caption(youtube: Any, track_id: str) -> str:
    data = execute(youtube.captions().download_media(id=track_id, tfmt="srt"), 200, "captions.download")
    if isinstance(data, bytes):
        return data.decode("utf-8-sig", errors="replace")
    return str(data)

# ---------------------------------------------------------------------- find

def cmd_find(youtube: Any, ch: dict, ep: dict, package_dir: str | None,
             video_id_override: str | None, apply: bool) -> int:
    state = load_state(ep["slug"])
    if video_id_override:
        vids = [get_video(youtube, video_id_override)]
        matches = vids
    else:
        res = execute(youtube.playlistItems().list(part="contentDetails", playlistId=ch["uploads"],
                                                   maxResults=50), 1, "playlistItems.list")
        ids = [it["contentDetails"]["videoId"] for it in res.get("items", [])]
        if not ids:
            raise CheckError("the channel's uploads list is empty")
        vids = execute(youtube.videos().list(part=VIDEO_PARTS, id=",".join(ids)), 1,
                       "videos.list").get("items", [])
        want = v.normalize_title(ep["title"])
        matches = [x for x in vids if v.normalize_title(x["snippet"]["title"]) == want]
    if len(matches) != 1:
        print(f"[FLAG] {len(matches)} videos match the episode title. Candidates (newest uploads):")
        for x in vids[:10]:
            s = video_summary(x)
            print(f"  {s['id']}  {s['privacy']:<8} publishAt={s['publishAt'] or '-'}  {s['title']}")
        print("Pass --video-id <ID> if the right one is listed.")
        return EXIT_CHECK
    s = video_summary(matches[0])
    print(f"Video:      {s['id']}  https://youtu.be/{s['id']}")
    print(f"Title:      {s['title']}")
    print(f"Privacy:    {s['privacy']}")
    print(f"Publish at: {fmt_when(s['publishAt'])}")
    print(f"Duration:   {v.fmt_hms(s['duration'])}   processing: {s['processing']}")

    pkg_change = None
    if package_dir:
        idx = Path(package_dir).expanduser() / "index.html"
        if not idx.exists():
            raise YtError(f"no index.html in {package_dir}")
        old = idx.read_text(encoding="utf-8")
        n = old.count("YOUTUBE_ID_HERE")
        if n == 0:
            already = old.count(s["id"])
            print(f"Package: no YOUTUBE_ID_HERE in {idx} ({already} mention(s) of {s['id']} already there)")
        else:
            new = old.replace("YOUTUBE_ID_HERE", s["id"])
            for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), str(idx), str(idx) + " (new)",
                                           lineterm="", n=0):
                print(f"  {ln}")
            pkg_change = (idx, new, n)

    if state.get("video_id") and state["video_id"] != s["id"]:
        print(f"[WARN] state file had a different video ID ({state['video_id']}); --apply will replace it")
    if not apply:
        print("\nDry run. Nothing written. Add --apply to save the video ID"
              + (" and update the package." if pkg_change else "."))
        return EXIT_OK
    state.update({"channel_id": ch["id"], "video_id": s["id"], "matched_title": s["title"]})
    if pkg_change:
        _atomic_write_text(pkg_change[0], pkg_change[1])
        print(f"Package updated: {pkg_change[2]} placeholder(s) replaced in {pkg_change[0]}")
    save_state(state, "find --apply", 3, f"matched {s['id']}" + (f"; package {pkg_change[0]}" if pkg_change else ""))
    print(f"Saved {state_path(ep['slug'])}")
    return EXIT_OK

# -------------------------------------------------------------------- status

def cmd_status(youtube: Any, ep: dict, video_id_override: str | None) -> int:
    state = load_state(ep["slug"])
    vid_id = require_video_id(state, video_id_override)
    s = video_summary(get_video(youtube, vid_id))
    flags = 0

    def line(ok: bool, text: str) -> None:
        nonlocal flags
        flags += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FLAG'}] {text}")

    pub = v.parse_iso_utc(ep["publication_date"])
    now = datetime.now(timezone.utc)
    print(f"Video {vid_id}: {s['title']}")
    if now < pub:
        line(s["privacy"] == "private", f"privacy is {s['privacy']} (should be private until go-live)")
        if s["publishAt"]:
            diff = abs((v.parse_iso_utc(s["publishAt"]) - pub).total_seconds())
            line(diff <= 60, f"publishAt {fmt_when(s['publishAt'])} vs episode {fmt_when(ep['publication_date'])}")
        else:
            line(False, "no publishAt: the video is not scheduled")
    else:
        line(s["privacy"] == "public", f"privacy is {s['privacy']} (go-live time has passed)")
    line(s["duration"] > 0, f"processing done (duration {v.fmt_hms(s['duration'])}, status {s['processing']})")

    srt = v.parse_and_validate_srt(ep["srt_path"], s["duration"] or None)
    if srt.cues and s["duration"]:
        gap = abs(s["duration"] - srt.last_end)
        line(gap <= 5.0, f"video {v.fmt_hms(s['duration'])} vs last caption {v.fmt_hms(srt.last_end)} "
                         f"({gap:.1f}s apart)")
    for e in srt.errors:
        line(False, f"SRT: {e}")

    # caption guard
    if s["duration"]:
        action, track, english = v.pick_english_track(list_captions(youtube, vid_id))
        if not state.get("caption_track_id"):
            print("[INFO] no caption upload recorded yet for this episode. Run: yt.py captions")
        elif not track or track["id"] != state["caption_track_id"]:
            line(False, "the English caption track we uploaded is gone or another one replaced it. "
                        "Run: yt.py captions --episode ... --apply")
        else:
            same = track["snippet"].get("lastUpdated") == state.get("caption_last_updated")
            line(same, "our captions are still the active English track" if same else
                 f"captions changed since we uploaded them (lastUpdated {track['snippet'].get('lastUpdated')}). "
                 "Riverside may have re-pushed. Run: yt.py captions --episode ... --apply")
    print(f"\n{'All checks passed.' if not flags else f'{flags} flag(s).'}")
    return EXIT_OK if not flags else EXIT_CHECK

# ------------------------------------------------------------------ captions

def cmd_captions(youtube: Any, ep: dict, video_id_override: str | None, apply: bool,
                 force: bool, adopt: bool) -> int:
    state = load_state(ep["slug"])
    vid_id = require_video_id(state, video_id_override)
    s = video_summary(get_video(youtube, vid_id))
    if s["duration"] <= 0:
        raise CheckError("YouTube is still processing this video (duration 0). Try again later.")

    srt = v.parse_and_validate_srt(ep["srt_path"], s["duration"])
    print(f"Our SRT: {srt.path}")
    print(f"  {len(srt.cues)} cues, {v.fmt_hms(srt.first_start)} to {v.fmt_hms(srt.last_end)}; "
          f"video is {v.fmt_hms(s['duration'])}")
    for e in srt.errors:
        print(f"  [FLAG] {e}")
    if not srt.ok:
        return EXIT_CHECK

    items = list_captions(youtube, vid_id)
    action, track, english = v.pick_english_track(items)
    print("English tracks on YouTube:")
    print_tracks(english)
    if action == "ambiguous":
        print("[FLAG] more than one uploaded English track. Look in Studio > Subtitles and remove the extra one.")
        return EXIT_CHECK
    if track and track["snippet"].get("status") == "syncing":
        print("[FLAG] that track is still syncing on YouTube's side. Try again in a few minutes.")
        return EXIT_CHECK

    # --adopt: record a track that already carries our text (uploaded by hand) without re-uploading
    if adopt:
        if not track:
            print("[FLAG] nothing to adopt: no uploaded English track.")
            return EXIT_CHECK
        remote = download_caption(youtube, track["id"])
        local = Path(srt.path).read_text(encoding="utf-8-sig")
        ok, ratio = v.captions_match(local, remote)
        print(f"Track {track['id']} vs our SRT: {ratio:.1%} word match")
        if not ok:
            print("[FLAG] the track on YouTube is not our SRT. Upload it instead: yt.py captions --apply")
            return EXIT_CHECK
        if not apply:
            print("\nDry run. Add --apply to record this track as ours (no upload).")
            return EXIT_OK
        state.update({"video_id": vid_id, "caption_track_id": track["id"],
                      "caption_track_kind": track["snippet"].get("trackKind"),
                      "caption_last_updated": track["snippet"].get("lastUpdated"),
                      "srt_sha256_uploaded": srt.sha256})
        save_state(state, "captions --adopt --apply", 251, f"adopted track {track['id']} ({ratio:.1%} match)")
        print("Recorded. `status` will now guard this track.")
        return EXIT_OK

    if (not force and track and state.get("srt_sha256_uploaded") == srt.sha256
            and state.get("caption_track_id") == track["id"]
            and state.get("caption_last_updated") == track["snippet"].get("lastUpdated")):
        print("Our SRT is already the active English track (same file, track unchanged). Nothing to do.")
        return EXIT_OK

    backup = EP_DIR / f"{ep['slug']}-riverside-captions.srt"
    if action == "update":
        plan = f"UPDATE track {track['id']} in place with our SRT"
        if backup.exists() and backup.stat().st_size > 0:
            plan += f" (backup already at {backup})"
        else:
            plan += f" after backing it up to {backup}"
    else:
        plan = "INSERT a new English (en-US) track with our SRT (no uploaded English track to replace; ASR tracks can't be overwritten)"
    print(f"\nPlan: {plan}")
    if not apply:
        print("Dry run. Nothing changed. Add --apply to do it.")
        return EXIT_OK

    before = track["snippet"].get("lastUpdated") if track else None
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(srt.path, mimetype="application/octet-stream", resumable=False)
    units = 50 + 1
    if action == "update":
        if not (backup.exists() and backup.stat().st_size > 0):
            EP_DIR.mkdir(exist_ok=True)
            _atomic_write_text(backup, download_caption(youtube, track["id"]))
            units += 200
            print(f"Backed up current track to {backup}")
            state["riverside_caption_backup_path"] = str(backup)
        execute(youtube.captions().update(part="snippet", body={"id": track["id"], "snippet": {"isDraft": False}},
                                          media_body=media), 450, "captions.update")
        units += 450
    else:
        execute(youtube.captions().insert(part="snippet", body={"snippet": {
            "videoId": vid_id, "language": "en-US", "name": "", "isDraft": False}}, media_body=media),
            400, "captions.insert")
        units += 400

    action2, track2, english2 = v.pick_english_track(list_captions(youtube, vid_id))
    units += 50
    if not track2:
        print("[FLAG] upload returned OK but no uploaded English track is listed. Check Studio.")
        return EXIT_CHECK
    moved = track2["snippet"].get("lastUpdated") != before
    print("After upload:")
    print_tracks(english2)
    state.update({"video_id": vid_id, "caption_track_id": track2["id"],
                  "caption_track_kind": track2["snippet"].get("trackKind"),
                  "caption_last_updated": track2["snippet"].get("lastUpdated"),
                  "srt_sha256_uploaded": srt.sha256})
    save_state(state, "captions --apply", units, f"{action} track {track2['id']} with {Path(srt.path).name}")
    if not moved:
        print("[FLAG] lastUpdated didn't change. The upload may not have taken; check Studio > Subtitles.")
        return EXIT_CHECK
    print("Done. Our captions are on the video.")
    return EXIT_OK

# --------------------------------------------------------------------- check

def cmd_check(youtube: Any, ep: dict, cfg: dict, video_id_override: str | None) -> int:
    state = load_state(ep["slug"])
    vid_id = require_video_id(state, video_id_override)
    vid = get_video(youtube, vid_id)
    s = video_summary(vid)
    desc = vid["snippet"].get("description", "")
    flags = 0

    def line(ok: bool, text: str) -> None:
        nonlocal flags
        flags += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FLAG'}] {text}")

    line(v.normalize_title(s["title"]) == v.normalize_title(ep["title"]),
         f"title matches Riverside" if v.normalize_title(s["title"]) == v.normalize_title(ep["title"])
         else f"title differs:\n    riverside: {ep['title']}\n    youtube:   {s['title']}")
    for p in v.title_problems(s["title"]):
        line(False, p)

    expected = v.html_to_youtube_text(ep["description_html"])
    d = v.description_diff(expected, desc)
    line(not d, "description matches Riverside" if not d else "description differs from Riverside:")
    for ln in d[:60]:
        print(f"    {ln}")
    if len(d) > 60:
        print(f"    ... {len(d) - 60} more diff lines")

    n, probs = v.validate_chapters(desc, s["duration"] or None)
    line(not probs, f"{n} chapters, YouTube rules OK" if not probs else f"chapters ({n} found):")
    for p in probs:
        print(f"    - {p}")

    lp = v.validate_links(desc, ep.get("post_url"), cfg)
    line(not lp, "links OK (write-up and support present, nothing pointing to Riverside)" if not lp else "links:")
    for p in lp:
        print(f"    - {p}")
    print(f"\n{'All checks passed.' if not flags else f'{flags} flag(s).'}")
    return EXIT_OK if not flags else EXIT_CHECK
