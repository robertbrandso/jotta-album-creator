"""Shared HTTP helper for the Jottacloud APIs.

Adds the bearer token, refreshes it once on 401, and retries transient failures
(network errors, HTTP 5xx) for idempotent GET requests.
"""
import time
import urllib.error
import urllib.request

from jotta_auth import get_access_token

GET_ATTEMPTS = 3

_access_token = None


class ApiError(Exception):
    """A request failed. Callers may catch this to carry on with other work."""


def _send(method, url, data, headers):
    global _access_token
    if _access_token is None:
        _access_token = get_access_token()
    h = {"Authorization": f"Bearer {_access_token}", "Origin": "https://jottacloud.com"}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(), resp.headers


def http(method, url, data=None, headers=None, with_headers=False):
    """Send a request and return the body (or (body, headers) with with_headers)."""
    global _access_token
    headers = headers or {}
    attempts = GET_ATTEMPTS if method == "GET" else 1
    short_url = url.split("?")[0]
    for attempt in range(attempts):
        last = attempt + 1 == attempts
        try:
            try:
                result = _send(method, url, data, headers)
            except urllib.error.HTTPError as e:
                if e.code != 401:
                    raise
                _access_token = get_access_token()
                result = _send(method, url, data, headers)
            return result if with_headers else result[0]
        except urllib.error.HTTPError as e:
            if e.code >= 500 and not last:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(f"{method} {short_url} failed ({e.code}): {e.read()[:300]!r}")
        except OSError as e:  # URLError, timeouts, connection resets
            if not last:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(f"{method} {short_url} failed: {e}")
