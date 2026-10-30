"""Fail unless every module of the integration has at least 95% line coverage (Silver rule test-coverage).

Run after: python -m pytest -q --cov=custom_components.ddp --cov-report=json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

THRESHOLD = 95.0


def main(report: Path) -> int:
    files = json.loads(report.read_text())["files"]
    below = {
        name: f["summary"]["percent_covered"]
        for name, f in files.items()
        if f["summary"]["percent_covered"] < THRESHOLD
    }
    for name, percent in sorted(below.items()):
        print(f"{name}: {percent:.1f}% < {THRESHOLD:.0f}%")
    print(f"{len(files) - len(below)}/{len(files)} modules at or above {THRESHOLD:.0f}%")
    return 1 if below else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")))
