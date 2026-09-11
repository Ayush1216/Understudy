"""Shared fixtures: the hostile target app on an ephemeral port, a capability rewired to it,
and an operator policy that allows it. Integration tests drive a real Chromium against it."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from understudy.schema import Capability

from .fixtures import beta_override_dict, member_balance_dict


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def target_server() -> str:
    """Base URL of a live target_app. One server for the whole session; tests reset its state."""
    from target_app.app import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            httpx.get(base + "/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("target_app did not start")
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def reset_target(target_server: str) -> str:
    httpx.post(target_server + "/_reset")
    yield target_server
    httpx.post(target_server + "/_reset")


@pytest.fixture
def operator_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {"APP_OPERATOR": "teller1", "APP_PASSWORD": "password"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def alpha_capability(base_url: str, *, with_beta: bool = False, **overrides: Any) -> Capability:
    """The fixture capability with entry_url and declared origin pointed at a live server."""
    d = member_balance_dict()
    d["target"]["entry_url"] = f"{base_url}/t/alpha/"
    d["policy_declaration"]["origins"] = [base_url]
    if with_beta:
        beta = beta_override_dict()
        beta["entry_url"] = f"{base_url}/t/beta/"
        d["tenant_overrides"] = {"beta": beta}
        d["policy_declaration"]["path_patterns"] = ["/t/*/**"]  # every tenant's paths; the override has no policy field
    d.update(overrides)
    return Capability.model_validate(d)


def write_policy(tmp_path: Path, base_url: str, **risk: Any) -> Path:
    """An operator policy.toml that grants the live server; risk knobs overridable."""
    max_risk = risk.get("max_risk", "irreversible")
    approval = risk.get("require_approval_for", ["irreversible"])
    approval_toml = ", ".join(f'"{r}"' for r in approval)
    p = tmp_path / "policy.toml"
    p.write_text(
        f"""[allow]
origins = ["{base_url}"]
path_patterns = ["/t/*/**", "/_inject", "/_reset"]
action_types = ["navigate", "click", "type", "select", "press", "wait", "assert", "extract"]

[risk]
max_risk = "{max_risk}"
require_approval_for = [{approval_toml}]

[limits]
replay_timeout_ms = 120000
discovery_timeout_ms = 600000
max_steps = 60
"""
    )
    return p


@pytest.fixture
def policy_path(tmp_path: Path, target_server: str) -> Path:
    return write_policy(tmp_path, target_server)
