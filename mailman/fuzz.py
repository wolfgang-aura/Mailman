"""Differential fuzzing of one function against a model of what it should do.

The caller supplies three `module:function` names: the target, a model, and a
generator. The generator takes a seeded `random.Random` and returns the
arguments for one case, as a tuple of positional arguments or a single value.
Each case runs the target and the model on deep copies of the same arguments
and compares what came back: the return value by `==`, or the exception type.

A run starts with a self-check: the target against itself on the same cases.
A clean generator and a deterministic target find nothing there, so any
finding in the self-check is the harness's own (an input the call mutates, a
result with no `==`, a nondeterministic target), and the differential run's
findings are refused rather than reported. On 2026-09-06 a hand-written
generator produced 260 false findings in 800 cases. Mailman #54.

This file imports only the standard library, so it runs under the target's own
environment interpreter: `python fuzz.py --target ... --output fuzz.json`.
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

FUZZ_SCHEMA_VERSION = 1
DEFAULT_CASES = 1000
#: Findings kept in the record. The count is always exact.
FINDING_LIMIT = 50
_REPR_LIMIT = 2000

CLEAN = "clean"
FINDINGS = "findings"
REFUSED = "refused"
#: Exit codes, so a caller can gate on the result without reading the record.
EXIT_CODES = {CLEAN: 0, FINDINGS: 1, REFUSED: 3}


def load_callable(spec: str) -> Callable[..., Any]:
    """Import `package.module:function` (the function may be dotted)."""
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"{spec!r} must be written module:function")
    value: Any = importlib.import_module(module_name)
    for part in attribute.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise ValueError(f"{spec} is not callable")
    return value


def _short(value: Any) -> str:
    try:
        text = repr(value)
    except Exception as error:  # noqa: BLE001 -- a broken __repr__ is still a value
        text = f"<unrepresentable {type(value).__name__}: {error}>"
    return text if len(text) <= _REPR_LIMIT else text[:_REPR_LIMIT] + "...[truncated]"


def outcome(function: Callable[..., Any], arguments: tuple[Any, ...]) -> dict[str, Any]:
    """What one call did: the value it returned or the exception it raised."""
    try:
        value = function(*copy.deepcopy(arguments))
    except Exception as error:  # noqa: BLE001 -- the exception is the outcome
        kind = type(error)
        return {
            "raised": f"{kind.__module__}.{kind.__qualname__}",
            "message": _short(str(error)),
        }
    return {"returned": value}


def same(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Whether two outcomes agree: equal values, or the same exception type."""
    if "raised" in first or "raised" in second:
        return first.get("raised") == second.get("raised")
    try:
        return bool(first["returned"] == second["returned"])
    except Exception:  # noqa: BLE001 -- an ambiguous == (numpy) is a disagreement
        return False


def _recorded(result: dict[str, Any]) -> dict[str, Any]:
    if "raised" in result:
        return result
    return {"returned": _short(result["returned"])}


def case_seeds(seed: int, cases: int) -> list[int]:
    """One seed per case, each enough on its own to regenerate that case."""
    generator = random.Random(seed)
    return [generator.getrandbits(48) for _ in range(cases)]


def generate(generator: Callable[[random.Random], Any], case_seed: int) -> tuple[Any, ...]:
    value = generator(random.Random(case_seed))
    return value if isinstance(value, tuple) else (value,)


def compare(
    target: Callable[..., Any],
    model: Callable[..., Any],
    generator: Callable[[random.Random], Any],
    seeds: list[int],
    *,
    progress: Callable[[int, int, int], None] = lambda done, total, found: None,
) -> tuple[int, list[dict[str, Any]]]:
    """Run every case; return the number of disagreements and the first few."""
    count = 0
    kept: list[dict[str, Any]] = []
    for index, case_seed in enumerate(seeds):
        arguments = generate(generator, case_seed)
        got = outcome(target, arguments)
        wanted = outcome(model, arguments)
        if not same(got, wanted):
            count += 1
            if len(kept) < FINDING_LIMIT:
                kept.append(
                    {
                        "case": index,
                        "case_seed": case_seed,
                        "arguments": _short(arguments),
                        "target": _recorded(got),
                        "model": _recorded(wanted),
                    }
                )
        if (index + 1) % 100 == 0 or index + 1 == len(seeds):
            progress(index + 1, len(seeds), count)
    return count, kept


