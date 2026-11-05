import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ssr_chat.twitch_auth import (TwitchAuth, TwitchAuthError, HttpResult, DEVICE_URL, TOKEN_URL,
                                 VALIDATE_URL, SUBSCRIPTIONS_URL, twitch_http)


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds


class Http:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception): raise reply
        return reply


def valid(**changes):
    return HttpResult(200, {"client_id": "client123", "user_id": "99567238", "login": "dubheorigin",
                           "scopes": ["user:read:chat"], "expires_in": 3600, **changes})


def token(access="SECRET-ACCESS", refresh="SECRET-REFRESH"):
    return HttpResult(200, {"access_token": access, "refresh_token": refresh, "token_type": "bearer"})


def device(**changes):
    return HttpResult(200, {"device_code": "SECRET-DEVICE", "user_code": "ABCD", "interval": 5,
        "expires_in": 60, "verification_uri": "https://www.twitch.tv/activate?device-code=ABCD", **changes})


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "new-token.bin"
        self.clock = Clock()

    def auth(self, replies):
        http = Http(replies)
        return TwitchAuth("client123", "99567238", self.path, http=http, clock=self.clock, sleep=self.clock.sleep), http

    def login(self, *, expires_in=3600):
        auth, http = self.auth([device(), token(), valid(expires_in=expires_in)])
        grant = auth.begin_device()
        auth.poll_device(grant)
        return auth, http

    def test_device_read_only_scope_and_protected_round_trip(self):
        auth, http = self.login()
        self.assertEqual(http.calls[0][2]["form"]["scopes"], "user:read:chat")
        self.assertEqual(http.calls[1][2]["form"]["scopes"], "user:read:chat")
        self.assertNotIn("SECRET-ACCESS", self.path.read_text())
        self.assertNotIn("SECRET-REFRESH", repr(auth))
        self.assertEqual(auth.identity["user_id"], "99567238")
        other, _ = self.auth([valid()])
        self.assertEqual(other.load_and_validate()["login"], "dubheorigin")
        self.assertEqual(other.access_token(), "SECRET-ACCESS")

    def test_pending_slowdown_and_expiry_are_bounded(self):
        auth, http = self.auth([device(), HttpResult(400, {"message": "authorization_pending"}),
            HttpResult(400, {"error": "slow_down"}), token(), valid()])
        grant = auth.begin_device()
        notices = []
        auth.poll_device(grant, on_wait=notices.append)
        self.assertEqual(self.clock.value, 20)
        self.assertEqual([n["status"] for n in notices], ["authorization_pending", "slow_down"])
        auth2, _ = self.auth([device(expires_in=4)])
        with self.assertRaisesRegex(TwitchAuthError, "device_authorization_expired"):
            auth2.poll_device(auth2.begin_device())

    def test_wrong_identity_client_scope_or_expiry_never_persists(self):
        for changes in ({"user_id": "7"}, {"client_id": "other"}, {"scopes": []}, {"expires_in": 0}):
            with self.subTest(changes=changes):
                auth, _ = self.auth([device(), token(), valid(**changes)])
                with self.assertRaisesRegex(TwitchAuthError, "mismatch"):
                    auth.poll_device(auth.begin_device())
                self.assertFalse(self.path.exists())

    def test_existing_token_is_not_overwritten_by_login(self):
        self.login()
        previous = self.path.read_bytes()
        auth, _ = self.auth([device(), token("OTHER", "OTHER-R"), valid()])
        with self.assertRaisesRegex(TwitchAuthError, "token_file_exists"):
            auth.poll_device(auth.begin_device())
        self.assertEqual(previous, self.path.read_bytes())

    def test_startup_and_hourly_validation(self):
        auth, http = self.login(expires_in=7200)
        for _ in range(4): auth.validate_due()
        self.assertEqual(len(http.calls), 3)
        self.clock.value += 3600
        http.replies.append(valid())
        auth.validate_due()
        self.assertEqual(http.calls[-1][1], VALIDATE_URL)

    def test_short_lifetime_refreshes_before_expiry_without_waiting_an_hour(self):
        auth, http = self.login(expires_in=120)
        self.clock.value += 59
        auth.validate_due()
        self.assertEqual(len(http.calls), 3)
        self.clock.value += 1
        http.replies += [token("EARLY-ACCESS", "EARLY-REFRESH"), valid(expires_in=7200)]
        auth.validate_due()
        self.assertEqual([call[1] for call in http.calls[3:]], [TOKEN_URL, VALIDATE_URL])
        self.assertEqual(auth.access_token(), "EARLY-ACCESS")
        self.assertEqual(auth._load()["refresh_token"], "EARLY-REFRESH")
        self.assertEqual(len(http.calls), 5)
        refresh = http.calls[3][2]["form"]
        self.assertNotIn("client_secret", refresh)
        self.assertNotIn("scopes", refresh)

    def test_startup_near_expiry_refreshes_after_validating_identity(self):
        self.login()
        auth, http = self.auth([valid(expires_in=1), token("STARTUP-ACCESS", "STARTUP-REFRESH"), valid()])
        self.assertTrue(auth.load_and_validate()["validated"])
        self.assertEqual([call[1] for call in http.calls], [VALIDATE_URL, TOKEN_URL, VALIDATE_URL])
        self.assertEqual(auth.access_token(), "STARTUP-ACCESS")
        self.assertEqual(auth._expires_at, self.clock.value + 3600)

    def test_expiry_deadline_refresh_failure_clears_cached_identity(self):
        auth, http = self.login(expires_in=90)
        self.clock.value += 31
        http.replies.append(HttpResult(400, {"message": "SECRET-REFRESH rejected"}))
        with self.assertRaisesRegex(TwitchAuthError, "token_refresh_failed_reauthorize"):
            auth.validate_due()
        self.assertIsNone(auth.identity)
        self.assertIsNone(auth._expires_at)
        self.assertNotIn("SECRET", str(http.calls[-1][0:2]))

    def test_startup_refresh_validation_failure_is_not_refreshed_twice(self):
        self.login()
        auth, http = self.auth([valid(expires_in=1), token("BAD-ACCESS", "ROTATED-REFRESH"), HttpResult(401, {})])
        with self.assertRaisesRegex(TwitchAuthError, "token_invalid"):
            auth.load_and_validate()
        self.assertEqual([call[1] for call in http.calls], [VALIDATE_URL, TOKEN_URL, VALIDATE_URL])
        self.assertIsNone(auth.identity)

    def test_401_rotates_public_refresh_once_and_saves_replacement(self):
        auth, http = self.login()
        http.replies += [HttpResult(401, {}), token("NEW-ACCESS", "NEW-REFRESH"), valid()]
        auth.validate_due(force=True)
        refresh_call = [call for call in http.calls if call[1] == TOKEN_URL][-1]
        self.assertEqual(refresh_call[2]["form"]["refresh_token"], "SECRET-REFRESH")
        self.assertNotIn("client_secret", refresh_call[2]["form"])
        self.assertEqual(auth._load()["refresh_token"], "NEW-REFRESH")
        self.assertFalse(self.path.with_name(self.path.name + ".refresh-lock").exists())

    def test_refresh_failure_is_actionable_and_secret_redacted(self):
        auth, http = self.login()
        http.replies += [HttpResult(401, {}), HttpResult(400, {"message": "SECRET-REFRESH rejected"})]
        with self.assertRaises(TwitchAuthError) as raised:
            auth.validate_due(force=True)
        self.assertIn("reauthorize", str(raised.exception))
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertIsNone(auth.identity)

    def test_transport_exceptions_redact_secrets(self):
        auth, _ = self.auth([RuntimeError("SECRET-ACCESS full header")])
        with self.assertRaises(TwitchAuthError) as raised: auth.begin_device()
        self.assertEqual(str(raised.exception), "http_request_failed")

    def test_wrong_device_uri_and_confidential_app_fail_before_login(self):
        auth, _ = self.auth([device(verification_uri="https://evil.test/activate")])
        with self.assertRaisesRegex(TwitchAuthError, "uri_invalid"): auth.begin_device()
        with self.assertRaisesRegex(TwitchAuthError, "confidential_app_requires"):
            TwitchAuth("client123", "99567238", self.path, client_type="confidential")

    def test_send_api_and_unpinned_destinations_are_blocked_without_http(self):
        auth, http = self.auth([])
        with self.assertRaisesRegex(TwitchAuthError, "not_allowed"):
            auth.request_helix("POST", "/chat/messages", json_body={})
        with self.assertRaisesRegex(TwitchAuthError, "destination_not_allowed"):
            twitch_http("GET", "https://evil.test")
        self.assertEqual(http.calls, [])

    def test_subscription_401_refresh_revalidates_before_retry(self):
        auth, http = self.login()
        http.replies += [HttpResult(401, {}), token("NEXT", "NEXT-R"), valid(), HttpResult(202, {"data": []})]
        result = auth.request_helix("POST", "/eventsub/subscriptions", json_body={"type": "channel.chat.message",
            "version": "1", "condition": {"user_id": "99567238", "broadcaster_user_id": "928757513"},
            "transport": {"method": "websocket", "session_id": "s1"}})
        self.assertEqual(result, {"data": []})
        requests = [c for c in http.calls if c[1] == SUBSCRIPTIONS_URL]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1][2]["headers"]["Authorization"], "Bearer NEXT")

    def test_legacy_plaintext_is_rejected_unchanged(self):
        self.path.write_text(json.dumps({"access_token": "DO-NOT-MIGRATE"}))
        before = self.path.read_bytes()
        auth, http = self.auth([])
        with self.assertRaisesRegex(TwitchAuthError, "token_file_unavailable"):
            auth.load_and_validate()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(http.calls, [])


if __name__ == "__main__": unittest.main()
