import json
import re
import types
from pathlib import Path
from typing import Any

import pytest

from understudy.evidence import (
    EvidenceDir,
    RunLogger,
    new_intervention_id,
    new_run_id,
    read_stability,
    record_stability,
)
from understudy.schema import CapabilityRef, SuccessResult

from .fixtures import member_balance

SECRET = "hunter2"
MASK = "«redacted:secret»"


class StubRedactor:
    def redact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return obj.replace(SECRET, MASK)
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        return obj


@pytest.fixture
def log(tmp_path: Path) -> RunLogger:
    return RunLogger("replay-x", tmp_path / "run.jsonl", StubRedactor())


@pytest.fixture
def evidence(tmp_path: Path) -> EvidenceDir:
    return EvidenceDir(tmp_path / "evidence", "replay-x", StubRedactor())


# ---- ids ------------------------------------------------------------------------------------


def test_run_id_has_kind_timestamp_and_hex_suffix():
    assert re.fullmatch(r"replay-\d{8}-\d{6}-[0-9a-f]{6}", new_run_id("replay"))
    assert new_run_id("discover").startswith("discover-")
    assert new_run_id("replay") != new_run_id("replay")
    assert re.fullmatch(r"iv_[0-9a-f]{10}", new_intervention_id())


def test_unknown_run_kind_is_refused():
    with pytest.raises(ValueError, match="unknown run kind"):
        new_run_id("dream")


# ---- logger ---------------------------------------------------------------------------------


def test_events_carry_a_timestamp_and_a_monotonic_seq(log: RunLogger):
    a = log.emit("run.start", goal="g")
    b = log.emit("step.start", step_id="s1")
    assert (a["seq"], b["seq"]) == (1, 2)
    assert a["ts"].endswith("+00:00") and a["run_id"] == "replay-x"
    lines = [json.loads(l) for l in log.path.read_text().splitlines()]
    assert [e["seq"] for e in lines] == [1, 2]
    assert lines[1]["type"] == "step.start" and lines[1]["step_id"] == "s1"


def test_secret_reaches_neither_disk_nor_a_subscriber(log: RunLogger):
    seen: list[dict] = []
    log.subscribe(seen.append)
    # An object the redactor cannot descend into must be made JSON-native BEFORE redaction,
    # or json.dumps' fallback would stringify the secret past it.
    returned = log.emit("action.performed", value=SECRET, nested={"pw": [SECRET]},
                        opaque=types.SimpleNamespace(pw=SECRET))
    text = log.path.read_text()
    assert SECRET not in text and MASK in text
    assert seen == [returned]
    assert seen[0]["value"] == MASK and seen[0]["nested"]["pw"] == [MASK]
    assert MASK in seen[0]["opaque"] and SECRET not in seen[0]["opaque"]


def test_bytes_payload_is_base64_not_a_crash(log: RunLogger):
    assert log.emit("x", data=b"\x89PNG\r\n")["data"] == "iVBORw0K"


def test_reserved_header_keys_in_payload_are_refused(log: RunLogger):
    with pytest.raises(ValueError, match="reserved event keys: \\['seq'\\]"):
        log.emit("a", seq=999)
    with pytest.raises(ValueError, match="reserved"):
        log.emit("a", type="click")  # an Action's discriminator via **action.model_dump()
    assert log.emit("b")["seq"] == 1  # a refused emit burns no sequence number


def test_throwing_subscriber_is_dropped_and_others_still_called(log: RunLogger, capsys):
    calls: list[str] = []

    def bad(e: dict) -> None:
        calls.append("bad")
        raise RuntimeError("viewer died")

    log.subscribe(bad)
    log.subscribe(lambda e: calls.append("good"))
    log.emit("step.end")
    log.emit("step.end")
    assert calls == ["bad", "good", "good"]
    assert "viewer died" in capsys.readouterr().err


def test_unsubscribe_stops_delivery_and_tolerates_an_already_dropped_subscriber(log: RunLogger):
    calls: list[int] = []
    off = log.subscribe(lambda e: calls.append(e["seq"]))
    log.emit("a")
    off()
    log.emit("b")
    assert calls == [1]

    def bad(e: dict) -> None:
        raise RuntimeError

    off_bad = log.subscribe(bad)
    log.emit("c")
    off_bad()  # already auto-dropped; must not raise


