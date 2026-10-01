"""The differential fuzz runner, and the self-check that guards its findings.

A hand-written generator on 2026-09-06 produced 260 false findings in 800
cases. A shared runner whose self-check refuses findings it cannot trust is
worth more than a per-session rewrite. Mailman #54.
"""
from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from mailman import fuzz
from mailman.cli import main

HERE = "tests.test_fuzz"


def words(rng: random.Random) -> tuple:
    return (" ".join(rng.choice(["a", "b", "", "c d"]) for _ in range(rng.randint(0, 6))),)


def split_model(text: str) -> list[str]:
    return text.split()


def split_target(text: str) -> list[str]:
    return [part for part in text.split(" ") if part]


def split_buggy(text: str) -> list[str]:
    return text.split(" ")


def numbers(rng: random.Random) -> int:
    return rng.randint(-5, 5)


def reciprocal_model(value: int) -> float:
    return 1 / value


def reciprocal_raises_value_error(value: int) -> float:
    if value == 0:
        raise ValueError("zero")
    return 1 / value


def reciprocal_other_message(value: int) -> float:
    if value == 0:
        raise ZeroDivisionError("a different message")
    return 1 / value


class _Opaque:
    """A result without __eq__: two calls never compare equal."""


def opaque(value: int) -> _Opaque:
    return _Opaque()


def lists(rng: random.Random) -> tuple:
    return ([rng.randint(0, 9) for _ in range(rng.randint(0, 5))],)


def sort_in_place(values: list[int]) -> list[int]:
    values.sort()
    values.append(-1)
    return values[:-1]


def sorted_model(values: list[int]) -> list[int]:
    return sorted(values)


def _run(target: str, model: str, generator: str, **options) -> dict:
    return fuzz.run_fuzz(
        target=f"{HERE}:{target}", model=f"{HERE}:{model}",
        generator=f"{HERE}:{generator}", **options,
    )


class RunTests(unittest.TestCase):
    def test_an_agreeing_target_is_clean(self) -> None:
        record = _run("split_target", "split_model", "words", cases=300, seed=4)
        self.assertEqual(record["status"], fuzz.CLEAN)
        self.assertTrue(record["self_check"]["passed"])
        self.assertEqual(record["finding_count"], 0)

    def test_a_disagreement_is_recorded_with_a_seed_that_replays_it(self) -> None:
        record = _run("split_buggy", "split_model", "words", cases=300, seed=4)
        self.assertEqual(record["status"], fuzz.FINDINGS)
        first = record["findings"][0]
        replayed = _run("split_buggy", "split_model", "words", case_seed=first["case_seed"])
        self.assertEqual(replayed["finding_count"], 1)
        self.assertEqual(replayed["findings"][0]["arguments"], first["arguments"])

    def test_the_exception_type_is_compared_and_the_message_is_not(self) -> None:
        differs = _run("reciprocal_raises_value_error", "reciprocal_model", "numbers", cases=200)
        self.assertEqual(differs["status"], fuzz.FINDINGS)
        self.assertEqual(differs["findings"][0]["target"]["raised"], "builtins.ValueError")
        self.assertEqual(differs["findings"][0]["model"]["raised"], "builtins.ZeroDivisionError")
        agrees = _run("reciprocal_other_message", "reciprocal_model", "numbers", cases=200)
        self.assertEqual(agrees["status"], fuzz.CLEAN)

    def test_findings_are_refused_when_the_target_disagrees_with_itself(self) -> None:
        record = _run("opaque", "reciprocal_model", "numbers", cases=50)
        self.assertEqual(record["status"], fuzz.REFUSED)
        self.assertFalse(record["self_check"]["passed"])
        self.assertEqual(record["findings"], [])
        self.assertIsNone(record["finding_count"])

    def test_a_call_that_mutates_its_arguments_does_not_leak_into_the_model(self) -> None:
        record = _run("sort_in_place", "sorted_model", "lists", cases=200)
        self.assertEqual(record["status"], fuzz.CLEAN)

    def test_the_same_seed_gives_the_same_cases(self) -> None:
        self.assertEqual(fuzz.case_seeds(9, 20), fuzz.case_seeds(9, 20))
        self.assertNotEqual(fuzz.case_seeds(9, 20), fuzz.case_seeds(10, 20))

    def test_a_malformed_spec_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            fuzz.load_callable("tests.test_fuzz")


MODULE = textwrap.dedent(
    """
    import random

    def generate(rng: random.Random):
        return (rng.randint(-3, 3),)

    def target(value):
        return abs(value) if value != 2 else -2

    def model(value):
        return abs(value)
    """
)


class InterpreterTests(unittest.TestCase):
    """The runner imports only the standard library, so the target's own
    environment interpreter can run it as a script."""

    def test_the_script_runs_under_another_interpreter_and_exits_on_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fuzzsubject.py").write_text(MODULE, encoding="utf-8")
            output = root / "fuzz.json"
            completed = subprocess.run(
                [sys.executable, fuzz.__file__, "--target", "fuzzsubject:target",
                 "--model", "fuzzsubject:model", "--generator", "fuzzsubject:generate",
                 "--cases", "100", "--path", str(root), "--output", str(output)],
                capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(completed.returncode, 1, completed.stderr)
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], fuzz.FINDINGS)
            self.assertIn("100/100", completed.stderr)

    def test_the_command_hands_the_run_to_the_named_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fuzzsubject2.py").write_text(MODULE, encoding="utf-8")
            output = root / "fuzz.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(
                    ["fuzz", "--target", "fuzzsubject2:model", "--model", "fuzzsubject2:model",
                     "--generator", "fuzzsubject2:generate", "--cases", "50",
                     "--path", str(root), "--output", str(output), "--python", sys.executable]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], fuzz.CLEAN)


if __name__ == "__main__":
    unittest.main()
