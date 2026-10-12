"""Jottacloud auth helper.

The personal login token (JOTTA_LOGIN_TOKEN) is single use: it is exchanged once
for a refresh token, which is stored in a 0600 file outside the repo and rotated
on every refresh. Tokens are never printed.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "jottacli"
STORE = os.path.expanduser("~/.config/jotta-album-creator/token.json")
POST_ATTEMPTS = 3


def _post(url, fields):
    """POST a form. Returns (status, body); status 0 means the network failed.

    Network errors and 5xx are retried, since they say nothing about the token.
    """
    data = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    for attempt in range(POST_ATTEMPTS):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()[:200]
            if status < 500:
                return status, body
        except OSError as e:  # URLError, timeouts
            status, body = 0, str(e)
        if attempt + 1 < POST_ATTEMPTS:
            time.sleep(2 * 2 ** attempt)
    return status, body


def _get_json(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def _save(token_endpoint, refresh_token):
    """Write the token file atomically, so a kill mid-write can't leave it truncated."""
    os.makedirs(os.path.dirname(STORE), mode=0o700, exist_ok=True)
    tmp = f"{STORE}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token_endpoint": token_endpoint, "refresh_token": refresh_token}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE)


def _login_with_login_token(raw):
    padded = raw + "=" * (-len(raw) % 4)
    info = json.loads(base64.urlsafe_b64decode(padded))
    endpoint = _get_json(info["well_known_link"])["token_endpoint"]
    status, body = _post(endpoint, {
        "client_id": CLIENT_ID,
        "grant_type": "password",
        "username": info["username"],
        "password": info["auth_token"],
    })
    if status != 200:
        raise SystemExit(f"login token exchange failed ({status}): {body!r}. "
                         "The login token is single use; create a new one.")
    _save(endpoint, body["refresh_token"])
    return body["access_token"]


def _refresh():
    try:
        with open(STORE) as f:
            saved = json.load(f)
        endpoint, refresh_token = saved["token_endpoint"], saved["refresh_token"]
    except (OSError, ValueError, KeyError):
        raise SystemExit(f"Token file {STORE} is missing or unreadable. "
                         "Delete it and log in again with a new JOTTA_LOGIN_TOKEN.")
    status, body = _post(endpoint, {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if status in (400, 401):
        raise SystemExit(f"The stored refresh token was rejected ({status}): {body!r}. "
                         f"Delete {STORE} and log in again with a new JOTTA_LOGIN_TOKEN.")
    if status != 200:
        raise SystemExit(f"Token endpoint unavailable ({status or 'network error'}: {body!r}). "
                         "The stored token is unchanged; try again later.")
    _save(endpoint, body.get("refresh_token", refresh_token))
    return body["access_token"]


def get_access_token():
    if os.path.exists(STORE):
        return _refresh()
    raw = os.environ.get("JOTTA_LOGIN_TOKEN")
    if not raw:
        raise SystemExit("No stored token and JOTTA_LOGIN_TOKEN is not set")
    return _login_with_login_token(raw.strip())