def test_tail_returns_the_last_n_events_from_disk(log: RunLogger):
    assert log.tail(3) == []
    for i in range(5):
        log.emit("step.end", i=i)
    fresh = RunLogger("replay-x", log.path, StubRedactor())  # no in-memory buffer to lean on
    assert [e["i"] for e in fresh.tail(2)] == [3, 4]
    assert len(fresh.tail(50)) == 5


def test_tail_skips_a_line_torn_by_a_mid_write_kill(log: RunLogger):
    log.emit("step.end", i=0)
    with log.path.open("a") as f:
        f.write('{"seq": 99, "ty')
    assert [e["i"] for e in log.tail(5)] == [0]


def test_echo_prints_one_line_for_summary_events_only_and_never_on_stdout(tmp_path: Path, capsys):
    log = RunLogger("r", tmp_path / "run.jsonl", StubRedactor(), echo=True)
    log.emit("step.start", step_id="s1")
    log.emit("step.end", step_id="s1", ok=True, detail={"nested": 1})
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is the JSON result channel for `invoke`
    assert captured.err.count("\n") == 1
    assert "step.end" in captured.err and "step_id=s1" in captured.err and "nested" not in captured.err


# ---- evidence dir ---------------------------------------------------------------------------


def test_evidence_dir_layout(evidence: EvidenceDir, tmp_path: Path):
    assert evidence.run_dir == tmp_path / "evidence" / "runs" / "replay-x"
    assert evidence.log_path == evidence.run_dir / "run.jsonl"
    assert evidence.relative(evidence.run_dir) == "evidence/runs/replay-x"
    # Nothing on disk yet — not from EvidenceDir, and not from the RunLogger it hands its
    # log_path to: a run that dies at startup must leave no empty directory for whoever reads
    # `evidence/runs/` later. Each subdirectory appears with its first file.
    RunLogger("replay-x", evidence.log_path, evidence._redactor)
    assert not evidence.run_dir.exists()
    evidence.write_bytes(evidence.screenshot_path("entry"), b"\x89PNG")
    assert (evidence.run_dir / "screenshots").is_dir() and not (evidence.run_dir / "dom").exists()
    evidence.write_text(evidence.dom_path("failure"), "<html>")
    assert (evidence.run_dir / "dom").is_dir()


def test_screenshots_and_dom_snapshots_are_numbered_and_slugified(evidence: EvidenceDir):
    assert evidence.screenshot_path("entry").name == "00-entry.png"
    assert evidence.screenshot_path("s3 Post/Condition!").name == "01-s3-post-condition.png"
    assert evidence.dom_path("failure").name == "00-failure.html"
    assert evidence.dom_path("").name == "01-x.html"
    p = evidence.write_bytes(evidence.screenshot_path("success"), b"\x89PNG")
    assert p.read_bytes() == b"\x89PNG" and p.parent.name == "screenshots"


def test_path_traversal_is_refused(evidence: EvidenceDir, tmp_path: Path):
    with pytest.raises(ValueError, match="escapes"):
        evidence.write_text("../../evil.txt", "x")
    with pytest.raises(ValueError, match="escapes"):
        evidence.write_bytes(tmp_path / "outside.bin", b"x")
    with pytest.raises(ValueError, match="escapes"):
        evidence.write_json("dom/../../../evil.json", {})
    assert not (tmp_path / "evidence" / "evil.txt").exists()


@pytest.mark.parametrize("run_id", ["../x", "a/b", "/abs", "..", ".", ""])
def test_run_id_that_is_not_a_bare_directory_name_is_refused(tmp_path: Path, run_id: str):
    with pytest.raises(ValueError, match="not a bare directory name"):
        EvidenceDir(tmp_path / "evidence", run_id, StubRedactor())
    assert not (tmp_path / "x").exists() and not (tmp_path / "evidence").exists()


