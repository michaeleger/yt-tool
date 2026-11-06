"""Offline tests (0 quota units). Run: python -m unittest discover -s tests -v"""
import json
import sys
import unittest
from datetime import timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import yt_validate as v  # noqa: E402

FX = HERE / "fixtures"
CFG = {"writeup_home_urls": ["https://www.eagertobehealthy.com"],
       "support_url": "https://www.eagertobehealthy.com/support",
       "flag_patterns": [r"riverside\.(com|fm)"]}
MARK = json.loads((FX / "sample-episode.json").read_text(encoding="utf-8"))
MARK_DURATION = 3326.7


class Timestamps(unittest.TestCase):
    def test_z_suffix_on_py310(self):
        dt = v.parse_iso_utc("2026-10-16T12:30:00Z")
        self.assertEqual((dt.hour, dt.minute, dt.tzinfo), (12, 30, timezone.utc))

    def test_millis_z(self):
        self.assertEqual(v.parse_iso_utc("2026-10-16T12:30:00.000Z"), v.parse_iso_utc("2026-10-16T12:30:00Z"))

    def test_offset(self):
        self.assertEqual(v.parse_iso_utc("2026-10-16T08:30:00-04:00"), v.parse_iso_utc("2026-10-16T12:30:00Z"))

    def test_naive_refused(self):
        with self.assertRaises(ValueError):
            v.parse_iso_utc("2026-10-16T12:30:00")

    def test_durations(self):
        self.assertEqual(v.iso_duration_seconds("PT55M27S"), 3327)
        self.assertEqual(v.iso_duration_seconds("PT1H2M3.5S"), 3723.5)
        self.assertEqual(v.iso_duration_seconds("P0D"), 0)


class Srt(unittest.TestCase):
    def test_valid_with_bom_and_crlf(self):
        r = v.parse_and_validate_srt(FX / "valid.srt", 60)
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(len(r.cues), 3)
        self.assertEqual(r.cues[0].text, "Hi, I’m Sam.")
        self.assertEqual(len(r.sha256), 64)

    def test_out_of_order(self):
        r = v.parse_and_validate_srt(FX / "out_of_order.srt")
        self.assertFalse(r.ok)
        self.assertTrue(any("out of order" in e for e in r.errors))

    def test_overrun(self):
        r = v.parse_and_validate_srt(FX / "overrun.srt", 60)
        self.assertFalse(r.ok)
        self.assertTrue(any("past the video" in e for e in r.errors))

    def test_overrun_within_grace(self):
        r = v.parse_and_validate_srt(FX / "overrun.srt", 69)
        self.assertTrue(r.ok, r.errors)

    def test_captions_match_ignores_formatting(self):
        a = (FX / "valid.srt").read_text(encoding="utf-8-sig")
        b = "1\n00:00:00,100 --> 00:00:04,000\nHi, I'm Sam. Welcome\n\n2\n00:00:04,000 --> 00:00:09,000\nback. That is all.\n"
        ok, ratio = v.captions_match(a, b)
        self.assertTrue(ok, ratio)

    def test_captions_mismatch(self):
        a = (FX / "valid.srt").read_text(encoding="utf-8-sig")
        b = "1\n00:00:00,000 --> 00:00:04,000\nUm, hi, uh, I'm, I'm Sam, um.\n"
        self.assertFalse(v.captions_match(a, b)[0])


class Titles(unittest.TestCase):
    def test_curly_quotes_entities_whitespace(self):
        self.assertEqual(v.normalize_title("Mom’s  Plans &amp; More "), v.normalize_title("mom's plans & more"))

    def test_problems(self):
        self.assertTrue(v.title_problems("x" * 101))
        self.assertTrue(v.title_problems("a <b>"))
        self.assertFalse(v.title_problems(MARK["title"]))


