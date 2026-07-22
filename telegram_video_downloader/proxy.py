from __future__ import annotations

import socket


def proxy_available(host: str = "127.0.0.1", port: int = 7890, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
