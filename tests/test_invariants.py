"""The architecture test. Two claims the rest of the submission rests on — replay runs with no
model in the loop, and the Surface seam is a seam and not a Playwright wrapper — are enforced by
the build, not by prose."""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "understudy"
LLM_MODULES = {"openai", "anthropic", "google", "groq", "understudy.discover"}


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _violations(package: str) -> list[str]:
    bad: list[str] = []
    for f in (SRC / package).rglob("*.py"):
        for name in _imports(f):
            if any(name == m or name.startswith(m + ".") for m in LLM_MODULES):
                bad.append(f"{f.relative_to(SRC)} imports {name}")
    return bad


def test_replay_never_imports_a_model_or_the_discovery_package():
    assert _violations("replay") == []


def test_surface_policy_evidence_catalog_console_never_import_a_model():
    for pkg in ("surface", "policy", "evidence", "catalog", "console"):
        assert _violations(pkg) == [], pkg


def test_the_llm_client_lives_only_in_discover():
    offenders = []
    for f in SRC.rglob("*.py"):
        if "discover" in f.parts:
            continue
        for name in _imports(f):
            if name.split(".")[0] in {"openai", "anthropic", "groq"}:
                offenders.append(str(f.relative_to(SRC)))
    assert offenders == []


def test_the_surface_protocol_imports_no_browser_driver():
    """REPORT §1 and §4 claim `surface/protocol.py` is the seam a desktop resolver would implement.
    That is only true while the seam itself names no browser: the moment it imports Playwright, the
    Observation/TargetDescriptor/act contract has quietly become a browser contract."""
    seam = SRC / "surface" / "protocol.py"
    drivers = {"playwright", "selenium", "puppeteer", "pyppeteer"}
    assert {n for n in _imports(seam) if n.split(".")[0] in drivers} == set()
    # ...and everything the seam does import is either stdlib or our own pure-data schema.
    assert {n.split(".")[0] for n in _imports(seam)} <= {"__future__", "dataclasses", "typing", "understudy"}
