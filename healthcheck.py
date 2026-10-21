"""Docker HEALTHCHECK probe: exit 0 if /ready answers 200, else 1.

Standard library only, and deliberately does NOT import config (which would
need DISCORD_TOKEN etc. just to run a probe).
"""
import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("HEALTH_PORT", "8080").strip() or "8080"
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=4) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except (urllib.error.URLError, OSError):
    sys.exit(1)
