"""Team mode: several free models build one construct together.

    spec = forge_team("a giant mech fist")

A single free model writing a whole construct in one go produces something simple. Here the
job is split up so each model does a small, focused piece of work:

  1. Planner   one model writes a blueprint: 4-8 components (forearm, palm, fingers...), where
               each attaches, how big it is, what it looks like, and which ones move.
  2. Builders  free models build the components in parallel, each in its own local space.
               Components marked as a mirror of another (left/right wings, legs) aren't built
               at all: they're copied and flipped, so pairs are always exactly symmetric.
  3. Assembly  components snap together as groups at their anchors; the geometry inspector
               checks the result and any component that floats away is rebuilt once.

Every call uses JSON mode (the host guarantees valid JSON), and each model only runs one
request at a time so it stays inside its free per-minute token limit.
"""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import openai

from . import config, store
from .inspect import inspect, world_boxes
from .providers import FREE_CLOUD, ForgeError, _retry_after
from .validate import validate_spec

Emit = Callable[..., None]

TEAMS = {
    "groq": {
        "planner": "openai/gpt-oss-120b",
        "builders": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
    },
}

_SHAPES = """\
Shapes (each part has exactly one; no other keys inside a shape; all sizes > 0):
  {"type": "box", "size": [x,y,z]}                      centred on the part's origin
  {"type": "sphere", "radius": r, "segments": 6-24}      centred (low segments = faceted)
  {"type": "cylinder", "radiusTop": r, "radiusBottom": r, "height": h, "segments": 6-24}
                                                         centred, axis along Y
  {"type": "cone", "radius": r, "height": h}             centred, tip points +Y
  {"type": "torus", "radius": r, "tube": t, "arc": deg}  lies in the XY plane (rotate [90,0,0] to lay flat)
  {"type": "tube", "points": [[x,y,z],...], "radius": r} smooth tube through points (cables, curves)
  {"type": "extrude", "outline": [[x,y],...], "depth": d} flat polygon in XY, thickness d along Z (blades, plates, fins)
  {"type": "group"}                                      invisible joint that carries children
"""

PLANNER_PROMPT = """\
You are the planner for the Emerald Ring, a power ring that builds glowing green hard-light
constructs out of simple 3D shapes. A team of builders will each build ONE component of your
blueprint, so your job is the overall design: proportions, layout and how the pieces connect.

Conventions: Y is up, the ground is y = 0, units are metres (whole construct about 3-10 m),
rotations in degrees.

Break the construct into 4-8 components. For each component give:
- "name": short snake_case name (e.g. "forearm", "finger_index", "wing_l")
- "parent": the name of the component it attaches to, or null for the base that rests on the ground
- "anchor": [x,y,z] the attachment point, RELATIVE TO THE PARENT'S ANCHOR (for the base: its
  position on the ground, usually [0,0,0])
- "bounds": {"min": [x,y,z], "max": [x,y,z]} the box the component fills, relative to its OWN
  anchor. The anchor must lie on the edge of or inside these bounds. A child's anchor must lie
  inside or on the surface of its parent's bounds, so the pieces touch.
- "description": what it looks like in detail: shapes to use, distinctive features, pose.
  Builders only see this, so be specific (e.g. "clenched: 3 segments curled down over the
  front of the palm, knuckle cap on top").
- "detail": how many parts the builder should use (4-14)
- "order": build order 0-9 (base first, small details last)
- "mirror_of": for a right-hand twin of a left component, the left component's name; it will be
  copied and flipped across x = 0, so give only its "name", "parent", "anchor", "order" and
  "mirror_of". Otherwise omit it.
- "animation" (optional, 2-4 components): motion of the WHOLE component around its anchor:
  {"property": "rotation" | "position" | "scale", "duration": seconds, "loop": "pingpong" | "repeat",
   "keyframes": [{"t": 0, "value": [x,y,z]}, {"t": 1, "value": [x,y,z]}]}
  Values are OFFSETS from rest (degrees for rotation; multipliers for scale).

Design checklist (think it through before answering):
- Pose: what rests on the ground? The construct should stand up and read clearly from a
  three-quarter view; avoid laying the main mass flat unless the object really is flat.
- Completeness: include every piece the object needs to be recognisable (a fist needs a
  wrist or forearm; a dragon needs a neck, head, legs, tail and wings).
- Proportions: sizes relative to each other must be believable (each finger roughly a quarter
  of the palm's width; a head roughly a quarter to a third of a torso's height).
- Connection: every child's anchor sits on its parent's surface, and its bounds overlap the
  parent's bounds.
- Shape language: describe each component's form precisely (curl, taper, angle, layering),
  because the builders can't see the other components.

Return JSON only: {"title": str, "dimensions": str, "components": [...]}.

Example (abbreviated):
{"title": "Sentry Walker", "dimensions": "~2.5 m tall", "components": [
 {"name": "body", "parent": null, "anchor": [0,1.4,0], "bounds": {"min": [-0.7,0,-0.5], "max": [0.7,0.8,0.5]},
  "description": "boxy armoured torso with a glowing round core on the chest and a backpack", "detail": 8, "order": 1},
 {"name": "leg_l", "parent": "body", "anchor": [0.4,0,0], "bounds": {"min": [-0.2,-1.4,-0.25], "max": [0.2,0,0.3]},
  "description": "hip ball, thick thigh, thinner shin, flat foot whose underside sits exactly 1.4 m below the anchor",
  "detail": 5, "order": 2, "animation": {"property": "rotation", "duration": 2, "loop": "pingpong",
  "keyframes": [{"t": 0, "value": [-15,0,0]}, {"t": 1, "value": [15,0,0]}]}},
 {"name": "leg_r", "parent": "body", "anchor": [-0.4,0,0], "order": 2, "mirror_of": "leg_l"}]}
"""

