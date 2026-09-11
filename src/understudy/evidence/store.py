"""The per-run evidence directory. Every writer redacts at the sink, so nothing sensitive can
be written by construction.

    <root>/runs/<run_id>/
      run.jsonl · result.json · capability.json · screenshots/ · dom/
      interventions.json + human-actions/     (written by the escalation layer via write_json/write_bytes)
      discovery/transcript.jsonl              (discovery only)
"""

from __future__ import annotations

import json
import os
import re
from itertools import count
from pathlib import Path
from typing import Any

from understudy.schema import (
    BusinessOutcomeResult,
    Capability,
    EscalatedResult,
    FailedResult,
    SuccessResult,
)

from .logger import Redacts, jsonable

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(label: str) -> str:
    return _SLUG.sub("-", label.lower()).strip("-")[:80] or "x"


class EvidenceDir:
    def __init__(self, root: Path, run_id: str, redactor: Redacts) -> None:
        # `_inside` anchors on run_dir, so run_dir itself must not be able to leave root.
        # `Path("..").name == ".."` and `Path("").name == ""`, hence the explicit tuple.
        if run_id in ("", ".", "..") or Path(run_id).name != run_id:
            raise ValueError(f"run_id {run_id!r} is not a bare directory name")
        self.root = root
        self.run_id = run_id
        self._redactor = redactor
        self.run_dir = root / "runs" / run_id
        (self.run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "dom").mkdir(exist_ok=True)
        self._shots = count()
        self._doms = count()

    @property
    def log_path(self) -> Path:
        return self.run_dir / "run.jsonl"

    def screenshot_path(self, label: str) -> Path:
        return self.run_dir / "screenshots" / f"{next(self._shots):02d}-{_slug(label)}.png"

    def dom_path(self, label: str) -> Path:
        return self.run_dir / "dom" / f"{next(self._doms):02d}-{_slug(label)}.html"

    def write_bytes(self, path: str | Path, data: bytes) -> Path:
        p = self._inside(path)
        p.write_bytes(data)
        return p

    def write_text(self, path: str | Path, text: str) -> Path:
        p = self._inside(path)
        p.write_text(self._redactor.redact(text), encoding="utf-8")
        return p

    def write_json(self, name: str | Path, obj: Any) -> Path:
        p = self._inside(name)
        p.write_text(json.dumps(self._redactor.redact(jsonable(obj)), indent=2, ensure_ascii=False),
                     encoding="utf-8")
        return p

    def write_result(
        self, result: SuccessResult | BusinessOutcomeResult | EscalatedResult | FailedResult
    ) -> Path:
        return self.write_json("result.json", result)

    def write_capability(self, cap: Capability) -> Path:
        """The tenant-resolved artifact actually executed, not the one on disk."""
        return self.write_json("capability.json", cap)

    def write_transcript_line(self, obj: Any) -> None:
        p = self._inside("discovery/transcript.jsonl")
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self._redactor.redact(jsonable(obj)), ensure_ascii=False) + "\n")

    def relative(self, path: str | Path) -> str:
        """Repo-relative-ish (relative to root's parent), for result.evidence_dir."""
        return os.path.relpath(path, self.root.parent)

    def _inside(self, path: str | Path) -> Path:
        # `run_dir / absolute` returns the absolute path, so the containment check must come
        # after the join. Both sides resolved: tmp dirs on darwin sit behind a symlink.
        p = Path(path)
        already_rooted = p.is_absolute() or p.parts[: len(self.run_dir.parts)] == self.run_dir.parts
        # screenshot_path() and dom_path() hand back run-dir-rooted paths. Joining one again
        # nests a second evidence/runs/<id> inside the first, which is where the screenshots
        # silently went: present on disk, invisible to anyone reading the run directory.
        p = p if already_rooted else self.run_dir / p
        if not (self.run_dir / p).resolve().is_relative_to(self.run_dir.resolve()):
            raise ValueError(f"{str(path)!r} escapes the evidence directory")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
