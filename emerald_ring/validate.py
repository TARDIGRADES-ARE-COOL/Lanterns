"""Validate a scene spec: JSON Schema first, then semantic checks the schema can't express.

Errors are returned as short, path-prefixed strings so the agent (Stage 4) can read
them and fix its own spec.
"""

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).parent / "schema" / "scene.schema.json"


@cache
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _loc(path) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out or "(root)"


def validate_spec(spec) -> list[str]:
    """Return a list of human-readable errors; empty means the spec is valid."""
    validator = Draft202012Validator(schema())
    errors = sorted(validator.iter_errors(spec), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        return [f"{_loc(e.absolute_path)}: {e.message}" for e in errors]
    return _semantic_errors(spec)


def _semantic_errors(spec: dict) -> list[str]:
    errors = []
    parts = spec["parts"]
    ids = [p["id"] for p in parts]

    seen = set()
    for i, pid in enumerate(ids):
        if pid in seen:
            errors.append(f"parts[{i}].id: duplicate part id '{pid}'")
        seen.add(pid)

    parent_of = {}
    for i, p in enumerate(parts):
        parent = p.get("parent")
        if parent is None:
            continue
        if parent == p["id"]:
            errors.append(f"parts[{i}].parent: part '{p['id']}' is its own parent")
        elif parent not in seen:
            errors.append(f"parts[{i}].parent: unknown parent '{parent}'")
        else:
            parent_of[p["id"]] = parent

    for pid in parent_of:
        chain, cur = [pid], parent_of.get(pid)
        while cur is not None:
            if cur in chain:
                errors.append(f"parts: parent cycle {' -> '.join(chain + [cur])}")
                break
            chain.append(cur)
            cur = parent_of.get(cur)

    targets = set()
    for i, a in enumerate(spec.get("animations", [])):
        if a["part"] not in seen:
            errors.append(f"animations[{i}].part: unknown part '{a['part']}'")
        key = (a["part"], a["property"])
        if key in targets:
            errors.append(f"animations[{i}]: '{a['part']}' already has a {a['property']} animation; merge them")
        targets.add(key)
        ts = [k["t"] for k in a["keyframes"]]
        if any(b <= a_ for a_, b in zip(ts, ts[1:])):
            errors.append(f"animations[{i}].keyframes: t values must be strictly increasing, got {ts}")

    return errors