BUILDER_PROMPT = """\
You are a builder for the Emerald Ring, which forges glowing green hard-light constructs out of
simple shapes. You build ONE component of a larger construct, in that component's own local
space: the origin [0,0,0] is the component's anchor (where it attaches to the rest).

Rules:
- Every part must lie inside the component's bounds, and the parts must touch each other.
  At least one part must touch the origin, which is where this component connects.
- FILL the bounds: together your parts should span close to the full width, height and depth
  of the bounds box, so the component comes out the size the planner intended.
- Y is up, units are metres, rotations in degrees (Euler XYZ).
- A part's "position" is its joint (pivot), relative to its parent part's joint (or to the
  origin if it has no parent). "offset" moves the shape away from the joint so it can pivot at
  one end. "scale" stretches only that part's shape.
- Use the requested amount of detail; clean geometric shapes with strong silhouettes.
- Omit fields at their defaults (rotation [0,0,0], offset [0,0,0], scale [1,1,1]). Round to 2
  decimals. Short ids.
- "intensity" (1.5-3) makes small features glow brighter: eyes, cores, edges. Use sparingly.
- Optional small animations of your own parts (gears spinning, a core pulsing):
  {"part": id, "property": "rotation" | "position" | "scale", "duration": s,
   "loop": "pingpong" | "repeat", "keyframes": [{"t": 0, "value": [..]}, {"t": 1, "value": [..]}]}
  Values are offsets from rest. Don't animate the whole component; that's handled separately.

""" + _SHAPES + """
Part format: {"id": str, "parent": id (optional), "shape": {...}, "position": [x,y,z],
              "rotation": [x,y,z], "offset": [x,y,z], "scale": [x,y,z], "intensity": n}

Return JSON only: {"parts": [...], "animations": [...]}.
"""


# ---------------------------------------------------------------- model calls

class _Pool:
    """One client per host; each model runs one request at a time (its own token budget)."""

    def __init__(self, provider: str, emit: Emit):
        cfg = FREE_CLOUD[provider]
        config.load_env()
        key = os.environ.get(cfg["key_env"])
        if not key:
            raise ForgeError(f"{provider} isn't set up. Get a free key at {cfg['signup']} and add "
                             f"{cfg['key_env']}=... to .env")
        self.client = openai.OpenAI(api_key=key, base_url=cfg["base_url"], timeout=180, max_retries=1)
        self.locks: dict[str, threading.Lock] = {}
        self.emit = emit
        self.tokens = 0

    def json(self, model: str, system: str, user: str, label: str, max_tokens: int = 5000,
             reasoning: str | None = None) -> dict:
        lock = self.locks.setdefault(model, threading.Lock())
        with lock:
            for attempt in range(4):
                try:
                    r = self.client.chat.completions.create(
                        model=model,
                        response_format={"type": "json_object"},
                        max_completion_tokens=max_tokens,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                        **({"reasoning_effort": reasoning} if reasoning else {}),
                    )
                    if r.usage:
                        self.tokens += (r.usage.prompt_tokens or 0) + (r.usage.completion_tokens or 0)
                    return json.loads(r.choices[0].message.content or "{}")
                except openai.RateLimitError as e:
                    wait = _retry_after(e)
                    if wait is None or wait > 90 or attempt == 3:
                        raise ForgeError(f"{model}: free-tier limit reached; try again later.")
                    self.emit("cooldown", who=label, seconds=round(wait))
                    time.sleep(wait + 1)
                except (openai.APIStatusError, json.JSONDecodeError) as e:
                    if attempt == 3:
                        raise ForgeError(f"{model} failed on {label}: {str(e)[:120]}")
                    self.emit("retry", who=label, reason="bad response, retrying")
                except openai.APIConnectionError:
                    raise ForgeError("Couldn't reach the free model host. Check your internet connection.")
        raise ForgeError(f"{model} failed on {label}")


