"""Template substitution: `{{inputs.x}}`, `{{secrets.X}}`, `{{env.X}}`.

Secrets are read from the environment AT SUBSTITUTION TIME and never stored: a rotated credential
takes effect on the next run, and no artifact, snapshot or log ever holds one.
"""

from __future__ import annotations

import os
import re
from typing import Any, TypeVar

from pydantic import BaseModel

REF = re.compile(r"\{\{\s*(inputs|secrets|env)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

M = TypeVar("M", bound=BaseModel)


class MissingReference(ValueError):
    """A template names an input that was not supplied or a variable that is not set."""


def references(s: str) -> set[tuple[str, str]]:
    return set(REF.findall(s))


def render(s: str, inputs: dict[str, Any]) -> str:
    def sub(m: re.Match[str]) -> str:
        kind, name = m.groups()
        if kind == "inputs":
            if name not in inputs:
                raise MissingReference(f"{{{{inputs.{name}}}}} was not supplied")
            return str(inputs[name])
        value = os.environ.get(name)
        if value is None:
            raise MissingReference(f"{{{{{kind}.{name}}}}}: {name} is not set in the environment")
        return value

    return REF.sub(sub, s)


def render_model(model: M, inputs: dict[str, Any]) -> M:
    """Substitute every string field, recursively, and re-validate. Templates may sit anywhere
    the recorder parameterized a value: an action, a descriptor's row anchor, a checkpoint."""
    return type(model).model_validate(_walk(model.model_dump(mode="json"), inputs))


def _walk(obj: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return render(obj, inputs)
    if isinstance(obj, dict):
        return {k: _walk(v, inputs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, inputs) for v in obj]
    return obj
