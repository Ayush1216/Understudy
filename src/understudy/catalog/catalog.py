"""The agent-facing capability catalog: every artifact in a directory, gated, listed, and
exported as JSON-Schema tools.

Three gates per file — JSON, Pydantic, lint — and a failure at any of them lands the file in
`invalid` with its reason. One bad artifact never takes down the catalog. Drafts are listed but
never exported as tools: an agent cannot call a recording no human has approved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from understudy.evidence import read_stability
from understudy.schema import Capability, ParamSpec, lint_capability


@dataclass
class Invalid:
    path: Path
    reason: str


def _semver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def tool_name(key: str) -> str:
    """`alpha.member.balance@1.0.0` -> `alpha__member__balance__v_1_0_0`. OpenAI tool names
    allow only `[A-Za-z0-9_-]`; the encoding is reversible via `capability_key_for`."""
    name, version = key.split("@")
    if "__" in name:
        raise ValueError(f"capability name {name!r} contains '__', which the tool-name encoding reserves")
    return name.replace(".", "__") + "__v_" + version.replace(".", "_")


def capability_key_for(tool: str) -> str:
    name, version = tool.rsplit("__v_", 1)
    return name.replace("__", ".") + "@" + version.replace("_", ".")


def write_capability(cap: Capability, dir: Path) -> Path:
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / f"{cap.name}.v{cap.version}.json"
    path.write_text(cap.model_dump_json(indent=2), encoding="utf-8")
    return path


_JSON_TYPE = {"string": "string", "number": "number", "boolean": "boolean", "enum": "string"}


def _parameters(inputs: list[ParamSpec]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for p in inputs:
        prop: dict[str, Any] = {"type": _JSON_TYPE[p.type], "description": p.description}
        if p.type == "enum":
            prop["enum"] = p.enum_values
        if p.pattern:
            prop["pattern"] = p.pattern
        props[p.name] = prop
    return {
        "type": "object",
        "properties": props,
        "required": [p.name for p in inputs if p.required],
        "additionalProperties": False,
    }


def _describe(cap: Capability) -> str:
    # `secret` outputs are dropped from the caller result; promising them here would be a lie.
    returns = ", ".join(f"{o.name} ({o.type})" for o in cap.outputs if o.sensitivity != "secret")
    codes = ", ".join(o.code for o in cap.outcomes)
    return f"{cap.title}. Returns: {returns or 'nothing'}. Business outcomes: {codes or 'none'}"


@dataclass
class Catalog:
    entries: dict[str, Capability]  # keyed by cap.key, "name@version"
    invalid: list[Invalid]
    paths: dict[str, Path]
    evidence_root: Path

    def get(self, name: str, version: str | None = None) -> Capability:
        versions = sorted((c.version for c in self.entries.values() if c.name == name), key=_semver)
        if not versions:
            raise KeyError(f"no capability named {name!r}; catalog has {sorted({c.name for c in self.entries.values()})}")
        if version is None:
            version = versions[-1]
        elif version not in versions:
            raise KeyError(f"{name}@{version} is not in the catalog; available versions: {versions}")
        return self.entries[f"{name}@{version}"]

    def describe(self, name: str, version: str | None = None) -> dict[str, Any]:
        return self.get(name, version).model_dump(mode="json")

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "name": c.name,
                "version": c.version,
                "title": c.title,
                "approval": c.approval,
                "tenant": c.target.tenant,
                "inputs": [
                    {"name": p.name, "type": p.type, "required": p.required,
                     "description": p.description, "sensitivity": p.sensitivity}
                    for p in c.inputs
                ],
                "outputs": [
                    {"name": o.name, "type": o.type, "description": o.description, "sensitivity": o.sensitivity}
                    for o in c.outputs
                ],
                "outcomes": [o.code for o in c.outcomes],
                "stability": read_stability(self.evidence_root, key, None),
            }
            for key, c in sorted(self.entries.items())
        ]

    def tool_schemas(self, *, include_draft: bool = False) -> list[dict[str, Any]]:
        """OpenAI function tools. Deprecated artifacts are never exported; drafts only on
        request, and only a human testing a recording should ask."""
        callable_ = {"approved", "draft"} if include_draft else {"approved"}
        return [
            {"type": "function", "function": {
                "name": tool_name(key), "description": _describe(c), "parameters": _parameters(c.inputs),
            }}
            for key, c in sorted(self.entries.items())
            if c.approval in callable_
        ]

    def approve(self, path: Path) -> Capability:
        """Promote draft -> approved. The one field a human flips; nothing else is touched."""
        cap = Capability.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if cap.approval == "deprecated":
            raise ValueError(f"{cap.key} is deprecated and cannot be approved")
        if cap.approval == "draft":
            cap = cap.model_copy(update={"approval": "approved"})
            path.write_text(cap.model_dump_json(indent=2), encoding="utf-8")
        if cap.key in self.entries:
            self.entries[cap.key] = cap
        return cap


def load_catalog(dir: Path, *, evidence_root: Path = Path("evidence")) -> Catalog:
    cat = Catalog({}, [], {}, evidence_root)
    for path in sorted(dir.glob("*.json")):
        try:
            cap = Capability.model_validate(json.loads(path.read_text(encoding="utf-8")))
            issues = lint_capability(cap)
        except (ValueError, OSError) as e:  # JSONDecodeError and ValidationError are both ValueError
            cat.invalid.append(Invalid(path, str(e)))
            continue
        if issues:
            cat.invalid.append(Invalid(path, "; ".join(f"{i.code}: {i.message}" for i in issues)))
        elif cap.key in cat.entries:
            cat.invalid.append(Invalid(path, f"duplicate key {cap.key}; already loaded from {cat.paths[cap.key].name}"))
        else:
            cat.entries[cap.key] = cap
            cat.paths[cap.key] = path
    return cat
