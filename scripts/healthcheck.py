"""Container healthcheck: exit 0 when the service answers /health."""

import os
import sys
import urllib.request

port = os.environ.get("PORT", "8000")
url = f"http://127.0.0.1:{port}/health"
try:
    with urllib.request.urlopen(url, timeout=2) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
