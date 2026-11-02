#!/usr/bin/env python3
"""
Print the access surface as JSON. A shim over `api/access_surface.py`, which is
where the derivation lives and which the admin capability matrix imports.

    docker compose -f docker-compose.dev.yml run --rm --no-deps \\
      -v "$PWD:/repo" -w /repo/api backend python scripts/access_surface.py

STDOUT IS JSON AND NOTHING ELSE — importing the app announces itself on stdout,
which is right for a container and fatal for a machine-readable oracle. The
module redirects that to stderr; do not print anything else here.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from access_surface import surface  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(surface(), indent=2))
