"""Parts library: reusable components lifted out of constructs you've already forged.

Every construct is a tree of parts: a dragon's `head` group carries the skull, jaw, horns and
eyes. Any branch with a few pieces is a ready-made component. This module finds them in
samples/ and constructs/, stores each one re-rooted at its own origin (looking exactly as it did
in its source construct), and lets you search them by meaning-ish keywords.

The index lives in constructs/.parts_index.json and refreshes itself when constructs change.
"""

import json
import math
import re
from dataclasses import dataclass

from . import store
from .inspect import _mul, _rotate, inspect, world_boxes

INDEX_PATH = store.CONSTRUCTS / ".parts_index.json"
MIN_PARTS = 3  # a component has at least this many parts (itself + 2 descendants)
MAX_SHARE = 0.9  # skip branches that are basically the whole construct
MIN_SCORE = 80  # only take components from constructs the inspector rated well

_STOP = {"a", "an", "the", "of", "on", "in", "with", "and", "its", "to", "for", "giant", "emerald",
         "hard", "light", "hardlight", "construct", "grp", "group", "root", "base", "g", "p", "c",
         "l", "r", "left", "right", "joint", "pivot", "anchor"}


@dataclass
class Component:
    id: str  # "<construct-slug>/<part-id>"
    name: str  # readable, e.g. "head"
    construct: str  # source title
    source: str  # source file (relative)
    parts: list[dict]
    animations: list[dict]
    size: list[float]  # width, height, depth in metres
    lo: list[float]  # bounding box relative to the component's origin
    hi: list[float]
    score: int  # inspector score of the source construct
    keywords: list[str]

    def summary(self) -> str:
        w, h, d = self.size
        anim = f", {len(self.animations)} animations" if self.animations else ""
        return f"{self.id}: {self.name} from \"{self.construct}\" ({len(self.parts)} parts{anim}, {w:.1f}×{h:.1f}×{d:.1f} m)"


# ---------------------------------------------------------------- geometry helpers

def _euler_xyz(m) -> list[float]:
    """Rotation matrix (Rx*Ry*Rz, like three.js 'XYZ') back to Euler degrees."""
    m13 = max(-1.0, min(1.0, m[0][2]))
    y = math.asin(m13)
    if abs(m13) < 0.9999999:
        x = math.atan2(-m[1][2], m[2][2])
        z = math.atan2(-m[0][1], m[0][0])
    else:
        x = math.atan2(m[2][1], m[1][1])
        z = 0.0
    return [round(math.degrees(a), 2) for a in (x, y, z)]


def _world_rotation(pid: str, parts: dict[str, dict]):
    """Combined rotation of a part's joint, including all its ancestors."""
    chain = []
    cur = pid
    while cur in parts:
        chain.append(parts[cur])
        cur = parts[cur].get("parent")
    m = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    for p in reversed(chain):
        m = _mul(m, _rotate(tuple(p.get("rotation", (0, 0, 0)))))
    return m


def _tokens(text: str) -> list[str]:
    words = re.split(r"[^a-z]+", text.lower())
    out = []
    for w in words:
        if len(w) < 3 or w in _STOP:
            continue
        out.append(w[:-1] if w.endswith("s") and len(w) > 3 else w)  # crude singular
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "construct"


# ---------------------------------------------------------------- extraction

