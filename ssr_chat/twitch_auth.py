"""Opt-in Twitch read-only OAuth. No legacy state lookup or chat send API."""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import ctypes
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
SUBSCRIPTIONS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
REQUIRED_SCOPES = frozenset({"user:read:chat"})
OWNER = "ssr-chat-twitch-receive-v1"


class TwitchAuthError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HttpResult:
    status: int
    data: dict = field(repr=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def twitch_http(method, url, *, headers=None, form=None, json_body=None):
    """Pinned HTTPS destinations, no ambient proxy, redirect or error disclosure."""
    if url not in {DEVICE_URL, TOKEN_URL, VALIDATE_URL, SUBSCRIPTIONS_URL}:
        raise TwitchAuthError("http_destination_not_allowed")
    if method not in {"GET", "POST"} or (form is not None and json_body is not None):
        raise TwitchAuthError("http_request_invalid")
    request_headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    if json_body is not None:
        data = json.dumps(json_body, allow_nan=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=8)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.code
            if 300 <= status < 400:
                raise TwitchAuthError("http_redirect_rejected")
            raw = response.read(131073)
            if len(raw) > 131072:
                raise TwitchAuthError("http_response_too_large")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise TwitchAuthError("http_response_invalid")
            return HttpResult(status, body)
    except TwitchAuthError:
        raise
    except Exception:
        raise TwitchAuthError("http_request_failed") from None


def _dpapi(data, *, decrypt=False):
    """Encrypt for the current Windows user; never use machine-wide DPAPI."""
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise TwitchAuthError("token_protection_failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


@dataclass(frozen=True)
class DeviceGrant:
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_uri: str = field(repr=False)
    interval: int
    expires_at: float
    expires_in: int
    expected_user_id: str

    def safe_prompt(self):
        # User code and verification URL are intentionally shown only for login.
        return {"user_code": self.user_code, "verification_uri": self.verification_uri,
                "expires_in": self.expires_in, "expected_user_id": self.expected_user_id}


class TwitchAuth:
    """One explicit account/app and new encrypted state file; receive scope only."""
    # The receiver checks every 30 seconds. Refresh early enough to cover that
    # interval and the bounded refresh/validation requests, even within an hour.
    REFRESH_MARGIN_SECONDS = 60

    def __init__(self, client_id, expected_user_id, token_path, *, client_type="public",
                 http=None, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(client_id, str) or re.fullmatch(r"[A-Za-z0-9]{1,128}", client_id) is None:
            raise TwitchAuthError("client_id_invalid")
        if not isinstance(expected_user_id, str) or re.fullmatch(r"[1-9][0-9]{0,19}", expected_user_id) is None:
            raise TwitchAuthError("expected_user_id_invalid")
        if client_type != "public":
            raise TwitchAuthError("confidential_app_requires_separate_secret_authorization")
        self.client_id, self.expected_user_id = client_id, expected_user_id
        self.token_path = Path(token_path).absolute()
        self._http, self._clock, self._sleep = http or twitch_http, clock, sleep
        self._lock = threading.RLock()
        self._bundle = None
        self._identity = None
        self._validated_at = None
        self._expires_at = None

    def __repr__(self):
        return f"TwitchAuth(expected_user_id={self.expected_user_id!r}, loaded={self._bundle is not None})"

    @property
    def identity(self):
        return None if self._identity is None else {**self._identity, "scopes": list(self._identity["scopes"])}

    def _remember_validated(self, bundle, identity):
        now = self._clock()
        self._bundle, self._identity, self._validated_at = bundle, identity, now
        self._expires_at = now + identity["expires_in"]

    def _clear_validation(self):
        self._identity, self._validated_at, self._expires_at = None, None, None

    def _call(self, method, url, **kwargs):
        try:
            result = self._http(method, url, **kwargs)
            if not isinstance(result, HttpResult) or not isinstance(result.data, dict):
                raise TwitchAuthError("http_response_invalid")
            return result
        except TwitchAuthError:
            raise
        except Exception:
            raise TwitchAuthError("http_request_failed") from None

    def begin_device(self):
        response = self._call("POST", DEVICE_URL, form={"client_id": self.client_id, "scopes": "user:read:chat"})
        if response.status != 200:
            raise TwitchAuthError("device_authorization_rejected_check_app_public_type")
        data = response.data
        try:
            uri = urllib.parse.urlsplit(str(data.get("verification_uri", "")))
            if (uri.scheme != "https" or uri.hostname != "www.twitch.tv" or uri.port not in (None, 443)
                    or uri.username or uri.password or uri.path != "/activate" or uri.fragment):
                raise ValueError()
        except Exception:
            raise TwitchAuthError("device_verification_uri_invalid") from None
        interval, expiry = data.get("interval"), data.get("expires_in")
        if (type(interval) is not int or not 1 <= interval <= 60 or type(expiry) is not int or not 1 <= expiry <= 3600
                or not isinstance(data.get("device_code"), str) or not 1 <= len(data["device_code"]) <= 4096
                or not isinstance(data.get("user_code"), str) or not 1 <= len(data["user_code"]) <= 128):
            raise TwitchAuthError("device_response_invalid")
        return DeviceGrant(data["device_code"], data["user_code"], data["verification_uri"],
                           interval, self._clock() + expiry, expiry, self.expected_user_id)

    def poll_device(self, grant, *, on_wait=None, max_wait_seconds=1800):
        if not isinstance(grant, DeviceGrant) or grant.expected_user_id != self.expected_user_id:
            raise TwitchAuthError("device_grant_binding_invalid")
        deadline = min(grant.expires_at, self._clock() + min(max_wait_seconds, 3600))
        interval = grant.interval
        while self._clock() < deadline:
            self._sleep(min(interval, max(0, deadline - self._clock())))
            if self._clock() >= deadline:
                break
            response = self._call("POST", TOKEN_URL, form={"client_id": self.client_id,
                "scopes": "user:read:chat", "device_code": grant.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            if response.status == 200:
                bundle = self._parse_bundle(response.data)
                identity = self._validate_bundle(bundle)
                with self._lock:
                    if self.token_path.exists():
                        raise TwitchAuthError("token_file_exists_use_existing_or_new_path")
                    self._save(bundle, replace=False)
                    self._remember_validated(bundle, identity)
                return self.identity
            error = response.data.get("error") or response.data.get("message")
            if response.status == 400 and error in {"authorization_pending", "slow_down"}:
                if error == "slow_down":
                    interval = min(60, interval + 5)
                if on_wait is not None:
                    on_wait({"status": error, "remaining_seconds": max(0, int(deadline - self._clock()))})
                continue
            raise TwitchAuthError("device_authorization_failed_reauthorize_or_check_app_type")
        raise TwitchAuthError("device_authorization_expired")

    def _parse_bundle(self, data):
        if (not isinstance(data.get("access_token"), str) or not 1 <= len(data["access_token"]) <= 4096
                or not isinstance(data.get("refresh_token"), str) or not 1 <= len(data["refresh_token"]) <= 4096
                or str(data.get("token_type", "")).lower() != "bearer"):
            raise TwitchAuthError("token_response_invalid")
        return {"access_token": data["access_token"], "refresh_token": data["refresh_token"]}

    def _validate_bundle(self, bundle):
        result = self._call("GET", VALIDATE_URL, headers={"Authorization": "OAuth " + bundle["access_token"]})
        if result.status == 401:
            raise TwitchAuthError("token_invalid")
        if result.status != 200:
            raise TwitchAuthError("token_validation_unavailable")
        data = result.data
        scopes = data.get("scopes")
        if (data.get("client_id") != self.client_id or data.get("user_id") != self.expected_user_id
                or not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes)
                or not REQUIRED_SCOPES.issubset(scopes) or type(data.get("expires_in")) is not int
                or data["expires_in"] <= 0):
            raise TwitchAuthError("token_identity_client_or_scope_mismatch")
        login = data.get("login")
        if not isinstance(login, str) or re.fullmatch(r"[A-Za-z0-9_]{1,25}", login) is None:
            raise TwitchAuthError("token_identity_invalid")
        return {"client_id": self.client_id, "user_id": self.expected_user_id, "login": login,
                "scopes": sorted(scopes), "expires_in": data["expires_in"], "validated": True}

    def _check_path(self):
        if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
               for p in (self.token_path, *self.token_path.parents)):
            raise TwitchAuthError("token_path_symlink_rejected")

    def _save(self, bundle, *, replace=True):
        self._check_path()
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({"owner": OWNER, "client_id": self.client_id, "user_id": self.expected_user_id, **bundle}).encode()
        if os.name == "nt":
            payload, protection = _dpapi(raw), "windows-user-dpapi"
        else:
            payload, protection = raw, "posix-mode-0600"
        envelope = json.dumps({"owner": OWNER, "protection": protection, "payload": base64.b64encode(payload).decode()}).encode()
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".twitch-token-", dir=self.token_path.parent)
            with os.fdopen(fd, "wb") as target:
                if os.name != "nt":
                    os.fchmod(target.fileno(), 0o600)
                target.write(envelope)
                target.flush()
                os.fsync(target.fileno())
            if replace:
                os.replace(temporary, self.token_path)
            else:
                # Exclusive creation remains exclusive even if another process
                # creates the destination after the earlier existence check.
                os.link(temporary, self.token_path)
        except Exception:
            raise TwitchAuthError("token_persistence_failed_reauthorize") from None
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _load(self):
        self._check_path()
        try:
            if not self.token_path.is_file() or self.token_path.stat().st_size > 65536:
                raise ValueError()
            if os.name != "nt" and self.token_path.stat().st_mode & 0o077:
                raise ValueError()
            envelope = json.loads(self.token_path.read_bytes())
            if envelope.get("owner") != OWNER:
                raise ValueError()
            protection = "windows-user-dpapi" if os.name == "nt" else "posix-mode-0600"
            if envelope.get("protection") != protection:
                raise ValueError()
            raw = base64.b64decode(envelope["payload"], validate=True)
            data = json.loads(_dpapi(raw, decrypt=True) if os.name == "nt" else raw)
            if (data.get("owner") != OWNER or data.get("client_id") != self.client_id or data.get("user_id") != self.expected_user_id):
                raise ValueError()
            return self._parse_bundle({**data, "token_type": "bearer"})
        except Exception:
            raise TwitchAuthError("token_file_unavailable_or_invalid") from None

    def load_and_validate(self):
        with self._lock:
            self._bundle = self._load()
            self._clear_validation()
            return self.validate_due(force=True)

    def _refresh(self):
        self._clear_validation()
        # An exclusive local lock prevents two processes consuming the one-use refresh token.
        lock_path = self.token_path.with_name(self.token_path.name + ".refresh-lock")
        try:
            lock = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise TwitchAuthError("token_refresh_busy_or_unavailable") from None
        try:
            current = self._load()
            if self._bundle is not None and current["access_token"] != self._bundle["access_token"]:
                candidate = current
            else:
                result = self._call("POST", TOKEN_URL, form={"client_id": self.client_id,
                    "grant_type": "refresh_token", "refresh_token": current["refresh_token"]})
                if result.status != 200:
                    raise TwitchAuthError("token_refresh_failed_reauthorize_check_app_type")
                candidate = self._parse_bundle(result.data)
            identity = self._validate_bundle(candidate)
            self._save(candidate)
            self._remember_validated(candidate, identity)
        finally:
            os.close(lock)
            try:
                lock_path.unlink()
            except OSError:
                pass

    def validate_due(self, *, force=False):
        with self._lock:
            if self._bundle is None:
                raise TwitchAuthError("token_not_loaded")
            if self._expires_at is not None and self._clock() >= self._expires_at - self.REFRESH_MARGIN_SECONDS:
                self._refresh()
                return self.identity
            if not force and self._validated_at is not None and self._clock() - self._validated_at < 3600:
                return self.identity
            try:
                identity = self._validate_bundle(self._bundle)
            except TwitchAuthError as error:
                self._clear_validation()
                if error.code != "token_invalid":
                    raise
                self._refresh()
                return self.identity
            self._remember_validated(self._bundle, identity)
            # Startup validation can reveal a token that is still valid but
            # will expire before the receiver's next periodic check. A failed
            # refresh is not retried as though the original validation failed.
            if self._clock() >= self._expires_at - self.REFRESH_MARGIN_SECONDS:
                self._refresh()
            return self.identity

    def access_token(self):
        with self._lock:
            self.validate_due()
            return self._bundle["access_token"]

    def request_helix(self, method, path, *, json_body=None):
        if method != "POST" or path != "/eventsub/subscriptions":
            raise TwitchAuthError("helix_operation_not_allowed")
        if (not isinstance(json_body, dict) or json_body.get("type") != "channel.chat.message"
                or json_body.get("version") != "1" or json_body.get("condition", {}).get("user_id") != self.expected_user_id
                or json_body.get("transport", {}).get("method") != "websocket"):
            raise TwitchAuthError("subscription_request_invalid")
        with self._lock:
            for attempt in range(2):
                token = self.access_token()
                result = self._call("POST", SUBSCRIPTIONS_URL, headers={"Authorization": "Bearer " + token,
                    "Client-Id": self.client_id}, json_body=json_body)
                if result.status == 202:
                    return result.data
                if result.status == 401 and attempt == 0:
                    self._refresh()
                    continue
                raise TwitchAuthError("eventsub_subscription_rejected")
        raise TwitchAuthError("eventsub_subscription_rejected")
