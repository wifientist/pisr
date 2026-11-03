"""
Settings spelt with the pre-rename PISR_ prefix still take effect.

WHY THIS EXISTS. The tool was renamed from PISR to PRISM, and every setting
with it. Production's env file lives outside the repository and still says
PISR_AUTH_MODE=accounts, PISR_TRUSTED_PROXY_IPS=…; if those silently stopped
being read, the next deploy would come up in passphrase mode with no trusted
proxies — an outage that looks like a config fault and is not one. These pin
the fallback, its precedence, and its reporting.

RUNS WITHOUT PYTEST, like the other suites:

    docker compose -f docker-compose.dev.yml run --rm --no-deps \\
      -v "$PWD:/repo" backend python /repo/api/tests/test_legacy_names.py
"""

import os
import sys
import tempfile
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

for _n, _v in (("R1_TENANT_ID", "t"), ("R1_CLIENT_ID", "c"), ("R1_SHARED_SECRET", "s")):
    os.environ.setdefault(_n, _v)
os.environ.setdefault("PRISM_AUTH_PASSPHRASE", "test-passphrase-123")

import config  # noqa: E402

_NAMES = ("PRISM_ZZ_TEST", "PISR_ZZ_TEST", "PRISM_ZZ_TEST_FILE", "PISR_ZZ_TEST_FILE")


def _clean():
    for n in _NAMES:
        os.environ.pop(n, None)
    config.LEGACY_USED.discard("PISR_ZZ_TEST")


def test_legacy_name_is_read_when_the_new_one_is_absent():
    _clean()
    os.environ["PISR_ZZ_TEST"] = "old"
    assert config._env("PRISM_ZZ_TEST") == "old"
    assert "PISR_ZZ_TEST" in config.LEGACY_USED, "a legacy value used must be reported"


def test_new_name_wins_over_legacy():
    _clean()
    os.environ["PISR_ZZ_TEST"] = "old"
    os.environ["PRISM_ZZ_TEST"] = "new"
    assert config._env("PRISM_ZZ_TEST") == "new"
    assert "PISR_ZZ_TEST" not in config.LEGACY_USED, \
        "a legacy name that supplied nothing must not be reported as in use"


def test_legacy_file_form_is_honoured():
    _clean()
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write("from-file\n")
    os.environ["PISR_ZZ_TEST_FILE"] = fh.name
    try:
        assert config._env("PRISM_ZZ_TEST") == "from-file"
    finally:
        Path(fh.name).unlink()


def test_new_plain_value_beats_a_legacy_file():
    """New spelling first, whatever its form: someone who has written the
    PRISM_ line has decided, and a leftover PISR_ secret file must not
    override it."""
    _clean()
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write("legacy-file")
    os.environ["PISR_ZZ_TEST_FILE"] = fh.name
    os.environ["PRISM_ZZ_TEST"] = "new"
    try:
        assert config._env("PRISM_ZZ_TEST") == "new"
    finally:
        Path(fh.name).unlink()


def test_non_prism_names_have_no_fallback():
    """R1_* and friends were never PISR_-prefixed; nothing to fall back to."""
    _clean()
    os.environ["PISR_ZZ_TEST"] = "old"
    assert config._env("ZZ_TEST") == ""


def test_both_unset_is_empty():
    _clean()
    assert config._env("PRISM_ZZ_TEST") == ""


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}\n        {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
        finally:
            _clean()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