# ---------------------------------------------------------------- blueprint

def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(name)).strip("_")[:24] or "part"


def _vec(v, n=3, default=0.0) -> list[float]:
    try:
        out = [float(x) for x in v][:n]
    except (TypeError, ValueError):
        out = []
    return out + [default] * (n - len(out))


def _check_blueprint(bp: dict) -> list[dict]:
    comps = bp.get("components")
    if not isinstance(comps, list) or not comps:
        raise ValueError("no components")
    names = set()
    clean = []
    for c in comps:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        c["name"] = _slug(c["name"])
        if c["name"] in names:
            continue
        names.add(c["name"])
        clean.append(c)
    for c in clean:
        if c.get("parent") not in names or c.get("parent") == c["name"]:
            c["parent"] = None
        if c.get("mirror_of") and _slug(c["mirror_of"]) not in names:
            c.pop("mirror_of")
        elif c.get("mirror_of"):
            c["mirror_of"] = _slug(c["mirror_of"])
    if not any(c["parent"] is None for c in clean):
        clean[0]["parent"] = None
    return clean


def _blueprint_issues(comps: list[dict]) -> list[str]:
    """Check the planned layout before anything is built (anchors, bounds, ground contact)."""
    by_name = {c["name"]: c for c in comps}
    world_anchor: dict[str, list[float]] = {}

    def anchor_of(name: str, depth: int = 0) -> list[float]:
        if name in world_anchor:
            return world_anchor[name]
        c = by_name[name]
        a = _vec(c.get("anchor"))
        if c.get("mirror_of") and not c.get("anchor"):
            src = _vec(by_name[c["mirror_of"]].get("anchor"))
            a = [-src[0], src[1], src[2]]
        if c.get("parent") and depth < 20:
            pa = anchor_of(c["parent"], depth + 1)
            a = [a[i] + pa[i] for i in range(3)]
        world_anchor[name] = a
        return a

    boxes = {}
    issues = []
    for c in comps:
        src = by_name.get(c.get("mirror_of")) if c.get("mirror_of") else c
        b = src.get("bounds") or {}
        if not b.get("min") or not b.get("max"):
            issues.append(f"{c['name']} has no bounds; give it bounds min/max.")
            continue
        lo, hi = _vec(b["min"]), _vec(b["max"])
        if c.get("mirror_of"):
            lo, hi = [-hi[0], lo[1], lo[2]], [-lo[0], hi[1], hi[2]]
        a = anchor_of(c["name"])
        boxes[c["name"]] = ([a[i] + min(lo[i], hi[i]) for i in range(3)], [a[i] + max(lo[i], hi[i]) for i in range(3)])
    if not boxes:
        return issues or ["No component has usable bounds."]

    glo = [min(bx[0][i] for bx in boxes.values()) for i in range(3)]
    ghi = [max(bx[1][i] for bx in boxes.values()) for i in range(3)]
    size = max(ghi[i] - glo[i] for i in range(3)) or 1.0
    tol = 0.05 * size
    if glo[1] > tol:
        issues.append(f"Nothing touches the ground: the lowest point is at y={glo[1]:.2f}. Move the base down to y=0.")
    if glo[1] < -tol:
        issues.append(f"Parts go {-glo[1]:.2f} m below the ground; nothing should be below y=0.")
    for c in comps:
        name, parent = c["name"], c.get("parent")
        if name not in boxes:
            continue
        lo, hi = boxes[name]
        if max(hi[i] - lo[i] for i in range(3)) < 0.06 * size:
            issues.append(f"{name} is tiny compared with the whole construct ({size:.1f} m); make it bigger or merge it.")
        if parent in boxes:
            plo, phi = boxes[parent]
            if not all(lo[i] - tol <= phi[i] and plo[i] - tol <= hi[i] for i in range(3)):
                issues.append(f"{name} doesn't touch its parent {parent}: its bounds don't overlap the parent's. "
                              "Fix its anchor or bounds.")
    height = ghi[1] - glo[1]
    if height < 0.3 * size:
        issues.append(f"The construct is very flat ({height:.1f} m tall but {size:.1f} m across). Unless the "
                      "object is genuinely flat, stand it up so it reads from the side.")
    return issues


