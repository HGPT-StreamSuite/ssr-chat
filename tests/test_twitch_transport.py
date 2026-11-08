import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from ssr_chat.twitch_transport import TwitchTransport, TwitchTransportError, validate_websocket_url, _envelope, connect_websocket


def envelope(kind, payload, **metadata):
    return json.dumps({"metadata": {"message_id": "delivery1", "message_type": kind,
        "message_timestamp": datetime.now(timezone.utc).isoformat(), **metadata}, "payload": payload})


def welcome(session="s1"):
    return envelope("session_welcome", {"session": {"id": session, "status": "connected", "keepalive_timeout_seconds": 10}})


def notification(session="s1", channel="928757513"):
    return envelope("notification", {"subscription": {"id": "sub1", "status": "enabled",
        "type": "channel.chat.message", "version": "1", "condition": {"broadcaster_user_id": channel, "user_id": "99567238"},
        "transport": {"method": "websocket", "session_id": session}}, "event": {
        "broadcaster_user_id": channel, "chatter_user_id": "928757513", "message_id": "message1", "message": {"text": "!ask test"}}},
        subscription_type="channel.chat.message", subscription_version="1")


class FakeSocket:
    def __init__(self, messages): self.messages, self.closed, self.reads = list(messages), False, 0
    async def recv(self):
        self.reads += 1
        await asyncio.sleep(0)
        if self.messages:
            item = self.messages.pop(0)
            if isinstance(item, Exception): raise item
            return item
        await asyncio.Future()
    async def close(self): self.closed = True


