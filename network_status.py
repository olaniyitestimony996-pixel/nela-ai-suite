"""
Tracks whether this machine is actually online, and lets the user force
Nela offline manually (a privacy switch, not just a status light).
Light on purpose: one tiny socket probe every 15 seconds.
"""

import socket
import threading
import time

_PROBES = (("1.1.1.1", 53), ("8.8.8.8", 53))  # two hosts so one blocked host doesn't read as "offline"


class NetworkStatus:
    def __init__(self, check_interval_seconds: int = 15):
        self.check_interval = check_interval_seconds
        self._actually_online = False
        self._manual_offline_override = False
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _check_once(self) -> bool:
        for host in _PROBES:
            try:
                socket.create_connection(host, timeout=1.5).close()
                return True
            except OSError:
                continue
        return False

    def _loop(self):
        while True:
            result = self._check_once()
            with self._lock:
                self._actually_online = result
            time.sleep(self.check_interval)

    def set_manual_override(self, force_offline: bool):
        with self._lock:
            self._manual_offline_override = force_offline

    @property
    def is_online(self) -> bool:
        with self._lock:
            return self._actually_online and not self._manual_offline_override

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "actually_online": self._actually_online,
                "manual_offline_override": self._manual_offline_override,
                "effective_online": self._actually_online and not self._manual_offline_override,
            }
