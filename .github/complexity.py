"""Hold every function of the integration to its complexity ceiling.

ruff measures the McCabe complexity of each function (C901, ruff.toml).
A new function may not pass 10; a function already past it when the gate
came in may not grow past the value .github/complexity.json records for
it. The ceilings follow the code down: a function that got simpler fails
the gate until its ceiling comes down in the same commit, one at 10 or
under until its ceiling goes, so a simplified function cannot grow back
unseen. Never raise a ceiling to make a push pass.

    python .github/complexity.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIMIT = 10
ceilings = json.loads((ROOT / ".github" / "complexity.json").read_text(encoding="utf-8"))

report = subprocess.run(
    [sys.executable, "-m", "ruff", "check", "--ignore-noqa", "--exit-zero",
     "--output-format", "json", "custom_components/gtfs2"],
    cwd=ROOT, capture_output=True, text=True, check=True)
measured = {}
for item in json.loads(report.stdout):
    name, value = re.match(r"`(.+)` is too complex \((\d+) > \d+\)", item["message"]).groups()
    key = f"{Path(item['filename']).name} {name}"
    measured[key] = max(int(value), measured.get(key, 0))

failed = False
for key, value in sorted(measured.items()):
    ceiling = ceilings.get(key, LIMIT)
    if value > ceiling:
        failed = True
        print(f"::error::{key}: complexity {value}, over its ceiling of {ceiling}")
    elif value < ceiling:
        failed = True
        print(f"::error::{key}: complexity {value}, lower its ceiling of {ceiling} to it")
for key in sorted(set(ceilings) - set(measured)):
    failed = True
    print(f"::error::{key}: at {LIMIT} or under now, drop its ceiling")
print(f"{len(measured)} functions over {LIMIT}, {len(ceilings)} ceilings")
sys.exit(failed)