class FakeAuth:
    client_id = "client123"
    expected_user_id = "99567238"
    def __init__(self): self.identity = None; self.calls = []; self.validations = 0
    def load_and_validate(self): self.identity = {"user_id": self.expected_user_id}; self.validations += 1; return self.identity
    def validate_due(self, **kwargs): self.validations += 1; return self.identity
    def request_helix(self, method, path, *, json_body):
        self.calls.append((method, path, json_body))
        return {"data": [{**json_body, "id": "sub1", "status": "enabled"}]}


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, messages, second=None):
        sockets = [FakeSocket(messages)] + ([FakeSocket(second)] if second is not None else [])
        opened = []
        async def connect(url):
            opened.append(url)
            return sockets[len(opened)-1]
        auth = FakeAuth()
        return TwitchTransport(auth, "928757513", websocket_connect=connect), auth, sockets, opened

    async def test_authenticated_binding_callback_stop_and_no_send(self):
        receiver, auth, sockets, opened = await self.make([welcome(), notification()])
        received = []
        async def callback(event, binding): received.append((event, binding)); return False
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["reason"], "callback_stop")
        self.assertEqual(result["callbacks_completed"], 1)
        self.assertTrue(received[0][1]["transport_authenticated"])
        self.assertEqual(received[0][1]["reader_user_id"], "99567238")
        self.assertEqual(received[0][1]["subscription_id"], "sub1")
        self.assertEqual(auth.calls[0][1], "/eventsub/subscriptions")
        self.assertEqual(result["public_messages_sent"], 0)
        self.assertTrue(all(s.closed for s in sockets))

    async def test_heartbeats_continue_during_slow_callback(self):
        keep = envelope("session_keepalive", {})
        receiver, _, sockets, _ = await self.make([welcome(), notification(), keep, keep, keep])
        async def callback(event, binding):
            await asyncio.sleep(.05)
            self.assertGreaterEqual(sockets[0].reads, 5)
            return False
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["keepalives"], 3)

    async def test_wrong_channel_rejected_before_callback(self):
        receiver, _, sockets, _ = await self.make([welcome(), notification(channel="123")])
        calls = []
        async def callback(event, binding): calls.append(event)
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["reason"], "notification_binding_mismatch")
        self.assertEqual(calls, [])
        self.assertTrue(sockets[0].closed)

    async def test_revocation_stops(self):
        receiver, _, _, _ = await self.make([welcome(), envelope("revocation", {"subscription": {"status": "authorization_revoked"}})])
        async def callback(event, binding): self.fail("callback must not run")
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["reason"], "eventsub_authorization_revoked")

    async def test_bounded_queue_overflow_fails_closed(self):
        receiver, _, _, _ = await self.make([welcome(), notification(), notification(), notification(), notification()])
        async def callback(event, binding): await asyncio.sleep(10)
        result = await receiver.run(callback, duration_seconds=2, queue_size=1)
        self.assertEqual(result["reason"], "event_queue_full")
        self.assertEqual(result["callbacks_completed"], 0)

    async def test_duplicate_events_are_forwarded_for_durable_parent_ledger(self):
        receiver, _, _, _ = await self.make([welcome(), notification(), notification()])
        calls = []
        async def callback(event, binding): calls.append(event["payload"]["event"]["message_id"])
        result = await receiver.run(callback, duration_seconds=2, max_events=2)
        self.assertEqual(calls, ["message1", "message1"])
        self.assertEqual(result["reason"], "event_limit")

    async def test_reconnect_preserves_subscription_and_closes_old_after_welcome(self):
        reconnect = envelope("session_reconnect", {"session": {"id": "s1", "status": "reconnecting",
            "reconnect_url": "wss://eventsub.wss.twitch.tv/ws?reconnect=opaque"}})
        receiver, auth, sockets, opened = await self.make([welcome(), reconnect], [welcome("s2"), notification("s2")])
        async def callback(event, binding):
            self.assertEqual(binding["session_id"], "s2")
            self.assertTrue(sockets[0].closed)
            return False
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["reason"], "callback_stop")
        self.assertEqual(len(auth.calls), 1)
        self.assertEqual(result["reconnects"], 1)
        self.assertEqual(opened[1], "wss://eventsub.wss.twitch.tv/ws?reconnect=opaque")

    async def test_network_failure_redacts_and_respects_reconnect_cap(self):
        receiver, _, _, _ = await self.make([welcome(), RuntimeError("SECRET-TOKEN raw data")])
        async def callback(event, binding): pass
        result = await receiver.run(callback, duration_seconds=2, max_reconnects=0)
        self.assertEqual(result["reason"], "connection_lost_reconnect_limit")
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_old_socket_close_during_reconnect_keeps_new_welcome(self):
        reconnect = envelope("session_reconnect", {"session": {"id": "s1", "status": "reconnecting",
            "reconnect_url": "wss://eventsub.wss.twitch.tv/ws?reconnect=opaque"}})
        new_welcome_started = asyncio.Event()

        class OldSocket(FakeSocket):
            async def recv(self):
                if self.messages:
                    return await super().recv()
                await new_welcome_started.wait()
                raise RuntimeError("old connection closed during handoff")

        class NewSocket(FakeSocket):
            async def recv(self):
                if self.reads == 0:
                    new_welcome_started.set()
                    # Let the old read fail before the opening task returns.
                    await asyncio.sleep(0)
                return await super().recv()

        sockets = [OldSocket([welcome(), reconnect]), NewSocket([welcome("s2"), notification("s2")])]
        opened = []
        async def connect(url):
            opened.append(url)
            return sockets[len(opened) - 1]
        auth = FakeAuth()
        receiver = TwitchTransport(auth, "928757513", websocket_connect=connect)
        received = []
        async def callback(event, binding):
            received.append(binding)
            return False
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "callback_stop")
        self.assertEqual(received[0]["session_id"], "s2")
        self.assertEqual(result["subscriptions_created"], 1)
        self.assertTrue(all(socket.closed for socket in sockets))

    async def test_closed_old_socket_does_not_hide_failed_new_welcome(self):
        reconnect = envelope("session_reconnect", {"session": {"id": "s1", "status": "reconnecting",
            "reconnect_url": "wss://eventsub.wss.twitch.tv/ws?reconnect=opaque"}})
        receiver, _, sockets, _ = await self.make(
            [welcome(), reconnect, RuntimeError("old closed")],
            [envelope("session_welcome", {"session": {"id": "s2", "status": "invalid"}})])
        async def callback(event, binding):
            self.fail("callback must not run")
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "session_welcome_invalid")
        self.assertTrue(all(socket.closed for socket in sockets))

    async def test_duration_cap_and_cleanup(self):
        receiver, _, sockets, _ = await self.make([welcome()])
        async def callback(event, binding): pass
        result = await receiver.run(callback, duration_seconds=1)
        self.assertEqual(result["reason"], "duration_limit")
        self.assertTrue(sockets[0].closed)

    async def test_callback_error_redacted(self):
        receiver, _, _, _ = await self.make([welcome(), notification()])
        async def callback(event, binding): raise RuntimeError("SECRET prompt")
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["reason"], "event_callback_failed")
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_invalid_subscription_response_stops(self):
        receiver, auth, _, _ = await self.make([welcome()])
        auth.request_helix = lambda *a, **k: {"data": []}
        async def callback(event, binding): pass
        result = await receiver.run(callback, duration_seconds=2)
        self.assertEqual(result["reason"], "subscription_response_invalid")

    async def test_ready_status_only_after_verified_enabled_subscription(self):
        receiver, auth, _, _ = await self.make([welcome(), notification()])
        statuses = []
        receiver._on_status = statuses.append
        async def callback(event, binding):
            self.assertEqual(statuses[0]["event"], "subscribed")
            self.assertEqual(statuses[0]["binding"], binding)
            return False
        await receiver.run(callback, duration_seconds=2)
        self.assertEqual([s["event"] for s in statuses], ["subscribed", "stopped"])
        bad, bad_auth, _, _ = await self.make([welcome()])
        bad_statuses = []
        bad._on_status = bad_statuses.append
        bad_auth.request_helix = lambda *a, **k: {"data": [{"status": "pending"}]}
        await bad.run(callback, duration_seconds=2)
        self.assertEqual([s["event"] for s in bad_statuses], ["stopped"])

    def test_destination_and_stale_envelope_rejections(self):
        for url in ("ws://eventsub.wss.twitch.tv/ws", "wss://evil.test/ws", "wss://eventsub.wss.twitch.tv.evil/ws",
                    "wss://secret@eventsub.wss.twitch.tv/ws", "wss://eventsub.wss.twitch.tv:444/ws"):
            with self.subTest(url=url), self.assertRaises(TwitchTransportError): validate_websocket_url(url)
        stale = json.loads(welcome())
        stale["metadata"]["message_timestamp"] = "2020-01-01T00:00:00Z"
        with self.assertRaisesRegex(TwitchTransportError, "stale"): _envelope(json.dumps(stale))

    async def test_default_factory_disables_proxy_ping_and_redirects(self):
        captured = {}
        socket = object()
        class ConnectStub:
            def __init__(self, url, **kwargs):
                captured.update(url=url, kwargs=kwargs, connector=self)
            def __await__(self):
                async def finish(): return socket
                return finish().__await__()
        with patch("websockets.asyncio.client.connect", ConnectStub):
            result = await connect_websocket("wss://eventsub.wss.twitch.tv/ws")
        self.assertIs(result, socket)
        self.assertIsNone(captured["kwargs"]["proxy"])
        self.assertIsNone(captured["kwargs"]["ping_interval"])
        self.assertEqual(captured["kwargs"]["max_queue"], 16)
        rejected = captured["connector"].process_redirect(RuntimeError("secret-location"))
        self.assertIsInstance(rejected, TwitchTransportError)
        self.assertNotIn("secret-location", str(rejected))


if __name__ == "__main__": unittest.main()
