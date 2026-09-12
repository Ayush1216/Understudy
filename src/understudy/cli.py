"""`python -m understudy <cmd>`: the command line over everything.

Result JSON goes to stdout and a one-line verdict to stderr, so a caller can parse one and a
human can read the other. The exit code is the result's: 0 for success OR a declared business
outcome (a legitimate answer, not an error), 1 for a failure, 2 when a human was needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn

from understudy.catalog import Catalog, load_catalog
from understudy.console import ConsoleEscalator, InterventionStore, build_app, serve
from understudy.discover import ConfigurationError, DiscoveryOptions, DiscoveryRun, load_env_file
from understudy.discover.client import ENV_FILE
from understudy.replay import ReplayOptions, ReplayRun
from understudy.schema import Capability, RunResult, exit_code

# How long a parked run waits for an operator. The console's store owns the timeout: it aborts
# the intervention on the record. The executor's own timer is only a backstop, so it gets
# more, or it fires first and leaves the store's entry open forever.
HUMAN_TIMEOUT_S = 600.0

_DISCOVERY_EXIT = {"completed": 0, "stopped": 1, "escalated": 2}


def main(argv: list[str] | None = None) -> int:
    load_env_file(ENV_FILE)
    try:
        args = _parser().parse_args(argv)
        return args.fn(args)
    except SystemExit as e:  # argparse's --help / usage error; an in-process caller gets the code
        if isinstance(e.code, str):  # a usage error raised deeper than the parser: a message, not a code
            print(e.code, file=sys.stderr)
            return 2
        return int(e.code or 0)
    except (KeyboardInterrupt, asyncio.CancelledError):  # Ctrl-C; with the console mounted it arrives as a cancel
        print("interrupted", file=sys.stderr)
        return 130


# ---- commands -----------------------------------------------------------------------------------


def _app(args: argparse.Namespace) -> int:
    print(f"target app: http://{args.host}:{args.port}/  (tenants alpha and beta; fault panel at /)", file=sys.stderr)
    uvicorn.run("target_app.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


def _discover(args: argparse.Namespace) -> int:
    opts = DiscoveryOptions(
        goal=args.goal, entry_url=args.url, inputs=_inputs(args), secret_names=args.secret, name=args.name,
        tenant=args.tenant, app=args.app, headless=not args.headed, slow_mo_ms=args.slow_mo, echo=args.echo,
        operator_policy_path=args.policy,
        evidence_root=args.evidence, capabilities_dir=args.capabilities, max_steps=args.max_steps, max_risk=args.max_risk,
    )
    try:
        result = asyncio.run(_drive(DiscoveryRun(opts), args))
    except (ConfigurationError, ValueError) as e:  # no API key, or an unreadable policy: fails
        print(e, file=sys.stderr)                          # before a browser opens, with the fix
        return 1
    if result.capability_path is not None:
        print(result.capability_path)
    print(f"discover {result.status}: {result.reason} ({result.llm_steps} model turns; evidence {result.evidence_dir})", file=sys.stderr)
    return _DISCOVERY_EXIT[result.status]


def _replay(args: argparse.Namespace) -> int:
    try:
        cap = Capability.model_validate_json(args.artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot load {args.artifact}: {e}", file=sys.stderr)
        return 1
    return _replay_cap(cap, args)


def _invoke(args: argparse.Namespace) -> int:
    """The agent path: by name, from the catalog, approved artifacts only."""
    name, version = _name_version(args.name)
    try:
        cap = _catalog(args).get(name, version)
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 1
    if cap.approval != "approved":
        print(f"refusing to invoke {cap.key}: approval is {cap.approval!r}; a human promotes a draft with "
              f"`understudy catalog approve {cap.key}`", file=sys.stderr)
        return 1
    return _replay_cap(cap, args)


def _replay_cap(cap: Capability, args: argparse.Namespace) -> int:
    opts = ReplayOptions(
        inputs=_inputs(args), tenant=args.tenant, headless=not args.headed, slow_mo_ms=args.slow_mo, echo=args.echo,
        allow_draft=args.allow_draft,
        operator_policy_path=args.policy, evidence_root=args.evidence, escalation_timeout_s=HUMAN_TIMEOUT_S + 30,
    )
    run = ReplayRun(cap, opts)
    if args.inject:
        mode, _, step = args.inject.partition("@")
        if mode == "error_rate":
            print("--inject: error_rate is sticky and probabilistic, so one fault before one step "
                  "cannot express it; arm it from the target app's panel at / instead", file=sys.stderr)
            return 2
        if mode not in INJECTABLE:
            print(f"--inject: unknown mode {mode!r}; one of {', '.join(INJECTABLE)}", file=sys.stderr)
            return 2
        step = step or next((s.id for s in reversed(cap.steps) if s.expects_navigation), cap.steps[-1].id)
        if step not in {s.id for s in cap.steps}:
            print(f"--inject: no step {step!r} in {cap.key}; steps are {[s.id for s in cap.steps]}", file=sys.stderr)
            return 2
        opts.before_step[step] = _arm(run, mode)
        print(f"armed fault {mode!r} on the browser's session before step {step}", file=sys.stderr)
    result = asyncio.run(_drive(run, args))
    print(result.model_dump_json(indent=2))
    print(_verdict(result), file=sys.stderr)
    return exit_code(result)


def _catalog_list(args: argparse.Namespace) -> int:
    for e in _catalog(args).list():
        s = e["stability"]
        tail = f"  runs {s['runs']} ok {s['successes']} outcomes {s['business_outcomes']} failed {s['failures']} escalated {s['escalations']}" if s else ""
        print(f"{e['key']:<36} {e['approval']:<10} {e['title']}{tail}")
    return 0


def _catalog_describe(args: argparse.Namespace) -> int:
    try:
        print(json.dumps(_catalog(args).describe(*_name_version(args.name)), indent=2))
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 1
    return 0


def _catalog_tools(args: argparse.Namespace) -> int:
    print(json.dumps(_catalog(args).tool_schemas(include_draft=args.include_draft), indent=2))
    return 0


def _catalog_approve(args: argparse.Namespace) -> int:
    cat = _catalog(args)
    try:
        cap = cat.get(*_name_version(args.name))
        cat.approve(cat.paths[cap.key])
    except (KeyError, ValueError) as e:
        print(e.args[0], file=sys.stderr)
        return 1
    print(f"approved {cap.key} ({cat.paths[cap.key]})", file=sys.stderr)
    return 0


# ---- running a live session ---------------------------------------------------------------------


async def _drive(run: ReplayRun | DiscoveryRun, args: argparse.Namespace) -> Any:
    """prepare / execute / close, with the operator console mounted on this loop when asked."""
    await run.prepare()  # a discovery run raises here (no key, no browser); a replay run records a pending result
    try:
        if args.console and run.session is not None:  # a run refused before a browser opened has nothing to take over
            engine = run.engine if isinstance(run, ReplayRun) else run.policy
            return await with_console(run, engine, args.console_port, run.execute)
        return await run.execute()
    finally:
        await run.close()


async def with_console(run: ReplayRun | DiscoveryRun, engine: Any, port: int, execute: Callable[[], Awaitable[Any]]) -> Any:
    store = InterventionStore(run.evidence, run.logger)
    server, task = await serve(build_app(run, store, engine), port=port)
    # uvicorn took SIGINT when it started serving. Take it back: a Ctrl-C must cancel the RUN,
    # whose teardown below stops the server — not stop the server and leave the run parked.
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, asyncio.current_task().cancel)
    url = f"http://127.0.0.1:{port}"
    run.escalator = ConsoleEscalator(run, store, url=url, timeout_s=HUMAN_TIMEOUT_S)
    print(f"operator console: {url}", file=sys.stderr)
    try:
        return await execute()
    finally:
        # An open operator page holds an event stream that never ends, and the server's shutdown
        # waits for every connection to drop (asyncio's wait_closed, 3.12+). Drop them.
        for c in list(server.server_state.connections):
            c.transport.close()
        server.should_exit = True
        await task


def _arm(run: ReplayRun, mode: str) -> Callable[[], Awaitable[None]]:
    """A before_step hook arming a target-app fault on the BROWSER's session (the request
    context shares its cookies), against the tenant-resolved entry origin."""
    async def hook() -> None:
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(run.cap.target.entry_url))
        r = await run.session.context.request.post(f"{origin}/_inject", data={"mode": mode, "ttl": 1})
        if not r.ok:
            raise RuntimeError(f"/_inject refused {mode!r}: {await r.text()}")
    return hook


def _verdict(r: RunResult) -> str:
    if r.status == "success":
        return f"success: outputs {json.dumps(r.outputs)}  (evidence {r.evidence_dir})"
    if r.status == "business_outcome":
        return f"business_outcome {r.code} (legitimate answer; exit 0): {r.message}  (evidence {r.evidence_dir})"
    if r.status == "escalated":
        return f"escalated {r.reason} at {r.at_step_id}: intervention {r.intervention_id}  (evidence {r.evidence_dir})"
    e = r.error
    return f"failed {e.kind} at {e.step_id or 'run'}: expected {e.expected}; observed {e.observed}  (evidence {r.evidence_dir})"


# ---- helpers ------------------------------------------------------------------------------------


def _catalog(args: argparse.Namespace) -> Catalog:
    cat = load_catalog(args.capabilities, evidence_root=args.evidence)
    for bad in cat.invalid:
        print(f"invalid: {bad.path}: {bad.reason.splitlines()[0]}", file=sys.stderr)
    return cat


def _name_version(s: str) -> tuple[str, str | None]:
    name, _, version = s.partition("@")
    return name, version or None


# The target app's per-request fault modes. `--inject` is a property of the DEMO, not the engine —
# they are listed here so a typo is a usage error instead of an INTERNAL failure six steps in.
# `error_rate` is deliberately absent: it is sticky and probabilistic, so it cannot be expressed
# by a flag that arms one fault before one step. Arm it from the app's panel instead.
INJECTABLE = ("not_found", "validation", "permission", "maintenance",
              "session_expired", "app_error", "slow")


def _inputs(args: argparse.Namespace) -> dict[str, str]:
    """`--input` twice for the same name is a typo, not an override: silently taking the last
    one would run the capability against something the caller did not ask for."""
    seen: dict[str, str] = {}
    for name, value in args.input:
        if name in seen and seen[name] != value:
            raise SystemExit(f"--input {name} given twice ({seen[name]!r} and {value!r}); pick one")
        seen[name] = value
    return seen


def _policy_path(s: str) -> Path:
    """Checked here rather than at load time: the policy is the security boundary, so a path
    typo must be a usage error, never a run that proceeds under something else."""
    p = Path(s)
    if not p.is_file():
        raise argparse.ArgumentTypeError(f"no operator policy at {p}")
    return p


def _kv(s: str) -> tuple[str, str]:
    name, sep, value = s.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {s!r}")
    return name, value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="understudy", description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def inputs(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--input", action="append", type=_kv, default=[], metavar="NAME=VALUE", help="typed input; repeatable")
        sp.add_argument("--tenant", help="replay the artifact's tenant override")
        sp.add_argument("--policy", type=_policy_path, default=Path("config/policy.toml"), metavar="PATH",
                        help="operator policy (the security boundary)")

    def dirs(sp: argparse.ArgumentParser, *, capabilities: bool = True) -> None:
        if capabilities:
            sp.add_argument("--capabilities", type=Path, default=Path("capabilities"), metavar="DIR")
        sp.add_argument("--evidence", type=Path, default=Path("evidence"), metavar="DIR")

    def browser(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--headed", action="store_true", help="show the browser")
        sp.add_argument("--slow-mo", type=int, default=0, metavar="MS",
                        help="pause MS between browser operations so a person can watch (try 500)")
        sp.add_argument("--echo", action="store_true",
                        help="print one line per event to stderr as it happens; a provider backoff is "
                             "then visibly a backoff rather than a hang")
        sp.add_argument("--console", action="store_true", help="mount the operator console on this run's own event loop")
        sp.add_argument("--console-port", type=int, default=7373)

    a = sub.add_parser("app", help="serve the hostile target app (tenants alpha and beta)")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=4599)
    a.set_defaults(fn=_app)

    d = sub.add_parser("discover", help="an LLM drives the UI once; the run compiles into a capability artifact")
    d.add_argument("--goal", required=True)
    d.add_argument("--url", required=True, help="entry URL")
    d.add_argument("--secret", action="append", default=[], metavar="NAME", help="env var the model may type as {{secrets.NAME}}; repeatable")
    d.add_argument("--name", help="capability name (default: derived from the goal)")
    d.add_argument("--app", default="target")
    d.add_argument("--max-steps", type=int, default=30)
    d.add_argument("--max-risk", choices=("safe", "sensitive", "irreversible"), default="safe")
    inputs(d), dirs(d), browser(d)
    d.set_defaults(fn=_discover)

    r = sub.add_parser("replay", help="replay an artifact file with no model in the loop (a human testing a recording)")
    r.add_argument("artifact", type=Path, metavar="ARTIFACT.json")
    r.add_argument("--inject", metavar="MODE[@STEP_ID]",
                   help=f"arm a target-app fault right before a step (default: the last navigating "
                        f"step). MODE is one of {', '.join(INJECTABLE)}")
    r.add_argument("--allow-draft", action="store_true", help="run a draft; invoke never does")
    inputs(r), dirs(r, capabilities=False), browser(r)
    r.set_defaults(fn=_replay)

    i = sub.add_parser("invoke", help="the agent path: run an APPROVED capability from the catalog by name")
    i.add_argument("name", metavar="NAME[@VERSION]")
    inputs(i), dirs(i)
    i.set_defaults(fn=_invoke, headed=False, slow_mo=0, echo=False, console=False, console_port=0, inject=None, allow_draft=False)

    c = sub.add_parser("catalog", help="list, describe, export as tools, or approve capabilities")
    cs = c.add_subparsers(dest="sub", required=True)
    for name, fn in (("list", _catalog_list), ("describe", _catalog_describe), ("tools", _catalog_tools), ("approve", _catalog_approve)):
        x = cs.add_parser(name)
        if name in ("describe", "approve"):
            x.add_argument("name", metavar="NAME[@VERSION]")
        if name == "tools":
            x.add_argument("--include-draft", action="store_true")
        dirs(x)
        x.set_defaults(fn=fn)
    return p
