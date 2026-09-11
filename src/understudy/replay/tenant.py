"""Base artifact + per-tenant delta -> the capability a run actually executes.

Objects deep-merge; lists replace wholesale. The merged capability re-passes the same schema and
lint gates the on-disk one passed: a resolved capability can never be weaker than a stored one.
"""

from __future__ import annotations

from typing import Any

from understudy.schema import Aliases, Capability, assert_lints_clean


def resolve_for_tenant(cap: Capability, tenant: str | None) -> tuple[Capability, Aliases, dict[str, str]]:
    """Raises ValueError for an unknown tenant, a merge that fails validation, or LintError."""
    if tenant is None:
        assert_lints_clean(cap)
        return cap, Aliases(), {}
    ov = cap.tenant_overrides.get(tenant)
    if ov is None:
        raise ValueError(f"unknown tenant {tenant!r}; declared: {sorted(cap.tenant_overrides)}")
    unknown = set(ov.steps) - {s.id for s in cap.steps}
    if unknown:
        raise ValueError(f"tenant {tenant!r} overrides unknown steps {sorted(unknown)}")
    d = cap.model_dump(mode="json")
    d["target"]["tenant"] = tenant
    if ov.entry_url is not None:
        d["target"]["entry_url"] = ov.entry_url
    for step in d["steps"]:
        if step["id"] in ov.steps:
            _merge(step, ov.steps[step["id"]])
    if ov.outcomes is not None:
        d["outcomes"] = [o.model_dump(mode="json") for o in ov.outcomes]
    if ov.recoveries is not None:
        d["recoveries"] = [r.model_dump(mode="json") for r in ov.recoveries]
    d["tenant_overrides"] = {}  # resolved once; the evidence copy is the artifact that ran
    merged = Capability.model_validate(d)
    assert_lints_clean(merged)
    return merged, ov.aliases, dict(ov.frame_map)


def _merge(base: dict[str, Any], delta: dict[str, Any]) -> None:
    for k, v in delta.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
