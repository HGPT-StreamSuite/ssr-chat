"""Bounded, opt-in EventSub receiver. This module has no chat-send operation."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import json
import logging
import re
from urllib.parse import urlsplit

from .twitch_auth import TwitchAuthError

WEBSOCKET_URL = "wss://eventsub.wss.twitch.tv/ws"


class TwitchTransportError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_websocket_url(url):
    try:
        parsed = urlsplit(url)
        if (not isinstance(url, str) or len(url) > 8192 or parsed.scheme != "wss"
                or parsed.hostname != "eventsub.wss.twitch.tv" or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment or parsed.path not in ("", "/", "/ws")):
            raise ValueError()
    except Exception:
        raise TwitchTransportError("websocket_destination_invalid") from None
    return url


async def connect_websocket(url):
    validate_websocket_url(url)
    from websockets.asyncio.client import connect
    # websockets normally follows HTTP redirects. Pinning the initial URL alone
    # is insufficient. The selected 17.3 release exposes this overridable method.
    class NoRedirectConnect(connect):
        def process_redirect(self, exc):
            return TwitchTransportError("websocket_redirect_rejected")
    logger = logging.Logger("ssr-chat-twitch-websocket", level=logging.CRITICAL)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    try:
        return await NoRedirectConnect(url, proxy=None, ping_interval=None, compression=None,
            open_timeout=8, close_timeout=2, max_size=262144, max_queue=16, logger=logger)
    except Exception:
        raise TwitchTransportError("websocket_connect_failed") from None


def _envelope(raw):
    try:
        if not isinstance(raw, (str, bytes)) or len(raw) > 262144:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("metadata"), dict) or not isinstance(result.get("payload"), dict):
            raise ValueError()
        metadata = result["metadata"]
        if not isinstance(metadata.get("message_id"), str) or not 1 <= len(metadata["message_id"]) <= 128:
            raise ValueError()
        stamp = datetime.fromisoformat(metadata["message_timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or not -30 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 600:
            raise ValueError()
        return result
    except Exception:
        raise TwitchTransportError("websocket_envelope_invalid_or_stale") from None


class TwitchTransport:
    def __init__(self, auth, broadcaster_id, *, websocket_connect=None, on_status=None):
        if not isinstance(broadcaster_id, str) or re.fullmatch(r"[1-9][0-9]{0,19}", broadcaster_id) is None:
            raise TwitchTransportError("broadcaster_id_invalid")
        self.auth, self.broadcaster_id = auth, broadcaster_id
        self._connect = websocket_connect or connect_websocket
        self._on_status = on_status
        self._running = False

    def _binding(self, session_id, subscription_id):
        return {"client_id": self.auth.client_id, "reader_user_id": self.auth.expected_user_id,
                "broadcaster_user_id": self.broadcaster_id, "subscription_id": subscription_id,
                "session_id": session_id, "transport_authenticated": True}

    async def _open(self, url):
        validate_websocket_url(url)
        try:
            return await asyncio.wait_for(self._connect(url), timeout=9)
        except TwitchTransportError:
            raise
        except Exception:
            raise TwitchTransportError("websocket_connect_failed") from None

    async def _welcome(self, socket):
        try:
            data = _envelope(await asyncio.wait_for(socket.recv(), timeout=10))
            session = data["payload"].get("session", {})
            keepalive = session.get("keepalive_timeout_seconds")
            if (data["metadata"].get("message_type") != "session_welcome" or session.get("status") != "connected"
                    or not isinstance(session.get("id"), str) or not 1 <= len(session["id"]) <= 512
                    or type(keepalive) is not int or not 1 <= keepalive <= 600):
                raise TwitchTransportError("session_welcome_invalid")
            return session["id"], keepalive
        except TwitchTransportError:
            raise
        except Exception:
            raise TwitchTransportError("session_welcome_failed") from None

    async def _subscribe(self, session_id):
        body = {"type": "channel.chat.message", "version": "1", "condition": {
            "broadcaster_user_id": self.broadcaster_id, "user_id": self.auth.expected_user_id},
            "transport": {"method": "websocket", "session_id": session_id}}
        data = await asyncio.to_thread(self.auth.request_helix, "POST", "/eventsub/subscriptions", json_body=body)
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != 1:
            raise TwitchTransportError("subscription_response_invalid")
        sub = rows[0]
        if (not isinstance(sub, dict) or sub.get("status") != "enabled" or sub.get("type") != body["type"]
                or sub.get("version") != "1" or sub.get("condition") != body["condition"]
                or sub.get("transport", {}).get("session_id") != session_id
                or sub.get("transport", {}).get("method") != "websocket"
                or not isinstance(sub.get("id"), str) or not 1 <= len(sub["id"]) <= 128):
            raise TwitchTransportError("subscription_response_binding_invalid")
        return sub["id"]

    def _check_notification(self, envelope, binding):
        metadata, payload = envelope["metadata"], envelope["payload"]
        sub, event = payload.get("subscription"), payload.get("event")
        if not isinstance(sub, dict) or not isinstance(event, dict):
            raise TwitchTransportError("notification_invalid")
        if (metadata.get("subscription_type") != "channel.chat.message" or metadata.get("subscription_version") != "1"
                or sub.get("type") != "channel.chat.message" or sub.get("version") != "1"
                or sub.get("id") != binding["subscription_id"] or sub.get("status") != "enabled"
                or sub.get("condition") != {"broadcaster_user_id": self.broadcaster_id, "user_id": self.auth.expected_user_id}
                or sub.get("transport", {}).get("method") != "websocket"
                or sub.get("transport", {}).get("session_id") != binding["session_id"]
                or event.get("broadcaster_user_id") != self.broadcaster_id):
            raise TwitchTransportError("notification_binding_mismatch")

    async def run(self, on_event, *, duration_seconds=600, max_events=20, queue_size=16,
                  callback_timeout_seconds=180, max_reconnects=3):
        """Async callback(envelope,binding); return False to stop after that callback.

        A separate consumer prevents model latency from blocking heartbeat reads.
        Callbacks must offload blocking model work. Duplicate notifications are
        intentionally passed to the caller's durable ledger. No event is a grant
        to invoke a model: the caller must enforce tester/scope/prefix policy.
        """
        if (self._running or not callable(on_event) or not 1 <= duration_seconds <= 3600
                or type(max_events) is not int or not 1 <= max_events <= 1000
                or type(queue_size) is not int or not 1 <= queue_size <= 100
                or not 1 <= callback_timeout_seconds <= 600 or type(max_reconnects) is not int or not 0 <= max_reconnects <= 10):
            raise TwitchTransportError("receiver_limits_invalid_or_already_running")
        self._running = True
        summary = {"status": "stopped", "reason": None, "transport_authenticated": False,
                   "notifications_received": 0, "callbacks_completed": 0, "keepalives": 0,
                   "reconnects": 0, "subscriptions_created": 0, "public_output_enabled": False,
                   "public_messages_sent": 0, "connection_gaps": 0}
        queue = asyncio.Queue(maxsize=queue_size)
        stop = asyncio.Event()
        sockets = set()
        deadline = asyncio.get_running_loop().time() + duration_seconds
        tasks = []

        async def emit_status(event, **details):
            if self._on_status is not None:
                try:
                    result = self._on_status({"event": event, "public_output_enabled": False, **details})
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=2)
                except Exception:
                    raise TwitchTransportError("status_sink_failed") from None

        def fail(code):
            if summary["reason"] is None:
                summary.update(status="failed", reason=code)
            stop.set()

        async def close(socket):
            if socket in sockets:
                sockets.discard(socket)
                try:
                    await asyncio.wait_for(socket.close(), timeout=3)
                except Exception:
                    pass

        async def open_and_welcome(url):
            socket = await self._open(url)
            sockets.add(socket)
            try:
                session, keepalive = await self._welcome(socket)
                return socket, session, keepalive
            except BaseException:
                await close(socket)
                raise

        async def dispatch(data, binding):
            kind = data["metadata"].get("message_type")
            if kind == "notification":
                self._check_notification(data, binding)
                if summary["notifications_received"] >= max_events:
                    return "cap"
                try:
                    queue.put_nowait((deepcopy(data), dict(binding)))
                except asyncio.QueueFull:
                    raise TwitchTransportError("event_queue_full") from None
                summary["notifications_received"] += 1
                return "cap" if summary["notifications_received"] >= max_events else None
            if kind == "session_keepalive":
                summary["keepalives"] += 1
                return None
            if kind == "revocation":
                raise TwitchTransportError("eventsub_authorization_revoked")
            if kind == "session_reconnect":
                session = data["payload"].get("session", {})
                if session.get("id") != binding["session_id"] or session.get("status") != "reconnecting":
                    raise TwitchTransportError("session_reconnect_invalid")
                return validate_websocket_url(session.get("reconnect_url"))
            raise TwitchTransportError("websocket_message_type_unexpected")

        async def receive():
            socket = None
            try:
                socket, session, keepalive = await open_and_welcome(WEBSOCKET_URL)
                subscription = await self._subscribe(session)
                summary["subscriptions_created"] += 1
                summary["transport_authenticated"] = True
                binding = self._binding(session, subscription)
                await emit_status("subscribed", binding=dict(binding), subscription_status="enabled")
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=keepalive + 1)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if summary["reconnects"] >= max_reconnects:
                            raise TwitchTransportError("connection_lost_reconnect_limit") from None
                        # A fresh session after a network gap requires re-subscription.
                        summary["reconnects"] += 1
                        summary["connection_gaps"] += 1
                        await emit_status("connection_gap", reconnect_attempt=summary["reconnects"])
                        await close(socket)
                        await asyncio.sleep(min(2 ** (summary["reconnects"] - 1), 5))
                        socket, session, keepalive = await open_and_welcome(WEBSOCKET_URL)
                        subscription = await self._subscribe(session)
                        summary["subscriptions_created"] += 1
                        binding = self._binding(session, subscription)
                        await emit_status("subscribed", binding=dict(binding), subscription_status="enabled")
                        continue
                    action = await dispatch(_envelope(raw), binding)
                    if action == "cap":
                        await queue.join()
                        if not stop.is_set():
                            summary["reason"] = "event_limit"
                            stop.set()
                        return
                    if action is not None:
                        if summary["reconnects"] >= max_reconnects:
                            raise TwitchTransportError("reconnect_limit")
                        summary["reconnects"] += 1
                        # Keep the old session open and consume its events during handoff.
                        opening = asyncio.create_task(open_and_welcome(action))
                        pending = asyncio.create_task(socket.recv())
                        try:
                            while not opening.done():
                                done, _ = await asyncio.wait({opening, pending}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
                                if not done:
                                    raise TwitchTransportError("reconnect_welcome_timeout")
                                if pending in done:
                                    try:
                                        prior_raw = pending.result()
                                    except Exception:
                                        # The old connection may close as the new
                                        # Welcome arrives. Finish the already bounded
                                        # handoff instead of discarding its new socket.
                                        pending = None
                                        break
                                    prior = await dispatch(_envelope(prior_raw), binding)
                                    if prior is not None:
                                        raise TwitchTransportError("reconnect_handoff_interrupted")
                                    pending = asyncio.create_task(socket.recv())
                            new_socket, new_session, new_keepalive = await opening
                            if pending is not None:
                                if pending.done():
                                    try:
                                        prior_raw = pending.result()
                                    except Exception:
                                        pass
                                    else:
                                        prior = await dispatch(_envelope(prior_raw), binding)
                                        if prior is not None:
                                            raise TwitchTransportError("reconnect_handoff_interrupted")
                                pending.cancel()
                                await asyncio.gather(pending, return_exceptions=True)
                            await close(socket)
                            socket, session, keepalive = new_socket, new_session, new_keepalive
                            binding = self._binding(session, subscription)
                            await emit_status("reconnected", binding=dict(binding), subscription_status="enabled")
                        finally:
                            children = [child for child in (opening, pending) if child is not None]
                            for child in children:
                                if not child.done():
                                    child.cancel()
                            await asyncio.gather(*children, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except (TwitchTransportError, TwitchAuthError) as error:
                fail(error.code)
            except Exception:
                fail("receiver_failed")
            finally:
                if socket is not None:
                    await close(socket)

        async def consume():
            try:
                while not stop.is_set():
                    item, binding = await queue.get()
                    try:
                        callback = on_event(item, binding)
                        if not inspect.isawaitable(callback):
                            raise TwitchTransportError("async_event_callback_required")
                        keep_going = await asyncio.wait_for(callback, timeout=callback_timeout_seconds)
                        summary["callbacks_completed"] += 1
                        if keep_going is False:
                            summary["reason"] = "callback_stop"
                            stop.set()
                    finally:
                        queue.task_done()
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                fail("event_callback_timeout")
            except Exception:
                fail("event_callback_failed")

        async def validate_session():
            try:
                while not stop.is_set():
                    await asyncio.sleep(30)
                    await asyncio.to_thread(self.auth.validate_due)
            except asyncio.CancelledError:
                raise
            except TwitchAuthError as error:
                fail(error.code)
            except Exception:
                fail("token_validation_failed")

        try:
            if self.auth.identity is None:
                await asyncio.to_thread(self.auth.load_and_validate)
            else:
                await asyncio.to_thread(self.auth.validate_due, force=True)
            tasks = [asyncio.create_task(receive()), asyncio.create_task(consume()), asyncio.create_task(validate_session())]
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(stop.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                summary["reason"] = "duration_limit"
                stop.set()
        except TwitchAuthError as error:
            fail(error.code)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(close(socket) for socket in tuple(sockets)), return_exceptions=True)
            self._running = False
            try:
                await emit_status("stopped", summary=dict(summary))
            except TwitchTransportError:
                summary["status_report_failed"] = True
        return summary