def test_text_json_and_transcript_writers_redact(evidence: EvidenceDir):
    t = evidence.write_text("human-actions/1.txt", f"typed {SECRET}")
    assert t.read_text() == f"typed {MASK}"
    j = evidence.write_json("interventions.json", [{"pw": SECRET, "o": types.SimpleNamespace(pw=SECRET)}])
    assert SECRET not in j.read_text() and json.loads(j.read_text())[0]["pw"] == MASK
    evidence.write_transcript_line({"role": "user", "content": SECRET})
    evidence.write_transcript_line({"role": "assistant", "content": "ok"})
    lines = (evidence.run_dir / "discovery" / "transcript.jsonl").read_text().splitlines()
    assert [json.loads(l)["content"] for l in lines] == [MASK, "ok"]


def test_result_and_capability_are_written_as_json(evidence: EvidenceDir):
    result = SuccessResult(
        status="success", run_id="replay-x", capability=CapabilityRef(id="c", name="n", version="1.0.0"),
        evidence_dir=evidence.relative(evidence.run_dir), outputs={"pw": SECRET}, steps_executed=1,
    )
    r = json.loads(evidence.write_result(result).read_text())
    assert r["status"] == "success" and r["outputs"] == {"pw": MASK}
    c = json.loads(evidence.write_capability(member_balance()).read_text())
    assert c["name"] == "alpha.member.balance"
    assert (evidence.run_dir / "result.json").exists() and (evidence.run_dir / "capability.json").exists()


# ---- stability sidecar ----------------------------------------------------------------------


def test_stability_read_modify_write_preserves_other_entries(tmp_path: Path):
    record_stability(tmp_path, "a@1.0.0", None, "success", {"s1": 0})
    record_stability(tmp_path, "a@1.0.0", "beta", "failed", {"s1": 3})
    before = read_stability(tmp_path, "a@1.0.0", "beta")
    record_stability(tmp_path, "a@1.0.0", None, "business_outcome", {"s1": 1})
    record_stability(tmp_path, "b@2.0.0", None, "escalated", {})
    base = read_stability(tmp_path, "a@1.0.0", "base")
    assert base is not None
    assert (base["runs"], base["successes"], base["business_outcomes"]) == (2, 1, 1)
    assert base["last_winning_rungs"] == {"s1": 1}
    assert read_stability(tmp_path, "a@1.0.0", "beta") == before
    assert read_stability(tmp_path, "b@2.0.0", None)["escalations"] == 1
    assert read_stability(tmp_path, "nope@0.0.0", None) is None


def test_repeated_records_update_one_entry_in_place(tmp_path: Path):
    for _ in range(3):
        record_stability(tmp_path, "a@1.0.0", "beta", "success", {})
    data = json.loads((tmp_path / "stability.json").read_text())
    assert list(data) == ["a@1.0.0"] and list(data["a@1.0.0"]) == ["beta"]
    assert data["a@1.0.0"]["beta"]["runs"] == 3
    assert not (tmp_path / "stability.json.tmp").exists()


def test_corrupt_sidecar_warns_and_starts_fresh_instead_of_crashing_the_run(tmp_path: Path, capsys):
    (tmp_path / "stability.json").write_text("{not json")
    assert read_stability(tmp_path, "a@1.0.0", None) is None
    assert record_stability(tmp_path, "a@1.0.0", None, "success", {})["runs"] == 1
    assert "corrupt" in capsys.readouterr().err
    assert read_stability(tmp_path, "a@1.0.0", None)["runs"] == 1


def test_a_run_dir_rooted_path_is_not_rooted_twice(tmp_path):
    """screenshot_path()/dom_path() hand back run-dir-rooted paths; write_bytes must not join
    them again or the file lands in evidence/runs/<id>/evidence/runs/<id>/... — on disk, but
    invisible to anyone reading the run directory."""
    ev = EvidenceDir(tmp_path / "evidence", "replay-1", StubRedactor())
    shot = ev.screenshot_path("entry")
    written = ev.write_bytes(shot, b"\x89PNG")
    assert written == shot
    assert written.read_bytes() == b"\x89PNG"
    assert "evidence/runs/replay-1/evidence" not in written.as_posix()
    assert [p.name for p in (ev.run_dir / "screenshots").iterdir()] == ["00-entry.png"]

    dom = ev.write_text(ev.dom_path("failure"), "<html></html>")
    assert dom.parent == ev.run_dir / "dom"
    # a plain relative name still roots at the run dir
    assert ev.write_text("notes.txt", "x").parent == ev.run_dir
