"""
Build the static field catalogue the baseline editor browses.

WHY THIS EXISTS. The set of settings an admin can recommend a value for is R1's
config schema, which is static and fully described in the OpenAPI spec — so the
editor should show it directly, not make an admin poll a live venue to discover
which fields exist. This reads the spec and emits a small committed catalogue
(`api/baselines/field_catalogue.json`) keyed exactly like the baseline itself
(`<endpoint>.<dotted-path>`), with each field's type, enum and a readable label.

DEV/BUILD TIME ONLY. The spec is a ~7 MB vendor artefact, gitignored, and NOT in
the runtime image — so this runs by hand when RUCKUS reships the spec, and the
committed catalogue is what the app loads. Rerun it and commit the result:

    docker compose -f docker-compose.dev.yml run --rm --no-deps \\
      -v "$PWD:/repo" backend python /repo/api/scripts/build_field_catalogue.py

It reads whatever single *.json sits in spec/. Everything here is stdlib.

LEVELS. The catalogue is structured by level (venue / apgroup / network) so the
multi-level recommendation model can grow into it. This build populates the
VENUE level; the apgroup and network levels are added as those phases land.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# api/scripts/ -> api/
API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

import config_labels                                   # noqa: E402
from services.pisr import fetch as fetch_module        # noqa: E402

SPEC_DIR = API.parent / "spec"
OUT = API / "baselines" / "field_catalogue.json"

# Endpoints whose fields are DATA, not a single settable value — channel
# enumerations, and the per-AP-model arrays (LED, LAN ports, antenna, USB, band
# mode) where a "recommend one value" makes no sense because the value is per
# model. Kept out of the curated default view but still emitted so "show
# everything" can reach them.
_DATA_ENDPOINTS = {
    "wifiAvailableChannels", "channels",
    "ledSettings", "lanPortSettings", "apModelAntennaTypeSettings",
    "apModelExternalAntennaSettings", "apModelUsbPortSettings",
    "apModelBandModeSettings", "apModelCapabilities", "apModels",
}


def _load_spec() -> dict:
    files = sorted(SPEC_DIR.glob("*.json"))
    if not files:
        sys.exit(f"No spec JSON in {SPEC_DIR}. Drop the RUCKUS OpenAPI export "
                 "there (it is gitignored) and rerun.")
    if len(files) > 1:
        print(f"note: {len(files)} json files in spec/; using {files[0].name}")
    return json.loads(files[0].read_text(encoding="utf-8")), files[0].name


def _resolver(spec: dict):
    def resolve(ref: str):
        node = spec
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return node
    return resolve


def _is_curated(endpoint: str, path: str) -> bool:
    """A field worth recommending a value for, vs data to scroll past."""
    if endpoint in _DATA_ENDPOINTS:
        return False
    # Per-AP-model arrays (apModelCapabilities.apModels.R510.*, etc.) are
    # inventory, not a setting an admin sets one value for.
    if "apModels" in path or "apModel" in path.split(".")[0]:
        return False
    return True


def _walk(schema, resolve, prefix="", depth=0, out=None):
    """
    Flatten a response schema to (path, type, enum) leaves.

    Handles $ref, the allOf/oneOf/anyOf combinators, nested objects, and one
    level into arrays-of-objects (so a per-item field is recorded once rather
    than lost at the array — which is what a first cut got wrong on
    ledSettings and the per-model endpoints).
    """
    if out is None:
        out = []
    if depth > 5:
        return out
    if "$ref" in schema:
        schema = resolve(schema["$ref"])

    # A response that is an array at the ROOT (per-model settings — ledSettings,
    # lanPortSettings) — descend one level into its items so those fields are
    # not lost, marking the path with `[]`.
    if schema.get("type") == "array":
        item = schema.get("items") or {}
        item = resolve(item["$ref"]) if "$ref" in item else item
        if item.get("properties"):
            _walk(item, resolve, f"{prefix}[]" if prefix else "[]", depth + 1, out)
        return out

    for comb in ("allOf", "oneOf", "anyOf"):
        for sub in schema.get(comb, []):
            _walk(sub, resolve, prefix, depth, out)

    for key, raw in (schema.get("properties") or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        val = resolve(raw["$ref"]) if "$ref" in raw else raw
        vtype = val.get("type")
        if vtype == "object" and val.get("properties"):
            _walk(val, resolve, path, depth + 1, out)
        elif vtype == "array":
            item = val.get("items") or {}
            item = resolve(item["$ref"]) if "$ref" in item else item
            if item.get("type") == "object" and item.get("properties"):
                # A list of settings objects — descend one level and mark the
                # path as living under an array with `[]`.
                _walk(item, resolve, f"{path}[]", depth + 1, out)
            else:
                out.append((path, "array", val.get("items", {}).get("enum")))
        else:
            out.append((path, vtype, val.get("enum")))
    return out


def _endpoint_schema(spec: dict, resolve, suffix: str):
    """
    The GET 200 response schema for `/venues/{venueId}/<suffix>`, or None. An
    empty suffix is the venue object itself, `/venues/{venueId}`.
    """
    path = f"/venues/{{venueId}}/{suffix}" if suffix else "/venues/{venueId}"
    op = (spec.get("paths", {}).get(path, {}) or {}).get("get")
    if not op:
        return None
    sch = (op.get("responses", {}).get("200", {})
           .get("content", {}).get("application/json", {}).get("schema", {}))
    seen = 0
    while "$ref" in sch and seen < 5:
        sch = resolve(sch["$ref"])
        seen += 1
    return sch


# Endpoints the report reads from the venue OBJECT rather than a
# VENUE_CONFIG_SOURCES entry. `shape.config_card` keys their rows on these same
# names (`ENDPOINTS.update(...)`), so a RUCKUS/company value like
# `apRadioSettings.radioParams24G.allowedChannels` needs them in the catalogue
# too. Each is (catalogue endpoint key -> spec path suffix); the venue object is
# `/venues/{venueId}` with no suffix.
_EXTRA_ENDPOINTS = {
    "apRadioSettings": "apRadioSettings",
    "apMeshSettings": "apMeshSettings",
    "apManagementTrafficVlanSettings": "apManagementTrafficVlanSettings",
    "radiusServerProfiles": "radiusServerProfiles",
    "venue": "",
}


def build_venue_level(spec: dict, resolve) -> dict:
    endpoints = {}
    missing = []
    # (catalogue endpoint key -> spec path suffix). For VENUE_CONFIG_SOURCES the
    # r1_path is both; the extras separate them for the venue object.
    sources = {r1_path: r1_path for r1_path in fetch_module.VENUE_CONFIG_SOURCES.values()}
    sources.update(_EXTRA_ENDPOINTS)
    for ep_key, suffix in sources.items():
        sch = _endpoint_schema(spec, resolve, suffix)
        if sch is None:
            missing.append((ep_key, suffix or "(venue object)"))
            continue
        fields = {}
        for path, vtype, enum in _walk(sch, resolve):
            fields[path] = {
                "type": vtype or "string",
                "label": config_labels.label_for(path.split(".")[-1].split("[")[0]),
                "curated": _is_curated(ep_key, path),
                **({"enum": enum} if enum else {}),
            }
        if fields:
            endpoints[ep_key] = {
                # A human name for the endpoint group, reusing the same
                # de-camelCase the report uses for its category headers.
                "label": config_labels.label_for(ep_key),
                "fields": fields,
            }
    if missing:
        print("note: no venue GET schema for:",
              ", ".join(f"{s}({p})" for s, p in missing))
    return endpoints


def main() -> None:
    spec, name = _load_spec()
    resolve = _resolver(spec)
    venue = build_venue_level(spec, resolve)

    catalogue = {
        "generatedFrom": name,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "levels": {
            "venue": {"endpoints": venue},
            # Populated in later phases; present so the shape is stable.
            "apgroup": {"endpoints": {}},
            "network": {"endpoints": {}},
        },
    }
    OUT.write_text(json.dumps(catalogue, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")

    n_fields = sum(len(e["fields"]) for e in venue.values())
    n_curated = sum(1 for e in venue.values()
                    for f in e["fields"].values() if f["curated"])
    print(f"wrote {OUT.relative_to(API.parent)}: {len(venue)} venue endpoint(s), "
          f"{n_fields} field(s) ({n_curated} curated), from {name}")


if __name__ == "__main__":
    main()
