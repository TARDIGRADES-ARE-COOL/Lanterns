"""Find, list and load scene specs in constructs/ and samples/."""

import json
import re
from datetime import datetime
from pathlib import Path

from . import ROOT

CONSTRUCTS = ROOT / "constructs"
SAMPLES = ROOT / "samples"


def resolve(ref: str) -> Path:
    """Turn 'samples/dragon', a construct id, or a file path into a spec path."""
    candidates = [Path(ref), ROOT / ref, CONSTRUCTS / ref, SAMPLES / ref]
    for c in candidates:
        for p in (c, c.with_name(c.name + ".json")):
            if p.is_file():
                return p.resolve()
    raise FileNotFoundError(f"No scene spec found for '{ref}'")


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def list_constructs() -> list[Path]:
    """Saved constructs, newest first (ids start with a timestamp)."""
    return sorted(CONSTRUCTS.glob("*.json"), reverse=True)


def latest() -> Path | None:
    items = list_constructs()
    return items[0] if items else None


def web_path(path: Path) -> str:
    """URL path the viewer uses to fetch a spec, e.g. /samples/dragon.json."""
    rel = path.resolve().relative_to(ROOT)
    if rel.parts[0] not in ("constructs", "samples"):
        raise ValueError("Specs must live in constructs/ or samples/ to be viewed")
    return "/" + rel.as_posix()


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "construct"


def save(spec: dict, prompt: str) -> Path:
    """Save a new construct as constructs/<timestamp>_<slug>.json and return the path."""
    CONSTRUCTS.mkdir(exist_ok=True)
    construct_id = f"{datetime.now():%Y%m%d-%H%M%S}_{slugify(spec.get('title') or prompt)}"
    path = CONSTRUCTS / f"{construct_id}.json"
    save_as(path, spec, prompt)
    return path


def save_as(path: Path, spec: dict, prompt: str) -> None:
    """Write a construct to an existing path; its id always matches the file name."""
    spec = {**spec, "id": path.stem, "prompt": prompt}
    path.write_text(json.dumps(spec, indent=2) + "\n")