class Description(unittest.TestCase):
    def test_links_become_hrefs(self):
        t = v.html_to_youtube_text('<p>Find him at <a href="https://x.org" rel="nofollow">x.org</a>.</p>')
        self.assertEqual(t, "Find him at https://x.org.")

    def test_paragraphs(self):
        t = v.html_to_youtube_text("<p>One</p><p>Two &amp; three</p><p>a<br>b</p>")
        self.assertEqual(t, "One\n\nTwo & three\n\na\nb")

    def test_diff_ignores_blank_lines(self):
        self.assertEqual(v.description_diff("A\n\nB\n", "A\nB"), [])
        self.assertTrue(v.description_diff("A\nB", "A\nC"))

    def test_mark_conversion(self):
        t = v.html_to_youtube_text(MARK["description_html"])
        self.assertIn("Find him at https://example-guest.org.", t)
        self.assertIn("Full write-up, transcript and sources: https://www.eagertobehealthy.com", t)
        self.assertNotIn("<", t)


class Chapters(unittest.TestCase):
    def test_mark_chapters_pass(self):
        t = v.html_to_youtube_text(MARK["description_html"])
        n, probs = v.validate_chapters(t, MARK_DURATION)
        self.assertEqual(n, 29)
        self.assertEqual(probs, [])

    def test_missing_zero(self):
        n, probs = v.validate_chapters("0:05 a\n1:00 b\n2:00 c", 300)
        self.assertTrue(any("must be 0:00" in p for p in probs))

    def test_short_and_too_few(self):
        _, probs = v.validate_chapters("00:00 a\n00:08 b", 300)
        self.assertTrue(any("minimum 10s" in p for p in probs))
        self.assertTrue(any("at least 3" in p for p in probs))

    def test_last_chapter_too_short(self):
        _, probs = v.validate_chapters("00:00 a\n01:00 b\n04:55 c", 300)
        self.assertTrue(any("last chapter" in p for p in probs))


class Links(unittest.TestCase):
    def test_mark_passes(self):
        t = v.html_to_youtube_text(MARK["description_html"])
        self.assertEqual(v.validate_links(t, None, CFG), [])

    def test_post_url_required_once_set(self):
        t = v.html_to_youtube_text(MARK["description_html"])
        probs = v.validate_links(t, "https://www.eagertobehealthy.com/sam-example-family-caregiver/", CFG)
        self.assertTrue(any("write-up link missing" in p for p in probs))

    def test_post_url_found_with_or_without_www(self):
        t = "Read: https://eagertobehealthy.com/mark-x/ Support: https://www.eagertobehealthy.com/support"
        self.assertEqual(v.validate_links(t, "https://www.eagertobehealthy.com/mark-x", CFG), [])

    def test_feed_and_media_flagged(self):
        t = ("https://www.eagertobehealthy.com https://www.eagertobehealthy.com/support "
             "https://api.riverside.com/hosting/drlxDc7x.rss https://hosting-media.riverside.com/media/clips/x.mp3")
        probs = v.validate_links(t, None, CFG)
        self.assertEqual(sum("off-site" in p for p in probs), 2)

    def test_guest_site_ok(self):
        t = "https://www.eagertobehealthy.com https://www.eagertobehealthy.com/support https://example-guest.org"
        self.assertEqual(v.validate_links(t, None, CFG), [])


class Tracks(unittest.TestCase):
    def T(self, i, lang, kind):
        return {"id": i, "snippet": {"language": lang, "trackKind": kind}}

    def test_one_standard(self):
        a, t, _ = v.pick_english_track([self.T("1", "en", "asr"), self.T("2", "en-US", "standard"),
                                        self.T("3", "es", "standard")])
        self.assertEqual((a, t["id"]), ("update", "2"))

    def test_only_asr(self):
        self.assertEqual(v.pick_english_track([self.T("1", "en", "ASR")])[0], "insert")

    def test_none(self):
        self.assertEqual(v.pick_english_track([])[0], "insert")

    def test_two_standard(self):
        self.assertEqual(v.pick_english_track([self.T("1", "en", "standard"), self.T("2", "en-GB", "standard")])[0],
                         "ambiguous")


if __name__ == "__main__":
    unittest.main()
