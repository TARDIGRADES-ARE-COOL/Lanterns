"""Geometry inspector: catches valid specs that would still LOOK wrong.

The validator checks the spec is well-formed; the inspector checks the construct itself, by
working out where every part really ends up in 3D (the same joint/offset/scale maths as the
viewer) and looking for:

  - parts floating in mid-air, not touching the rest of the construct
  - parts sunk below the ground, or a construct hovering above it
  - left/right pairs that don't mirror each other
  - too little detail, no animations, or an absurd overall size

inspect(spec) returns a score (0-100, higher is better) and plain-English issues the model can
act on. It's used to send feedback for another round, and to pick the best of several attempts.
"""

import math
from dataclasses import dataclass, field

Vec = tuple[float, float, float]
Mat = list[list[float]]  # 4x4, row-major


@dataclass
class Report:
    score: int
    issues: list[str] = field(default_factory=list)
    parts: int = 0
    animations: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------- matrix helpers

def _mul(a: Mat, b: Mat) -> Mat:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _translate(v: Vec) -> Mat:
    return [[1, 0, 0, v[0]], [0, 1, 0, v[1]], [0, 0, 1, v[2]], [0, 0, 0, 1]]


def _scale(v: Vec) -> Mat:
    return [[v[0], 0, 0, 0], [0, v[1], 0, 0], [0, 0, v[2], 0], [0, 0, 0, 1]]


def _rotate(deg: Vec) -> Mat:
    """Euler XYZ in degrees, matching three.js (matrix = Rx * Ry * Rz)."""
    a, b, c = (math.radians(d) for d in deg)
    rx = [[1, 0, 0, 0], [0, math.cos(a), -math.sin(a), 0], [0, math.sin(a), math.cos(a), 0], [0, 0, 0, 1]]
    ry = [[math.cos(b), 0, math.sin(b), 0], [0, 1, 0, 0], [-math.sin(b), 0, math.cos(b), 0], [0, 0, 0, 1]]
    rz = [[math.cos(c), -math.sin(c), 0, 0], [math.sin(c), math.cos(c), 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    return _mul(_mul(rx, ry), rz)


def _apply(m: Mat, p: Vec) -> Vec:
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3))


# ---------------------------------------------------------------- shape sample points

