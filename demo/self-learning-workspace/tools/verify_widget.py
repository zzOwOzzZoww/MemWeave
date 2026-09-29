from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def verify(path: Path) -> None:
    pairs = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=list)
    keys = [key for key, _ in pairs]
    expected_keys = ["name", "mode", "retry", "signature"]
    if keys != expected_keys:
        raise ValueError(f"field order must be {expected_keys}, got {keys}")
    data = dict(pairs)
    if not isinstance(data["name"], str) or data["name"] != data["name"].lower():
        raise ValueError("name must be lowercase")
    if data["mode"] not in {"safe", "fast"}:
        raise ValueError("mode must be safe or fast")
    if not isinstance(data["retry"], int) or not 0 <= data["retry"] <= 9:
        raise ValueError("retry must be an integer from 0 to 9")
    raw = f"{data['name']}|{data['mode']}|{data['retry']}"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if data["signature"] != expected:
        raise ValueError(f"signature mismatch: expected {expected}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_widget.py <widget-file>")
    try:
        verify(Path(sys.argv[1]))
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("PASS")