# ---------------------------------------------------------------- building

def _builder_request(comp: dict, blueprint: dict, comps: list[dict], feedback: str = "") -> str:
    b = comp.get("bounds") or {}
    others = "\n".join(f"- {c['name']}: {c.get('description', '(mirror of ' + str(c.get('mirror_of')) + ')')}"
                       for c in comps if c is not comp)
    return (
        f"Construct: {blueprint.get('title', '')} ({blueprint.get('dimensions', '')}).\n"
        f"Other components (built by others, for context only):\n{others}\n\n"
        f"YOUR component: {comp['name']}\n"
        f"Description: {comp.get('description', '')}\n"
        f"Bounds relative to your origin: min {_vec(b.get('min'))}, max {_vec(b.get('max'))}\n"
        f"Use about {comp.get('detail', 8)} parts.\n"
        + (f"\nYour previous attempt had problems; fix them:\n{feedback}\n" if feedback else "")
    )


def _clean_parts(raw: dict) -> tuple[list[dict], list[dict]]:
    parts = [p for p in (raw.get("parts") or []) if isinstance(p, dict) and p.get("id") and isinstance(p.get("shape"), dict)]
    anims = [a for a in (raw.get("animations") or []) if isinstance(a, dict)]
    return parts, anims


def _mirror_parts(parts: list[dict], anims: list[dict]) -> tuple[list[dict], list[dict]]:
    """Flip a component across x = 0 (positions, offsets, rotations and shape points)."""
    out = []
    for p in json.loads(json.dumps(parts)):
        for key in ("position", "offset"):
            if key in p:
                p[key] = [-p[key][0], p[key][1], p[key][2]]
        if "rotation" in p:
            r = p["rotation"]
            p["rotation"] = [r[0], -r[1], -r[2]]
        s = p["shape"]
        if s.get("type") == "tube" and "points" in s:
            s["points"] = [[-q[0], q[1], q[2]] for q in s["points"]]
        if s.get("type") == "extrude" and "outline" in s:
            s["outline"] = [[-q[0], q[1]] for q in s["outline"]]
        out.append(p)
    flipped = []
    for a in json.loads(json.dumps(anims)):
        if a.get("property") in ("rotation", "position"):
            for k in a.get("keyframes", []):
                v = k.get("value", [0, 0, 0])
                k["value"] = [-v[0], v[1], v[2]] if a["property"] == "position" else [v[0], -v[1], -v[2]]
        flipped.append(a)
    return out, flipped


def _assemble(blueprint: dict, comps: list[dict], built: dict[str, tuple[list, list]], prompt: str) -> dict:
    by_name = {c["name"]: c for c in comps}
    orders = sorted({int(c.get("order", 0)) for c in comps})
    parts, animations = [], []
    for c in comps:
        gid = f"c_{c['name']}"
        anchor = _vec(c.get("anchor"))
        if c.get("mirror_of"):
            src = by_name[c["mirror_of"]]
            if not c.get("anchor"):
                a = _vec(src.get("anchor"))
                anchor = [-a[0], a[1], a[2]]
        base_order = orders.index(int(c.get("order", 0))) * 10
        parts.append({"id": gid, "parent": f"c_{c['parent']}" if c.get("parent") else None,
                      "shape": {"type": "group"}, "position": anchor, "build": {"order": base_order}})
        cparts, canims = built.get(c["name"], ([], []))
        ids = {p["id"] for p in cparts}
        rename = {pid: f"{c['name']}_{_slug(pid)}" for pid in ids}
        for p in cparts:
            q = dict(p)
            q["id"] = rename[p["id"]]
            q["parent"] = rename.get(p.get("parent"), gid)
            q["position"] = _vec(p.get("position"))
            order = (p.get("build") or {}).get("order", 0) if isinstance(p.get("build"), dict) else 0
            q["build"] = {"order": min(100, base_order + min(int(order or 0), 9))}
            parts.append(q)
        for a in canims:
            if a.get("part") in rename:
                animations.append({**a, "part": rename[a["part"]]})
        anim = c.get("animation")
        if not anim and c.get("mirror_of"):
            src_anim = by_name[c["mirror_of"]].get("animation")
            if src_anim:
                anim = _mirror_parts([], [dict(src_anim)])[1][0]
        if isinstance(anim, dict) and anim.get("keyframes"):
            animations.append({**anim, "part": gid})
    # Keep only animation fields the schema knows, one per (part, property).
    allowed = {"part", "property", "duration", "loop", "easing", "phase", "keyframes"}
    seen, anims = set(), []
    for a in animations:
        a = {k: v for k, v in a.items() if k in allowed}
        if (a.get("part"), a.get("property")) in seen:
            continue
        seen.add((a.get("part"), a.get("property")))
        anims.append(a)
    allowed_part = {"id", "parent", "label", "shape", "position", "rotation", "scale", "offset", "build", "intensity"}
    parts = [{k: v for k, v in p.items() if k in allowed_part and v is not None} for p in parts]
    return {"version": 1, "title": str(blueprint.get("title") or prompt)[:80], "prompt": prompt,
            "parts": parts, "animations": anims, "forge": {"duration": 8}}


