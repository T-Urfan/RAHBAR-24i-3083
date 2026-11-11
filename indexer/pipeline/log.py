"""One JSON object per line on stdout (section 4.8). The Job's logs are its only record."""

from __future__ import annotations

import json
import time


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}, default=str), flush=True)