def run_fuzz(
    *,
    target: str,
    model: str,
    generator: str,
    cases: int = DEFAULT_CASES,
    seed: int = 0,
    case_seed: int | None = None,
    paths: list[Path] | None = None,
    progress: Callable[[str, int, int, int], None] = lambda stage, done, total, found: None,
) -> dict[str, Any]:
    """Self-check, then the differential run; the record of both."""
    if cases < 1:
        raise ValueError("cases must be at least 1")
    for path in reversed(paths or []):
        sys.path.insert(0, str(Path(path).resolve()))
    target_function = load_callable(target)
    model_function = load_callable(model)
    generator_function = load_callable(generator)
    seeds = [case_seed] if case_seed is not None else case_seeds(seed, cases)
    record: dict[str, Any] = {
        "schema_version": FUZZ_SCHEMA_VERSION,
        "target": target,
        "model": model,
        "generator": generator,
        "seed": seed,
        "case_seed": case_seed,
        "cases": len(seeds),
        "python": sys.version.split()[0],
        "started_at": datetime.now(UTC).isoformat(),
    }
    found, examples = compare(
        target_function, target_function, generator_function, seeds,
        progress=lambda done, total, count: progress("self-check", done, total, count),
    )
    record["self_check"] = {
        "compared": "target against itself",
        "findings": found,
        "examples": examples,
        "passed": found == 0,
    }
    if found:
        record.update(
            {
                "status": REFUSED,
                "finding_count": None,
                "findings": [],
                "detail": (
                    f"the target disagreed with itself on {found} of {len(seeds)} "
                    "cases, so a disagreement with the model would prove nothing. "
                    "Look for an argument the call mutates, a result without ==, "
                    "or a nondeterministic target."
                ),
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
        return record
    found, examples = compare(
        target_function, model_function, generator_function, seeds,
        progress=lambda done, total, count: progress("differential", done, total, count),
    )
    record.update(
        {
            "status": FINDINGS if found else CLEAN,
            "finding_count": found,
            "findings": examples,
            "detail": (
                f"{found} of {len(seeds)} cases disagreed with the model"
                if found
                else f"all {len(seeds)} cases agreed with the model"
            ),
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )
    return record


def write_record(path: Path, record: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="module:function under test")
    parser.add_argument("--model", required=True, help="module:function it should agree with")
    parser.add_argument(
        "--generator", required=True,
        help="module:function taking a random.Random, returning one case's arguments",
    )
    parser.add_argument("--cases", type=int, default=DEFAULT_CASES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--case-seed", type=int,
        help="replay one recorded case by its case_seed instead of a full run",
    )
    parser.add_argument(
        "--path", type=Path, action="append", default=[],
        help="directory to import from, such as the target workspace; repeatable",
    )
    parser.add_argument("--output", type=Path, required=True)


def fuzz_arguments(arguments: argparse.Namespace) -> list[str]:
    """The same options again, for a run under another interpreter."""
    argv = [
        "--target", arguments.target, "--model", arguments.model,
        "--generator", arguments.generator, "--cases", str(arguments.cases),
        "--seed", str(arguments.seed), "--output", str(Path(arguments.output).resolve()),
    ]
    if arguments.case_seed is not None:
        argv += ["--case-seed", str(arguments.case_seed)]
    for path in arguments.path:
        argv += ["--path", str(Path(path).resolve())]
    return argv


def _progress(stage: str, done: int, total: int, found: int) -> None:
    print(f"{stage}: {done}/{total} cases, {found} disagreeing", file=sys.stderr, flush=True)


def run_from_arguments(arguments: argparse.Namespace) -> int:
    record = run_fuzz(
        target=arguments.target,
        model=arguments.model,
        generator=arguments.generator,
        cases=arguments.cases,
        seed=arguments.seed,
        case_seed=arguments.case_seed,
        paths=arguments.path,
        progress=_progress,
    )
    write_record(arguments.output, record)
    print(json.dumps(summary(record, arguments.output), indent=2))
    return EXIT_CODES[record["status"]]


def summary(record: dict[str, Any], output: Path) -> dict[str, Any]:
    return {
        "status": record["status"],
        "cases": record["cases"],
        "self_check_passed": record["self_check"]["passed"],
        "finding_count": record["finding_count"],
        "detail": record["detail"],
        "record": str(Path(output).resolve()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    return run_from_arguments(parser.parse_args(argv))


if __name__ == "__main__":
    # Run as a script, this file's folder is first on sys.path, and its
    # sibling modules would shadow a target's own top-level names.
    if sys.path and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
        sys.path.pop(0)
    raise SystemExit(main())
