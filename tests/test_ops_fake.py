"""Command flow tests against a fake YouTube service (no network, 0 units).

Needs google-api-python-client installed (for MediaFileUpload / HttpError).
Run: python -m unittest discover -s tests -v
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import yt_ops  # noqa: E402
import yt_validate as v  # noqa: E402

FX = HERE / "fixtures"
CFG = {"writeup_home_urls": ["https://www.eagertobehealthy.com"],
       "support_url": "https://www.eagertobehealthy.com/support",
       "flag_patterns": [r"riverside\.(com|fm)"]}
RAW_SRT = "1\n00:00:00,000 --> 00:00:04,000\nUm, hi, uh, I'm, I'm Sam, um.\n"


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeYouTube:
    """Just enough of the youtube v3 client for yt_ops."""

    def __init__(self, title, description, duration="PT1M0S", privacy="private",
                 publish_at="2026-10-16T12:30:00Z", tracks=None):
        self.video = {"id": "vid123", "snippet": {"title": title, "description": description},
                      "status": {"privacyStatus": privacy, "publishAt": publish_at},
                      "contentDetails": {"duration": duration},
                      "processingDetails": {"processingStatus": "succeeded"}}
        self.other = {"id": "old999", "snippet": {"title": "Some older episode", "description": ""},
                      "status": {"privacyStatus": "public"}, "contentDetails": {"duration": "PT40M"},
                      "processingDetails": {"processingStatus": "succeeded"}}
        self.tracks = tracks if tracks is not None else [
            {"id": "trk1", "snippet": {"language": "en-US", "trackKind": "standard", "name": "",
                                        "status": "serving", "isDraft": False, "lastUpdated": "2026-10-14T01:00:00Z"}}]
        self.track_text = {"trk1": RAW_SRT}
        self.calls = []

    # resources
    def playlistItems(self):
        return self

    def videos(self):
        return self

    def captions(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        if "playlistId" in kw:
            return Req(lambda: {"items": [{"contentDetails": {"videoId": "vid123"}},
                                          {"contentDetails": {"videoId": "old999"}}]})
        if "videoId" in kw:
            return Req(lambda: {"items": [dict(t) for t in self.tracks]})
        ids = kw["id"].split(",")
        return Req(lambda: {"items": [x for x in (self.video, self.other) if x["id"] in ids]})

    def download_media(self, id, tfmt):
        self.calls.append(("download", id))
        return Req(lambda: self.track_text[id].encode())

    def update(self, part, body, media_body):
        self.calls.append(("update", body["id"]))

        def do():
            for t in self.tracks:
                if t["id"] == body["id"]:
                    t["snippet"]["lastUpdated"] = "2026-10-14T09:00:00Z"
            self.track_text[body["id"]] = Path(media_body._filename).read_text(encoding="utf-8-sig")
            return {"id": body["id"]}
        return Req(do)

    def insert(self, part, body, media_body):
        self.calls.append(("insert", body["snippet"]["language"]))

        def do():
            self.tracks.append({"id": "trkNEW", "snippet": {"language": "en-US", "trackKind": "standard",
                                                             "name": "", "status": "serving",
                                                             "lastUpdated": "2026-10-14T09:00:00Z"}})
            self.track_text["trkNEW"] = Path(media_body._filename).read_text(encoding="utf-8-sig")
            return {"id": "trkNEW"}
        return Req(do)


def run(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_ep_dir = yt_ops.EP_DIR
        yt_ops.EP_DIR = self.tmp / "episodes"
        mark = json.loads((FX / "sample-episode.json").read_text(encoding="utf-8"))
        shutil.copy(FX / "valid.srt", self.tmp / "clean.srt")
        mark["srt_path"] = str(self.tmp / "clean.srt")
        mark["publication_date"] = "2099-10-16T12:30:00Z"
        (self.tmp / "ep.json").write_text(json.dumps(mark), encoding="utf-8")
        self.ep = yt_ops.load_episode(self.tmp / "ep.json")
        desc = v.html_to_youtube_text(mark["description_html"])
        self.yt = FakeYouTube(mark["title"].replace("'", "’"), desc, publish_at="2099-10-16T12:30:00.000Z")
        self.ch = {"id": "UCx", "title": "Eager to Be Healthy", "uploads": "UUx"}
        pkg = self.tmp / "pkg"
        pkg.mkdir()
        (pkg / "index.html").write_text("<p>x</p>\nhttpv://www.youtube.com/watch?v=YOUTUBE_ID_HERE\n", encoding="utf-8")
        self.pkg = pkg

    def tearDown(self):
        yt_ops.EP_DIR = self.old_ep_dir
        shutil.rmtree(self.tmp)

    def test_full_flow(self):
        # find: dry run writes nothing
        rc, out = run(yt_ops.cmd_find, self.yt, self.ch, self.ep, str(self.pkg), None, False)
        self.assertEqual(rc, 0, out)
        self.assertIn("vid123", out)
        self.assertIn("YOUTUBE_ID_HERE", (self.pkg / "index.html").read_text())
        self.assertFalse((yt_ops.EP_DIR / "sam-example.json").exists())
        # find --apply
        rc, out = run(yt_ops.cmd_find, self.yt, self.ch, self.ep, str(self.pkg), None, True)
        self.assertEqual(rc, 0, out)
        self.assertIn("watch?v=vid123", (self.pkg / "index.html").read_text())
        # captions dry run: plans an update, no write
        rc, out = run(yt_ops.cmd_captions, self.yt, self.ep, None, False, False, False)
        self.assertEqual(rc, 0, out)
        self.assertIn("UPDATE track trk1", out)
        self.assertNotIn(("update", "trk1"), self.yt.calls)
        # captions --apply: backup + update
        rc, out = run(yt_ops.cmd_captions, self.yt, self.ep, None, True, False, False)
        self.assertEqual(rc, 0, out)
        self.assertIn(("download", "trk1"), self.yt.calls)
        self.assertIn(("update", "trk1"), self.yt.calls)
        self.assertEqual((yt_ops.EP_DIR / "sam-example-riverside-captions.srt").read_text(), RAW_SRT)
        # second run skips
        n_updates = self.yt.calls.count(("update", "trk1"))
        rc, out = run(yt_ops.cmd_captions, self.yt, self.ep, None, True, False, False)
        self.assertIn("Nothing to do", out)
        self.assertEqual(self.yt.calls.count(("update", "trk1")), n_updates)
        # status passes, then flags when someone (Riverside) changes the track
        rc, out = run(yt_ops.cmd_status, self.yt, self.ep, None)
        self.assertEqual(rc, 0, out)
        self.assertIn("our captions are still the active English track", out)
        self.yt.tracks[0]["snippet"]["lastUpdated"] = "2026-10-15T00:00:00Z"
        rc, out = run(yt_ops.cmd_status, self.yt, self.ep, None)
        self.assertEqual(rc, 2, out)
        self.assertIn("captions changed since we uploaded them", out)
        # check passes on matching metadata (curly apostrophe in YouTube title is fine)
        rc, out = run(yt_ops.cmd_check, self.yt, self.ep, CFG, None)
        self.assertEqual(rc, 2, out)  # 60 s fake video makes Sam's chapters fail the last-chapter rule
        self.assertIn("[PASS] title matches Riverside", out)
        self.assertIn("[PASS] description matches Riverside", out)
        self.assertIn("[PASS] links OK", out)

    def test_check_flags_description_change_and_feed_link(self):
        run(yt_ops.cmd_find, self.yt, self.ch, self.ep, None, None, True)
        self.yt.video["snippet"]["description"] += "\nSubscribe: https://api.riverside.com/hosting/drlxDc7x.rss"
        self.yt.video["contentDetails"]["duration"] = "PT55M27S"
        rc, out = run(yt_ops.cmd_check, self.yt, self.ep, CFG, None)
        self.assertEqual(rc, 2)
        self.assertIn("description differs", out)
        self.assertIn("off-site to Riverside", out)
        self.assertIn("29 chapters, YouTube rules OK", out)

    def test_ambiguous_title_stops(self):
        self.yt.other["snippet"]["title"] = self.ep["title"]
        rc, out = run(yt_ops.cmd_find, self.yt, self.ch, self.ep, None, None, True)
        self.assertEqual(rc, 2)
        self.assertIn("2 videos match", out)

    def test_insert_when_only_asr(self):
        self.yt.tracks = [{"id": "asr1", "snippet": {"language": "en", "trackKind": "asr", "status": "serving",
                                                      "lastUpdated": "2026-10-14T01:00:00Z"}}]
        run(yt_ops.cmd_find, self.yt, self.ch, self.ep, None, None, True)
        rc, out = run(yt_ops.cmd_captions, self.yt, self.ep, None, True, False, False)
        self.assertEqual(rc, 0, out)
        self.assertIn(("insert", "en-US"), self.yt.calls)
        self.assertNotIn(("download", "asr1"), self.yt.calls)

    def test_adopt_hand_uploaded_track(self):
        self.yt.track_text["trk1"] = (FX / "valid.srt").read_text(encoding="utf-8-sig")
        run(yt_ops.cmd_find, self.yt, self.ch, self.ep, None, None, True)
        rc, out = run(yt_ops.cmd_captions, self.yt, self.ep, None, True, False, True)
        self.assertEqual(rc, 0, out)
        self.assertIn("100.0% word match", out)
        self.assertNotIn(("update", "trk1"), self.yt.calls)
        rc, out = run(yt_ops.cmd_status, self.yt, self.ep, None)
        self.assertIn("our captions are still the active English track", out)

    def test_adopt_refuses_raw_track(self):
        run(yt_ops.cmd_find, self.yt, self.ch, self.ep, None, None, True)
        rc, out = run(yt_ops.cmd_captions, self.yt, self.ep, None, True, False, True)
        self.assertEqual(rc, 2)
        self.assertIn("not our SRT", out)


if __name__ == "__main__":
    unittest.main()
