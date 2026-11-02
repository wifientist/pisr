#!/usr/bin/env python3
"""
Run every backend suite and report once.

    docker compose -f docker-compose.dev.yml run --rm --no-deps \
      -v "$PWD:/repo" -w /repo/api backend python tests/run_all.py

These suites deliberately do not use pytest — each is a plain script with a
`__main__` that counts its own failures — so this is the thing that would
otherwise be a shell loop everybody writes slightly differently. It exits
non-zero if any suite does, which is what a hook or a pre-deploy check wants.

NEEDS THE APP'S ENVIRONMENT, so run it in the container rather than on the
host. `--no-deps` keeps it from starting a second backend beside whatever is
already up.
"""

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def main() -> int:
    failed = []
    for path in sorted(TESTS.glob("test_*.py")):
        result = subprocess.run(
            [sys.executable, str(path)], capture_output=True, text=True)
        # Each suite prints "  ok <name>" per test and "N failure(s)" at the
        # end; the tail is all that is worth showing unless something broke.
        summary = (result.stdout.strip().splitlines() or ["(no output)"])[-1]
        ok = result.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'}  {path.name:26} {summary}")
        if not ok:
            failed.append((path.name, result.stdout, result.stderr))

    if failed:
        print(f"\n{len(failed)} suite(s) failed\n")
        for name, out, err in failed:
            print(f"── {name} " + "─" * (60 - len(name)))
            for line in out.splitlines():
                if line.strip().startswith(("FAIL", "ERROR")) or "Error" in line:
                    print(line)
            if err.strip():
                print(err.strip()[-800:])
    else:
        print("\nall suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