def _component_errors(spec: dict) -> dict[str, list[str]]:
    """Validation errors, grouped by the component they belong to."""
    errors = validate_spec(spec)
    by_comp: dict[str, list[str]] = {}
    for e in errors:
        m = re.match(r"parts\[(\d+)\]", e) or re.match(r"animations\[(\d+)\]", e)
        comp = "?"
        if m and e.startswith("parts["):
            comp = spec["parts"][int(m.group(1))]["id"]
        elif m:
            comp = spec["animations"][int(m.group(1))].get("part", "?")
        by_comp.setdefault(comp, []).append(e)
    return by_comp


def _drop_invalid(spec: dict) -> dict:
    """Remove individual parts/animations the validator rejects (last resort)."""
    for _ in range(5):
        errors = validate_spec(spec)
        if not errors:
            return spec
        bad_parts, bad_anims = set(), set()
        for e in errors:
            m = re.match(r"parts\[(\d+)\]", e)
            if m:
                bad_parts.add(int(m.group(1)))
            m = re.match(r"animations\[(\d+)\]", e)
            if m:
                bad_anims.add(int(m.group(1)))
        if not bad_parts and not bad_anims:
            break
        dropped = {spec["parts"][i]["id"] for i in bad_parts if not spec["parts"][i]["id"].startswith("c_")}
        spec["parts"] = [p for i, p in enumerate(spec["parts"]) if i not in bad_parts or p["id"].startswith("c_")]
        for p in spec["parts"]:  # re-home children of dropped parts
            if p.get("parent") in dropped:
                p["parent"] = None
        spec["animations"] = [a for i, a in enumerate(spec["animations"])
                              if i not in bad_anims and a.get("part") not in dropped]
    return spec


def _floating_components(spec: dict, comps: list[dict]) -> list[str]:
    """Components whose parts float away from the rest, according to the inspector."""
    report = inspect(spec)
    names = {c["name"] for c in comps}
    hit = set()
    for issue in report.issues:
        if "float in mid-air" in issue:
            for token in re.findall(r"[A-Za-z0-9_-]+", issue):
                for n in names:
                    if token.startswith(n + "_"):
                        hit.add(n)
    return sorted(hit)


