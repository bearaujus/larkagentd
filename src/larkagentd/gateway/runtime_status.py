"""Truthful process status; connection state is reported by the websocket adapter."""

import logging
import threading
import time

from ..core.diagnostics import emit


class RuntimeStatus:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.role = "starting"
        self.websocket = "disconnected"
        self.workers = 0
        self.stopping = False

    def connection(self, connected, error_type=None):
        with self.lock:
            new = "connected" if connected else "reconnecting"
            changed = self.websocket != new
            self.websocket = new
            stopping = self.stopping
        if changed and not stopping:
            emit(
                "lark.connected" if connected else "lark.reconnecting",
                logging.INFO if connected else logging.WARNING,
                error_type=error_type,
            )
            if connected:
                emit(
                    "daemon.ready",
                    role="receiver",
                    mode="hermes",
                    next_step="Send_a_message_to_the_bot",
                )

    def snapshot(self, database_ok=None):
        with self.lock:
            return dict(
                database="unknown"
                if database_ok is None
                else "available"
                if database_ok
                else "unavailable",
                role=self.role,
                websocket=self.websocket,
                workers=self.workers,
                uptime_seconds=int(time.monotonic() - self.started),
                ready=database_ok is True
                and not self.stopping
                and self.workers > 0
                and (
                    self.role == "standby"
                    or (self.role == "receiver" and self.websocket == "connected")
                ),
            )
