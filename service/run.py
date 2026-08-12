"""Start the service.

    python run.py

Environment:
    OPENROUTER_API_KEY   optional; without it the service does retrieval only
    JOBKB_ROOT           where the OKF bundle lives (default ~/.jobkb)
    JOBKB_PORT           default 8765
    JOBKB_TOKEN          optional shared secret the extension must send
"""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn  # noqa: E402

from jobkb.config import settings  # noqa: E402


def port_taken(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":
    # Starting a second copy is the easy mistake: the old one keeps the port and
    # keeps answering, so the extension talks to stale code and nothing looks
    # wrong. Refuse loudly instead.
    if port_taken(settings.host, settings.port):
        print(f"\n  Port {settings.port} is already in use — a copy of the service")
        print("  is probably already running. That copy keeps answering the")
        print("  extension, so starting another one changes nothing.\n")
        print("  Stop the old one first:")
        print("    powershell -c \"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\""
              " | Where-Object CommandLine -like '*run.py*'"
              " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\"\n")
        print(f"  Or check what is answering:  curl http://{settings.host}:{settings.port}/health\n")
        sys.exit(1)

    print(f"knowledge base : {settings.root}")
    print(f"listening on   : http://{settings.host}:{settings.port}")
    print(f"openrouter key : {'set' if settings.has_key else 'NOT SET (retrieval only)'}")
    uvicorn.run("jobkb.api:app", host=settings.host, port=settings.port, log_level="info")