def _ring(r: float, y: float, n: int = 8) -> list[Vec]:
    return [(r * math.cos(2 * math.pi * i / n), y, r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _shape_points(shape: dict) -> list[Vec]:
    """Points on the surface of a primitive, in its own local space (before transforms)."""
    t = shape["type"]
    if t == "box":
        x, y, z = (s / 2 for s in shape["size"])
        return [(sx * x, sy * y, sz * z) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    if t == "sphere":
        r = shape["radius"]
        d = r / math.sqrt(3)
        axes = [(r, 0, 0), (-r, 0, 0), (0, r, 0), (0, -r, 0), (0, 0, r), (0, 0, -r)]
        return axes + [(sx * d, sy * d, sz * d) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    if t == "cylinder":
        h = shape["height"] / 2
        return _ring(max(shape["radiusTop"], 1e-3), h) + _ring(max(shape["radiusBottom"], 1e-3), -h)
    if t == "cone":
        h = shape["height"] / 2
        return _ring(shape["radius"], -h) + [(0, h, 0)]
    if t == "torus":
        big, tube = shape["radius"], shape["tube"]
        arc = math.radians(shape.get("arc", 360))
        pts = []
        for i in range(17):
            a = arc * i / 16
            for rr in (big - tube, big + tube):
                pts.append((rr * math.cos(a), rr * math.sin(a), 0))
            pts += [(big * math.cos(a), big * math.sin(a), tube), (big * math.cos(a), big * math.sin(a), -tube)]
        return pts
    if t == "tube":
        r = shape["radius"]
        return [(p[0] + dx, p[1] + dy, p[2] + dz) for p in shape["points"]
                for dx, dy, dz in ((r, 0, 0), (-r, 0, 0), (0, r, 0), (0, -r, 0), (0, 0, r), (0, 0, -r))]
    if t == "extrude":
        z = shape["depth"] / 2 + shape.get("bevel", 0)
        return [(x, y, sz * z) for x, y in shape["outline"] for sz in (-1, 1)]
    return []  # group: no geometry


# ---------------------------------------------------------------- world-space boxes

@dataclass
class _Box:
    id: str
    lo: Vec
    hi: Vec

    @property
    def center(self) -> Vec:
        return tuple((a + b) / 2 for a, b in zip(self.lo, self.hi))

    @property
    def extent(self) -> Vec:
        return tuple(b - a for a, b in zip(self.lo, self.hi))

    def touches(self, other: "_Box", tol: float) -> bool:
        return all(self.lo[i] - tol <= other.hi[i] and other.lo[i] - tol <= self.hi[i] for i in range(3))


def world_boxes(spec: dict) -> list[_Box]:
    """Axis-aligned world bounding box of every visible part, at the rest pose."""
    parts = {p["id"]: p for p in spec["parts"]}
    joint_cache: dict[str, Mat] = {}

    def joint(pid: str) -> Mat:
        if pid not in joint_cache:
            p = parts[pid]
            local = _mul(_translate(tuple(p["position"])), _rotate(tuple(p.get("rotation", (0, 0, 0)))))
            parent = p.get("parent")
            joint_cache[pid] = _mul(joint(parent), local) if parent in parts else local
        return joint_cache[pid]

    boxes = []
    for p in spec["parts"]:
        pts = _shape_points(p["shape"])
        if not pts:
            continue
        mesh = _mul(_translate(tuple(p.get("offset", (0, 0, 0)))), _scale(tuple(p.get("scale", (1, 1, 1)))))
        m = _mul(joint(p["id"]), mesh)
        world = [_apply(m, q) for q in pts]
        lo = tuple(min(w[i] for w in world) for i in range(3))
        hi = tuple(max(w[i] for w in world) for i in range(3))
        boxes.append(_Box(p["id"], lo, hi))
    return boxes


# ---------------------------------------------------------------- checks

def _mirror_pairs(ids: set[str]) -> list[tuple[str, str]]:
    pairs = []
    for pid in ids:
        for a, b in (("_l", "_r"), ("_left", "_right"), ("left_", "right_"), ("l_", "r_")):
            if a.startswith("_") and pid.endswith(a) and pid[: -len(a)] + b in ids:
                pairs.append((pid, pid[: -len(a)] + b))
            elif not a.startswith("_") and pid.startswith(a) and b + pid[len(a):] in ids:
                pairs.append((pid, b + pid[len(a):]))
        # also "_l_" in the middle, e.g. "leg_l_upper"
        if "_l_" in pid and pid.replace("_l_", "_r_", 1) in ids:
            pairs.append((pid, pid.replace("_l_", "_r_", 1)))
    return pairs


def _names(ids: list[str], limit: int = 6) -> str:
    shown = ", ".join(ids[:limit])
    return shown + (f" (+{len(ids) - limit} more)" if len(ids) > limit else "")


def inspect(spec: dict) -> Report:
    boxes = world_boxes(spec)
    n_parts = len(spec.get("parts", []))
    n_anims = len(spec.get("animations", []))
    report = Report(score=100, parts=n_parts, animations=n_anims)
    if not boxes:
        return Report(score=0, issues=["The construct has no visible parts."], parts=n_parts, animations=n_anims)

    lo = tuple(min(b.lo[i] for b in boxes) for i in range(3))
    hi = tuple(max(b.hi[i] for b in boxes) for i in range(3))
    size = max(hi[i] - lo[i] for i in range(3)) or 1.0
    tol = 0.03 * size

    def issue(text: str, penalty: int) -> None:
        report.issues.append(text)
        report.score -= penalty

    # Overall size and ground contact.
    if size < 1.0 or size > 20.0:
        issue(f"The construct is {size:.1f} m across; keep it roughly 2-12 m.", 10)
    if lo[1] > 0.05 * size:
        issue(f"The whole construct hovers {lo[1]:.2f} m above the ground (y=0). Lower it so its "
              "base or feet rest on the ground, unless it's meant to fly.", 15)
    sunk = [b.id for b in boxes if b.lo[1] < -0.05 * size]
    if sunk:
        issue(f"These parts sink below the ground (y<0): {_names(sunk)}. Raise them so nothing "
              "goes under y=0.", min(20, 5 * len(sunk)))

    # Floating parts: group touching boxes; everything should join the main body.
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes[i].touches(boxes[j], tol):
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(boxes)):
        groups.setdefault(find(i), []).append(i)
    if len(groups) > 1:
        # A cluster is fine if it's the main body or rests on the ground (e.g. a pile of stones).
        lowest = min(range(len(boxes)), key=lambda i: boxes[i].lo[1])
        main = max(groups.values(), key=lambda g: (lowest in g, len(g)))
        ground = lo[1] + tol
        floating = [boxes[i].id for g in groups.values()
                    if g is not main and min(boxes[i].lo[1] for i in g) > ground for i in g]
    else:
        floating = []
    if floating:
        issue(f"These parts float in mid-air, not touching the rest of the construct: "
              f"{_names(floating)}. Move them (or their joints) so they connect to the body.",
              min(30, 10 + len(floating)))

    # Left/right pairs should be the same size. (Positions may differ: one arm can be raised.)
    by_id = {b.id: b for b in boxes}
    bad_pairs = []
    for a, b in _mirror_pairs(set(by_id)):
        ea, eb = sorted(by_id[a].extent), sorted(by_id[b].extent)
        if any(abs(x - y) > 0.25 * max(x, y, 1e-6) and abs(x - y) > tol for x, y in zip(ea, eb)):
            bad_pairs.append(f"{a}/{b}")
    if bad_pairs:
        issue(f"These left/right pairs are different sizes: {_names(bad_pairs, 4)}. Make each pair "
              "the same size (mirror the numbers exactly).", min(20, 6 * len(bad_pairs)))

    # Detail and life.
    if n_parts < 18:
        issue(f"Only {n_parts} parts: add detail so it reads clearly (aim for 20-45).", 15)
    if n_anims == 0:
        issue("No animations: add 2-6 subtle idle animations (spins, sways, pulses).", 15)

    report.score = max(0, report.score)
    return report