def forge_team(
    description: str,
    *,
    provider: str = "groq",
    on_event: Emit | None = None,
    launch_viewer: Callable[[Path], str] | None = None,
    save: bool = True,
) -> dict:
    emit: Emit = on_event or (lambda kind, **data: None)
    team = TEAMS.get(provider)
    if not team:
        raise ForgeError(f"Team mode isn't configured for {provider}.")
    pool = _Pool(provider, emit)
    start = time.monotonic()

    # 1. Plan.
    emit("stage", text="Planner is designing the blueprint")
    comps, request, best = None, f"Design this construct: {description}", None
    for attempt in range(3):
        blueprint = pool.json(team["planner"], PLANNER_PROMPT, request, "planner",
                              max_tokens=7000, reasoning="high")
        try:
            candidate = _check_blueprint(blueprint)
        except ValueError:
            emit("retry", who="planner", reason="blueprint had no components, retrying")
            continue
        issues = _blueprint_issues(candidate)
        if best is None or len(issues) < best[2]:
            best = (blueprint, candidate, len(issues))
        if not issues or attempt == 2:
            break
        emit("blueprint_issues", issues=issues)
        request = (f"Design this construct: {description}\n\nYour previous blueprint:\n"
                   f"{json.dumps(blueprint)}\n\nIt has these layout problems; return a corrected "
                   "full blueprint:\n" + "\n".join(f"- {i}" for i in issues))
    if not best:
        raise ForgeError("The planner couldn't produce a blueprint.")
    blueprint, comps, _ = best
    emit("blueprint", title=blueprint.get("title", ""), components=[
        (c["name"], c.get("mirror_of"), c.get("detail")) for c in comps])

    # 2. Build every non-mirrored component in parallel, spreading the work across models.
    to_build = [c for c in comps if not c.get("mirror_of")]
    builders = team["builders"]

    def build(i_comp: tuple[int, dict], feedback: str = "") -> tuple[str, tuple[list, list]]:
        i, comp = i_comp
        model = builders[i % len(builders)]
        for attempt in range(2):
            try:
                raw = pool.json(model, BUILDER_PROMPT, _builder_request(comp, blueprint, comps, feedback),
                                comp["name"])
                parts, anims = _clean_parts(raw)
                if parts:
                    emit("built", who=comp["name"], model=model, parts=len(parts), animations=len(anims))
                    return comp["name"], (parts, anims)
            except ForgeError as e:
                if attempt:
                    emit("retry", who=comp["name"], reason=f"gave up on this component ({str(e)[:60]})")
                    return comp["name"], ([], [])
            model = builders[(i + 1) % len(builders)]  # second try on a different model
            emit("retry", who=comp["name"], reason=f"retrying on {model.split('/')[-1]}")
        return comp["name"], ([], [])

    emit("stage", text=f"{len(to_build)} builders at work")
    built: dict[str, tuple[list, list]] = {}
    with ThreadPoolExecutor(max_workers=len(builders) * 2) as ex:
        for name, result in ex.map(build, enumerate(to_build)):
            built[name] = result
    for c in comps:
        if c.get("mirror_of") and c["mirror_of"] in built:
            built[c["name"]] = _mirror_parts(*built[c["mirror_of"]])
            emit("mirrored", who=c["name"], source=c["mirror_of"])

    # 3. Assemble, then rebuild components that are broken or float away (one round).
    spec = _assemble(blueprint, comps, built, description)
    bad = {}
    for comp_part, errs in _component_errors(spec).items():
        for c in to_build:
            if comp_part.startswith(c["name"]) or comp_part == f"c_{c['name']}":
                bad.setdefault(c["name"], []).extend(errs)
    for name in _floating_components(spec, comps):
        src = next((c for c in comps if c["name"] == name), None)
        target = src["mirror_of"] if src and src.get("mirror_of") else name
        bad.setdefault(target, []).append(
            "Your parts didn't connect to the rest of the construct: start at the origin [0,0,0] "
            "(the attachment point) and stay inside your bounds.")
    if bad:
        emit("stage", text=f"Rebuilding {len(bad)} component(s) the inspector flagged")
        redo = [(i, c) for i, c in enumerate(to_build) if c["name"] in bad]
        with ThreadPoolExecutor(max_workers=len(builders)) as ex:
            futures = [ex.submit(build, ic, "\n".join(f"- {e}" for e in bad[ic[1]["name"]][:8])) for ic in redo]
            for f in futures:
                name, result = f.result()
                if result[0]:
                    built[name] = result
        for c in comps:
            if c.get("mirror_of") and c["mirror_of"] in built:
                built[c["name"]] = _mirror_parts(*built[c["mirror_of"]])
        spec = _assemble(blueprint, comps, built, description)

    spec = _drop_invalid(spec)
    if validate_spec(spec):
        raise ForgeError("The team's construct didn't pass validation.")
    report = inspect(spec)
    seconds = round(time.monotonic() - start)
    emit("team_done", score=report.score, parts=len(spec["parts"]), animations=len(spec["animations"]),
         seconds=seconds, tokens=pool.tokens, issues=report.issues)

    if not save:
        return spec
    path = store.save({k: v for k, v in spec.items() if k != "prompt"}, description)
    emit("saved", path=path)
    if launch_viewer:
        emit("launched", url=launch_viewer(path))
    return store.load(path)
