"""Start a real API process on an available loopback port and verify liveness."""

import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]

process = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("API process exited before startup")
        try:
            with urlopen(f"http://127.0.0.1:{port}/health/live", timeout=1) as response:
                assert response.status == 200
                assert response.headers["X-Request-ID"]
                print("PASS: real Uvicorn process starts and responds to liveness")
                break
        except (URLError, TimeoutError):
            time.sleep(0.2)
    else:
        raise RuntimeError("API startup timed out")
finally:
    process.terminate()
    process.wait(timeout=10)
