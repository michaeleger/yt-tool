"""Google side of yt.py: config, OAuth token, channel guard, retrying calls, quota count."""
from __future__ import annotations

import json
import logging
import os
import random
import socket
import stat
import time
from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_CREDS_DIR = Path.home() / ".yt_creds"
AUTH_PORT = 4568  # SEO Advisor's authorize.py uses 4567

log = logging.getLogger("yt")

# Exit codes (spec section 5)
EXIT_OK, EXIT_ERROR, EXIT_CHECK, EXIT_AUTH, EXIT_QUOTA = 0, 1, 2, 3, 4


class YtError(Exception):
    exit_code = EXIT_ERROR


class AuthError(YtError):
    exit_code = EXIT_AUTH


class QuotaError(YtError):
    exit_code = EXIT_QUOTA


class CheckError(YtError):
    exit_code = EXIT_CHECK


# ------------------------------------------------------------------ config

def load_config(path: Path | None = None) -> dict:
    p = path or TOOL_DIR / "config.json"
    if not p.exists():
        raise YtError(f"no config at {p}. Copy config.example.json to config.json and fill it in.")
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("client_secret_path", str(Path.home() / ".yt_creds" / "client_secret.json"))
    cfg.setdefault("creds_dir", str(DEFAULT_CREDS_DIR))
    cfg.setdefault("writeup_home_urls", ["https://www.eagertobehealthy.com"])
    cfg.setdefault("support_url", "https://www.eagertobehealthy.com/support")
    cfg.setdefault("flag_patterns", [r"riverside\.(com|fm)"])
    return cfg


def setup_logging() -> None:
    logdir = TOOL_DIR / "logs"
    logdir.mkdir(exist_ok=True)
    h = logging.FileHandler(logdir / "yt.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    # keep request headers and tokens out of any log
    for name in ("googleapiclient", "googleapiclient.discovery", "google_auth_oauthlib",
                 "google.auth", "urllib3", "oauthlib", "requests_oauthlib"):
        logging.getLogger(name).setLevel(logging.WARNING)


def scrub(msg: str) -> str:
    """Remove anything token-shaped from an error message before printing it."""
    import re
    msg = re.sub(r"(access_token|refresh_token|client_secret|code)=[^&\s]+", r"\1=***", msg)
    msg = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", msg)
    msg = re.sub(r"ya29\.[A-Za-z0-9._\-]+", "ya29.***", msg)
    msg = re.sub(r"1//[A-Za-z0-9._\-]{20,}", "1//***", msg)
    return msg

# ------------------------------------------------------------------- creds

def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def check_permissions(cfg: dict, need_token: bool = True) -> None:
    creds_dir = Path(cfg["creds_dir"]).expanduser()
    token = creds_dir / "token.json"
    secret = Path(cfg["client_secret_path"]).expanduser()
    if not secret.exists():
        raise AuthError(f"client secret not found at {secret} (config: client_secret_path)")
    if _mode(secret.resolve()) & 0o077:
        raise AuthError(f"{secret.resolve()} is readable by others. Fix: chmod 600 {secret.resolve()}")
    if need_token:
        if not token.exists():
            raise AuthError(f"no token at {token}. Run: yt.py auth")
        if _mode(token) & 0o077:
            raise AuthError(f"{token} is readable by others. Fix: chmod 600 {token}")


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def run_auth_flow(cfg: dict, open_browser: bool) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow
    check_permissions(cfg, need_token=False)
    flow = InstalledAppFlow.from_client_secrets_file(str(Path(cfg["client_secret_path"]).expanduser()), SCOPES)
    if not open_browser:
        print(f"Open the sign-in link below in a browser that can reach localhost:{AUTH_PORT}.")
        print(f"(From your laptop: ssh -L {AUTH_PORT}:localhost:{AUTH_PORT} you@your-server)")
        print("On the second screen pick the Eager to Be Healthy channel, NOT your personal profile.\n")
    creds = flow.run_local_server(host="localhost", port=AUTH_PORT, open_browser=open_browser,
                                  prompt="consent", access_type="offline",
                                  authorization_prompt_message="Sign-in link: {url}",
                                  success_message="Signed in. You can close this tab and go back to the terminal.")
    _write_private(Path(cfg["creds_dir"]).expanduser() / "token.json", creds.to_json())
    return creds


def load_credentials(cfg: dict) -> Any:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    check_permissions(cfg, need_token=True)
    token = Path(cfg["creds_dir"]).expanduser() / "token.json"
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise AuthError("token refresh failed (expired or revoked). Run: yt.py auth. "
                                f"Detail: {scrub(str(e))}") from None
            _write_private(token, creds.to_json())
        else:
            raise AuthError("token is not valid. Run: yt.py auth")
    return creds


def build_service(creds: Any) -> Any:
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

# ----------------------------------------------------------- calls + quota

class Quota:
    used = 0


def execute(request: Any, cost: int, label: str, retries: int = 3) -> Any:
    """Run one API request with retry on transient errors. Counts estimated quota."""
    from googleapiclient.errors import HttpError
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            Quota.used += cost
            result = request.execute()
            log.info("%s ok (est %d units)", label, cost)
            return result
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            reason = _reason(e)
            log.warning("%s HTTP %s %s", label, status, reason)
            if reason in ("quotaExceeded", "dailyLimitExceeded") and status == 403:
                raise QuotaError("YouTube API daily quota exceeded. It resets at midnight Pacific time.") from None
            retryable = status in (429, 500, 502, 503, 504) or reason in ("rateLimitExceeded", "userRateLimitExceeded")
            if retryable and attempt < retries:
                time.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
                continue
            if status in (401,):
                raise AuthError(f"{label}: not authorized ({reason}). Run: yt.py auth") from None
            raise YtError(f"{label} failed: HTTP {status} {reason}: {scrub(_message(e))}") from None
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            if attempt < retries:
                log.warning("%s network error, retrying: %s", label, scrub(str(e)))
                time.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
                continue
            raise YtError(f"{label}: network error: {scrub(str(e))}") from None


def _reason(e: Any) -> str:
    try:
        data = json.loads(e.content.decode("utf-8"))
        errs = data.get("error", {}).get("errors", [])
        return errs[0].get("reason", "") if errs else data.get("error", {}).get("status", "")
    except Exception:
        return ""


def _message(e: Any) -> str:
    try:
        return json.loads(e.content.decode("utf-8")).get("error", {}).get("message", "")
    except Exception:
        return str(e)

# ------------------------------------------------------------ channel guard

def my_channel(youtube: Any) -> dict:
    res = execute(youtube.channels().list(part="snippet,contentDetails", mine=True), 1, "channels.list")
    items = res.get("items", [])
    if len(items) != 1:
        raise AuthError(f"token sees {len(items)} channels; expected exactly one (the Brand Account)")
    ch = items[0]
    return {"id": ch["id"], "title": ch["snippet"]["title"],
            "uploads": ch["contentDetails"]["relatedPlaylists"]["uploads"]}


def verify_channel(youtube: Any, cfg: dict) -> dict:
    ch = my_channel(youtube)
    expected = cfg.get("expected_channel_id", "")
    print(f"Channel: {ch['title']} ({ch['id']})")
    if not expected:
        raise AuthError("expected_channel_id is empty in config.json. Put the channel ID above there "
                        "if it is the Eager to Be Healthy channel, then run again.")
    if ch["id"] != expected:
        raise AuthError(f"signed in as the wrong channel ({ch['title']}). Run yt.py auth and pick "
                        "the Eager to Be Healthy channel.")
    return ch
