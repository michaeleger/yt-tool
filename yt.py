#!/usr/bin/env python3
"""yt.py: the YouTube step for Eager to Be Healthy episodes.

Riverside uploads and schedules each episode video. This tool finds it, puts our
clean captions on it, checks it against the Riverside episode, and guards the
captions. It never uploads video. Writes happen only with --apply.

  yt.py auth [--no-browser]
  yt.py find     --episode EP.json [--package DIR] [--video-id ID] [--apply]
  yt.py status   --episode EP.json [--video-id ID]
  yt.py captions --episode EP.json [--video-id ID] [--adopt] [--force] [--apply]
  yt.py check    --episode EP.json [--video-id ID]

Exit codes: 0 ok, 1 error, 2 check failed, 3 auth/wrong channel, 4 quota.
See README.md for setup.
"""
from __future__ import annotations

import argparse
import sys

import yt_api
from yt_api import Quota, YtError, scrub


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="yt.py", description=__doc__.split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config.json path (default: next to yt.py)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="one-time sign-in; pick the Eager to Be Healthy channel")
    a.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")

    def ep_cmd(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--episode", required=True, help="episode.json written at the publish step")
        p.add_argument("--video-id", help="skip title matching / state and use this video")
        return p

    f = ep_cmd("find", "find Riverside's video by title; save its ID")
    f.add_argument("--package", help="blog package dir: replace YOUTUBE_ID_HERE in its index.html")
    f.add_argument("--apply", action="store_true")
    ep_cmd("status", "read-only: schedule, processing, caption length, caption guard")
    c = ep_cmd("captions", "put our SRT on the video as the English track")
    c.add_argument("--adopt", action="store_true",
                   help="record a track that already has our text (uploaded by hand) without re-uploading")
    c.add_argument("--force", action="store_true", help="upload even if it looks done already")
    c.add_argument("--apply", action="store_true")
    ep_cmd("check", "read-only: title, description, chapters, links vs Riverside")

    args = ap.parse_args(argv)
    yt_api.setup_logging()
    try:
        from pathlib import Path
        cfg = yt_api.load_config(Path(args.config) if args.config else None)
        if args.cmd == "auth":
            yt_api.run_auth_flow(cfg, open_browser=not args.no_browser)
            youtube = yt_api.build_service(yt_api.load_credentials(cfg))
            ch = yt_api.my_channel(youtube)
            print(f"\nSigned in as channel: {ch['title']} ({ch['id']})")
            if cfg.get("expected_channel_id") and cfg["expected_channel_id"] != ch["id"]:
                print("[FLAG] That is not the expected channel. Run auth again and pick Eager to Be Healthy.")
                return yt_api.EXIT_AUTH
            if not cfg.get("expected_channel_id"):
                print("If that is the Eager to Be Healthy channel, put this ID in config.json "
                      f"as expected_channel_id: {ch['id']}")
            return yt_api.EXIT_OK

        import yt_ops
        ep = yt_ops.load_episode(args.episode)
        youtube = yt_api.build_service(yt_api.load_credentials(cfg))
        ch = yt_api.verify_channel(youtube, cfg)
        if args.cmd == "find":
            rc = yt_ops.cmd_find(youtube, ch, ep, args.package, args.video_id, args.apply)
        elif args.cmd == "status":
            rc = yt_ops.cmd_status(youtube, ep, args.video_id)
        elif args.cmd == "captions":
            rc = yt_ops.cmd_captions(youtube, ep, args.video_id, args.apply, args.force, args.adopt)
        elif args.cmd == "check":
            rc = yt_ops.cmd_check(youtube, ep, cfg, args.video_id)
        else:  # pragma: no cover
            rc = yt_api.EXIT_ERROR
        print(f"(quota used this run: about {Quota.used} of 10,000 daily units)")
        return rc
    except YtError as e:
        print(f"ERROR: {scrub(str(e))}", file=sys.stderr)
        yt_api.log.error("%s: %s", args.cmd, scrub(str(e)))
        return e.exit_code
    except KeyboardInterrupt:
        return yt_api.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
