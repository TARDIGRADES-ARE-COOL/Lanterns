"""Expand reused components inside a scene spec.

The agent can put a one-line "use" entry in its parts list instead of designing a component
from scratch:

    {"id": "wing_l", "use": "dragon-guarding-a-bridge/wing_l", "parent": "torso",
     "position": [0.4, 0.6, 0.5], "rotation": [0, 0, 10], "size": 1.3, "mirror": false}

expand() replaces it with the component's real parts from the parts library:
  - `id` becomes a placement joint at `position`/`rotation` (other parts can attach to it, and
    it can be animated like any part); the component hangs off it as it looked in its source.
  - `size` scales the whole component uniformly (geometry, offsets, positions, animations).
  - `mirror` flips it left-right (across x), e.g. to make a right wing from a left one.
  - its animations come along, and its build order is fitted in after the entry's own order.
"""

import copy
import json

from . import parts as parts_lib

_GEOMETRY_KEYS = {
    "box": ["size"],
    "sphere": ["radius"],
    "cylinder": ["radiusTop", "radiusBottom", "height"],
    "cone": ["radius", "height"],
    "torus": ["radius", "tube"],
    "tube": ["points", "radius"],
    "extrude": ["outline", "depth", "bevel"],
}


def _scale_value(v, k: float):
    if isinstance(v, (int, float)):
        return round(v * k, 4)
    if isinstance(v, list):
        return [_scale_value(x, k) for x in v]
    return v


def _scale_part(p: dict, k: float) -> dict:
    p = copy.deepcopy(p)
    for key in ("position", "offset"):
        if key in p:
            p[key] = _scale_value(p[key], k)
    shape = p.get("shape", {})
    for key in _GEOMETRY_KEYS.get(shape.get("type"), []):
        if key in shape:
            shape[key] = _scale_value(shape[key], k)
    return p


def _mirror_part(p: dict) -> dict:
    p = copy.deepcopy(p)
    for key in ("position", "offset"):
        if key in p:
            p[key] = [-p[key][0], p[key][1], p[key][2]]
    if "rotation" in p:
        r = p["rotation"]
        p["rotation"] = [r[0], -r[1], -r[2]]
    shape = p.get("shape", {})
    if shape.get("type") == "tube" and "points" in shape:
        shape["points"] = [[-q[0], q[1], q[2]] for q in shape["points"]]
    if shape.get("type") == "extrude" and "outline" in shape:
        shape["outline"] = [[-q[0], q[1]] for q in shape["outline"]]
    return p


def _adapt_animation(a: dict, k: float, mirror: bool) -> dict:
    a = copy.deepcopy(a)
    for kf in a.get("keyframes", []):
        v = kf.get("value", [0, 0, 0])
        if a.get("property") == "position":
            v = _scale_value(v, k)
            if mirror:
                v = [-v[0], v[1], v[2]]
        elif a.get("property") == "rotation" and mirror:
            v = [v[0], -v[1], -v[2]]
        kf["value"] = v
    return a


def expand(spec: dict, components=None) -> tuple[dict, list[dict], list[str]]:
    """Replace every "use" entry with real parts.

    Returns (expanded spec, list of what was reused, errors). The input isn't modified.
    """
    if not any(isinstance(p, dict) and p.get("use") for p in spec.get("parts", [])):
        return spec, [], []
    comps = components if components is not None else parts_lib.refresh()
    by_id = {c.id: c for c in comps}
    spec = copy.deepcopy(spec)
    new_parts, new_anims, used, errors = [], list(spec.get("animations", [])), [], []

    for i, entry in enumerate(spec.get("parts", [])):
        if not (isinstance(entry, dict) and entry.get("use")):
            new_parts.append(entry)
            continue
        comp = by_id.get(str(entry["use"]))
        pid = str(entry.get("id") or f"reused{i}")
        if comp is None:
            errors.append(f"parts[{i}] ({pid}): unknown component {entry['use']!r}. Use an id exactly "
                          "as returned by find_parts, or design this piece yourself.")
            continue
        try:
            k = float(entry.get("size", 1) or 1)
        except (TypeError, ValueError):
            k = 1.0
        k = max(0.05, min(k, 20.0))
        mirror = bool(entry.get("mirror"))
        base_order = int((entry.get("build") or {}).get("order", 0)) if isinstance(entry.get("build"), dict) else 0

        # Placement joint: where the agent put it.
        joint = {"id": pid, "shape": {"type": "group"},
                 "position": entry.get("position", [0, 0, 0]), "build": {"order": base_order}}
        for key in ("parent", "rotation"):
            if entry.get(key) is not None:
                joint[key] = entry[key]
        new_parts.append(joint)

        orders = [int((p.get("build") or {}).get("order", 0)) for p in comp.parts]
        lowest = min(orders) if orders else 0
        rename = {p["id"]: f"{pid}__{p['id']}" for p in comp.parts}
        root_id = comp.parts[0]["id"]
        for p in comp.parts:
            q = _scale_part(p, k)
            if mirror:
                q = _mirror_part(q)
            q["id"] = rename[p["id"]]
            q["parent"] = pid if p["id"] == root_id else rename.get(p.get("parent"), pid)
            if p["id"] == root_id:
                q["position"] = [0, 0, 0]
            order = int((p.get("build") or {}).get("order", 0)) - lowest
            q["build"] = {"order": min(100, base_order + order)}
            new_parts.append(q)
        for a in comp.animations:
            if a.get("part") in rename:
                new_anims.append({**_adapt_animation(a, k, mirror), "part": rename[a["part"]]})
        used.append({"id": pid, "component": comp.id, "parts": len(comp.parts), "size": k, "mirror": mirror,
                     "chars": len(json.dumps(comp.parts)) + len(json.dumps(comp.animations))})

    spec["parts"] = new_parts
    # A component's animation may clash with one the agent wrote for the same part: keep the agent's.
    seen, anims = set(), []
    for a in new_anims:
        key = (a.get("part"), a.get("property"))
        if key not in seen:
            seen.add(key)
            anims.append(a)
    spec["animations"] = anims
    return spec, used, errors


def describe(comp) -> str:
    """One line for the agent: what it is, its size and where it sits relative to its origin."""
    child_names = sorted({p["id"].split("_")[0] for p in comp.parts[1:]})[:8]
    anims = f"; animated ({', '.join(sorted({a['property'] for a in comp.animations}))})" if comp.animations else ""
    lo, hi = comp.lo, comp.hi
    return (f"- {comp.id}: {comp.name} from \"{comp.construct}\", {len(comp.parts)} parts "
            f"({', '.join(child_names)}){anims}. At size 1 it spans x {lo[0]}..{hi[0]}, "
            f"y {lo[1]}..{hi[1]}, z {lo[2]}..{hi[2]} m around its origin.")
