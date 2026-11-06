# yt-tool

Put clean captions on a podcast's YouTube video and check the video against the episode, without ever uploading video.

## Why

Riverside can publish each episode to YouTube for you, but it attaches its own auto-captions and you can't easily check that the title, description, chapters and links match what you published elsewhere. Fixing that by hand in YouTube Studio for every episode is slow and easy to get wrong. yt-tool finds the video Riverside scheduled, uploads your edited SRT, validates description links and chapters, backs up Riverside's caption track, and warns you later if your captions get replaced.

Built for the [Eager to Be Healthy](https://www.eagertobehealthy.com) podcast.

## What it does

- `auth`: one-time OAuth sign-in. It checks that you picked the expected channel (Brand Accounts are supported).
- `find`: matches the episode to its YouTube video by title and publish time, and records the video id.
- `status`: read-only report of the video, caption tracks and whether ours is still in place.
- `captions`: validates your SRT (ordering, overruns, filler cleanup), backs up Riverside's track, then uploads yours. `--adopt` records a track you already uploaded by hand.
- `check`: validates description text, chapters (YouTube's rules) and links against your config.
- Dry run by default. Nothing is written without `--apply`. Tokens are never logged.

Exit codes: 0 ok, 1 error, 2 check failed, 3 auth or wrong channel, 4 quota.

## Status: alpha, read this

- The offline test suite passes (36 tests, using a fake YouTube client).
- **The live YouTube path is not fully proven yet.** OAuth and the API calls are written to Google's docs, but a full live run against a real channel is still pending. Expect rough edges in `auth` and `captions --apply`. Try `status` (read-only) first.
- `link` (swapping the write-up link in the YouTube description) is not built.

## Requirements

- Linux or macOS, Python 3.10+
- A Google Cloud project with **YouTube Data API v3** enabled
- An OAuth client of type **Desktop app** (`client_secret.json`) with scope `https://www.googleapis.com/auth/youtube.force-ssl`
- Default quota (10,000 units/day) is plenty. Rough costs: find 3, status 51, captions first run 750, check 1.

## Install

```bash
git clone https://github.com/michaeleger/yt-tool.git
cd yt-tool
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json`:

| Key | Meaning |
|---|---|
| `expected_channel_id` | Your channel id (`UC...`). Leave it empty for the first `auth`, then paste the id it prints. |
| `client_secret_path` | Path to your OAuth client JSON (keep it outside the repo) |
| `creds_dir` | Where the token is stored (default `~/.yt_creds`, mode 600) |
| `writeup_home_urls` | Your site URLs that description links may point to |
| `support_url` | A URL every description must include (e.g. your support page) |
| `flag_patterns` | Regexes for links that should never appear (e.g. `riverside\\.(com\|fm)`) |

### Google Cloud setup (once)

1. Enable **YouTube Data API v3** in your project.
2. Add the `youtube.force-ssl` scope on the consent screen. Set the app to *In production* (unverified is fine for your own channel) so refresh tokens don't expire after 7 days.
3. Create a **Desktop app** OAuth client and download its JSON.

### Authorize

```bash
.venv/bin/python yt.py auth                       # opens a browser
# headless server:
ssh -L 4568:localhost:4568 you@your-server
.venv/bin/python yt.py auth --no-browser          # open the printed URL on your laptop
```

On Google's account picker, choose the channel (Brand Account), not your personal profile. Put the printed channel id into `config.json`.

## Usage

Each episode is described by a JSON file. See `examples/sample-episode.json`:

```json
{"slug": "...", "title": "...", "description_html": "<p>...</p>",
 "publication_date": "2026-10-16T12:30:00Z", "srt_path": "~/captions/episode.srt", "post_url": null}
```

```bash
.venv/bin/python yt.py find     --episode EP.json [--package DIR]   # dry run; add --apply to save
.venv/bin/python yt.py status   --episode EP.json                   # read-only
.venv/bin/python yt.py captions --episode EP.json                   # dry run; add --apply to upload
.venv/bin/python yt.py captions --episode EP.json --adopt --apply   # captions already uploaded by hand
.venv/bin/python yt.py check    --episode EP.json                   # read-only
```

Pass `--video-id ID` to skip title matching. Run `status` again after any Riverside edit and after the episode goes live, because it flags when your captions were replaced.

Local state goes in `episodes/` (video id, plus a backup of Riverside's captions) and the log goes in `logs/yt.log`. Both are gitignored.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Limitations

- Alpha. See the status section above.
- Assumes Riverside (or someone else) already uploaded and scheduled the video.
- One channel per config.

## License

MIT. See [LICENSE](LICENSE).
