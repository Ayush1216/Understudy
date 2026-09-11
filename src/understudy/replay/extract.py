"""Read declared outputs from the current screen. Called AT the `extract` step that names them,
so a value that only exists on an intermediate screen is captured before the page moves on."""

from __future__ import annotations

import re
from typing import Any

from understudy.schema import OutputSpec
from understudy.surface import Surface, SurfaceError

_NOT_NUMERIC = re.compile(r"[^0-9.\-]")


class OutputMissing(Exception):
    def __init__(self, name: str, expected: str, observed: str) -> None:
        super().__init__(f"output {name!r} is missing: {observed}")
        self.name = name
        self.expected = expected
        self.observed = observed


def transform(raw: str, how: str) -> str:
    if how == "trim":
        return raw.strip()
    if how == "digits_only":
        return re.sub(r"\D", "", raw)
    if how == "currency_to_number":
        return _NOT_NUMERIC.sub("", raw)
    if how == "upper":
        return raw.upper()
    if how == "lower":
        return raw.lower()
    return raw


async def extract_outputs(specs: list[OutputSpec], surface: Surface) -> dict[str, Any]:
    """Required-and-empty raises OutputMissing. `secret` outputs are extracted to prove the step
    worked, then dropped from the returned dict."""
    out: dict[str, Any] = {}
    for spec in specs:
        src = spec.source
        where = f"{src.target.label or src.target.role}" if src.target else f"URL /{src.url_pattern}/"
        raw: str | None
        try:
            if src.kind == "text_of":
                raw = await surface.read_text(src.target)
            elif src.kind == "attribute_of":
                raw = await surface.read_attribute(src.target, src.attribute)
            else:
                m = re.search(src.url_pattern, await surface.url())
                raw = m.group(spec.name) if m and spec.name in m.groupdict() else None
            observed = "empty value" if raw is None or not raw.strip() else ""
        except SurfaceError as e:
            raw, observed = None, e.observed or e.message
        value: Any = transform(raw, src.transform) if raw is not None else ""
        if value == "":
            if spec.required:
                raise OutputMissing(spec.name, f"a value for {spec.name} at {where}", observed)
            continue
        if spec.type in ("number", "currency"):
            try:
                value = float(value)
            except ValueError:
                raise OutputMissing(spec.name, f"a numeric value for {spec.name} at {where}", f"{raw!r} is not a number") from None
        elif spec.type == "boolean":
            value = value.strip().lower() in ("true", "1", "yes", "y", "on")
        if spec.sensitivity != "secret":
            out[spec.name] = value
    return out