def extract(spec: dict, source: str) -> list[Component]:
    """All reusable components in one construct."""
    report = inspect(spec)
    if report.score < MIN_SCORE:
        return []
    parts = {p["id"]: p for p in spec["parts"]}
    children: dict[str | None, list[str]] = {}
    for p in spec["parts"]:
        children.setdefault(p.get("parent") if p.get("parent") in parts else None, []).append(p["id"])

    def subtree(pid: str) -> list[str]:
        out = [pid]
        for c in children.get(pid, []):
            out += subtree(c)
        return out

    total = len(spec["parts"])
    slug = _slug(spec.get("title", "construct"))
    comps = []
    for pid, p in parts.items():
        ids = subtree(pid)
        if len(ids) < MIN_PARTS or len(ids) > MAX_SHARE * total:
            continue
        idset = set(ids)
        # Re-root: the component's root sits at the origin, keeping the orientation it had in
        # the source (its own rotation plus everything above it).
        root = dict(parts[pid])
        root.pop("parent", None)
        root["position"] = [0, 0, 0]
        root["rotation"] = _euler_xyz(_world_rotation(pid, parts))
        comp_parts = [root] + [dict(parts[i]) for i in ids[1:]]
        anims = [a for a in spec.get("animations", []) if a.get("part") in idset]
        boxes = world_boxes({"parts": comp_parts})
        if not boxes:
            continue
        lo = [round(min(b.lo[i] for b in boxes), 2) for i in range(3)]
        hi = [round(max(b.hi[i] for b in boxes), 2) for i in range(3)]
        name = p.get("label") or pid
        words = _tokens(name.replace("_", " ") + " " + pid)
        words += _tokens(" ".join(parts[i].get("label", "") + " " + i for i in ids[1:]))
        words += _tokens(spec.get("title", "") + " " + spec.get("prompt", ""))
        comps.append(Component(
            id=f"{slug}/{pid}", name=name.replace("_", " "), construct=spec.get("title", ""),
            source=source, parts=comp_parts, animations=anims,
            size=[round(hi[i] - lo[i], 2) for i in range(3)], lo=lo, hi=hi,
            score=report.score, keywords=sorted(set(words)),
        ))
    return comps


# ---------------------------------------------------------------- index

def _sources() -> list:
    return sorted(store.SAMPLES.glob("*.json")) + sorted(store.CONSTRUCTS.glob("*.json"))


def refresh() -> list[Component]:
    """Update the index for any construct that's new or changed, then return every component."""
    try:
        index = json.loads(INDEX_PATH.read_text())
    except (OSError, ValueError):
        index = {}
    files = _sources()
    seen = set()
    changed = False
    for f in files:
        rel = f.relative_to(store.ROOT).as_posix()
        seen.add(rel)
        mtime = f.stat().st_mtime
        if rel in index and index[rel]["mtime"] == mtime:
            continue
        try:
            comps = extract(json.loads(f.read_text()), rel)
        except (ValueError, KeyError, TypeError):
            comps = []
        index[rel] = {"mtime": mtime, "components": [c.__dict__ for c in comps]}
        changed = True
    for rel in list(index):
        if rel not in seen:  # construct was deleted
            del index[rel]
            changed = True
    if changed:
        INDEX_PATH.parent.mkdir(exist_ok=True)
        INDEX_PATH.write_text(json.dumps(index))
    return [Component(**c) for entry in index.values() for c in entry["components"]]


def search(query: str, k: int = 8, components: list[Component] | None = None) -> list[Component]:
    """Components most relevant to a description, best first, at most one per source construct
    per name so near-duplicates (many mech fists) don't crowd out everything else."""
    comps = components if components is not None else refresh()
    q = set(_tokens(query))
    if not q:
        return []
    scored = []
    for c in comps:
        name_words = set(_tokens(c.name + " " + c.id.split("/", 1)[1]))
        kw = set(c.keywords)
        hits = 3 * len(q & name_words) + len(q & kw)
        if hits:
            scored.append((hits + c.score / 200 + min(len(c.parts), 20) / 100, c))
    scored.sort(key=lambda t: -t[0])
    out, seen = [], set()
    for _, c in scored:
        # Mirrored/repeated copies (4 identical towers) and near-identical components from
        # similar constructs (many mech fists) show up once.
        sigs = {(c.name.lower(), len(c.parts)),
                (c.source, len(c.parts), tuple(round(x, 1) for x in c.size))}
        if sigs & seen:
            continue
        seen |= sigs
        out.append(c)
        if len(out) == k:
            break
    return out


def get(component_id: str, components: list[Component] | None = None) -> Component | None:
    for c in components if components is not None else refresh():
        if c.id == component_id:
            return c
    return None
