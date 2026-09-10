"""Whose address ends up on a commit that leaves this machine."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman.identity import (
    Identity,
    IdentityError,
    apply_identity,
    author_violations,
    branch_commits,
    is_private_email,
    load_identity,
    resolve_identity,
    save_identity,
)


PRIVATE = "169568318+wolfgang-aura@users.noreply.github.com"
OTHER_PRIVATE = "9+someone@users.noreply.github.com"
PERSONAL = "someone.real@gmail.com"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _repository(root: Path) -> Path:
    """A clone whose only identity is the machine's, as a fresh one would be."""
    path = root / "repo"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Machine Default")
    _git(path, "config", "user.email", PERSONAL)
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-m", "base")
    return path


class PrivateEmailTests(unittest.TestCase):
    def test_the_noreply_suffix_is_recognised(self) -> None:
        self.assertTrue(is_private_email(PRIVATE))
        self.assertTrue(is_private_email("  " + PRIVATE.upper() + " "))
        self.assertFalse(is_private_email(PERSONAL))


class ResolutionTests(unittest.TestCase):
    def test_the_environment_beats_the_data_root(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            save_identity(root, Identity(name="From File", email=PRIVATE))
            resolved = resolve_identity(
                root,
                environment={
                    "MAILMAN_GIT_NAME": "From Env",
                    "MAILMAN_GIT_EMAIL": PRIVATE,
                },
            )
            self.assertEqual(resolved, Identity(name="From Env", email=PRIVATE))

    def test_nothing_configured_never_falls_back_to_the_machine(self) -> None:
        # The whole point: an unset identity stays visible instead of being
        # quietly replaced by whatever git config --global happens to hold.
        with TemporaryDirectory() as name:
            self.assertIsNone(resolve_identity(Path(name), environment={}))

    def test_a_saved_identity_round_trips(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            identity = Identity(name="wolfgang-aura", email=PRIVATE)
            save_identity(root, identity)
            self.assertEqual(load_identity(root), identity)

    def test_half_an_environment_is_an_error(self) -> None:
        with TemporaryDirectory() as name:
            with self.assertRaises(IdentityError):
                resolve_identity(
                    Path(name), environment={"MAILMAN_GIT_NAME": "No Address"}
                )

    def test_a_malformed_address_is_an_error(self) -> None:
        with TemporaryDirectory() as name:
            with self.assertRaises(IdentityError):
                resolve_identity(
                    Path(name),
                    environment={
                        "MAILMAN_GIT_NAME": "N",
                        "MAILMAN_GIT_EMAIL": "not-an-email",
                    },
                )

    def test_a_file_that_is_not_json_is_an_error(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            (root / "identity.json").write_text("{", encoding="utf-8")
            with self.assertRaises(IdentityError):
                load_identity(root)


class ApplicationTests(unittest.TestCase):
    def test_the_applied_identity_beats_the_repository_default(self) -> None:
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            apply_identity(repository, Identity(name="wolfgang-aura", email=PRIVATE))
            (repository / "file.txt").write_text("changed\n", encoding="utf-8")
            _git(repository, "commit", "-am", "change")
            self.assertEqual(_git(repository, "log", "-1", "--format=%ae"), PRIVATE)


class ViolationTests(unittest.TestCase):
    def test_a_commit_under_the_machine_identity_is_a_violation(self) -> None:
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            base = _git(repository, "rev-parse", "HEAD")
            (repository / "file.txt").write_text("changed\n", encoding="utf-8")
            _git(repository, "commit", "-am", "change")

            commits = branch_commits(repository, base)
            self.assertEqual(len(commits), 1)
            self.assertEqual(commits[0]["author_email"], PERSONAL)

            violations = author_violations(
                commits, Identity(name="wolfgang-aura", email=PRIVATE)
            )
            self.assertEqual(len(violations), 1)
            self.assertEqual(violations[0]["sha"], commits[0]["sha"])
            roles = {entry["role"] for entry in violations[0]["emails"]}
            self.assertEqual(roles, {"author", "committer"})

    def test_a_co_author_trailer_is_held_to_the_same_rule(self) -> None:
        """https://github.com/wolfgang-aura/Mailman/issues/57

        A trailer publishes an address without ever touching `git config`.
        """
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            _git(repository, "config", "user.email", PRIVATE)
            base = _git(repository, "rev-parse", "HEAD")
            (repository / "file.txt").write_text("changed\n", encoding="utf-8")
            _git(
                repository,
                "commit",
                "-am",
                f"change\n\nCo-authored-by: Someone Real <{PERSONAL}>\n",
            )

            commits = branch_commits(repository, base)
            violations = author_violations(
                commits, Identity(name="wolfgang-aura", email=PRIVATE)
            )

            self.assertEqual(len(violations), 1)
            self.assertEqual(
                violations[0]["emails"], [{"role": "co-author", "email": PERSONAL}]
            )

    def test_a_vendor_no_reply_co_author_trailer_passes(self) -> None:
        """The trailer this repository's own commits carry identifies nobody.

        Refusing it would block a clean run the way #80 did, over an address
        that is not a personal mailbox.
        """
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            _git(repository, "config", "user.email", PRIVATE)
            base = _git(repository, "rev-parse", "HEAD")
            (repository / "file.txt").write_text("changed\n", encoding="utf-8")
            _git(
                repository,
                "commit",
                "-am",
                "change\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n",
            )

            commits = branch_commits(repository, base)

            self.assertEqual(
                author_violations(
                    commits, Identity(name="wolfgang-aura", email=PRIVATE)
                ),
                [],
            )

    def test_a_noreply_co_author_trailer_passes(self) -> None:
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            _git(repository, "config", "user.email", PRIVATE)
            base = _git(repository, "rev-parse", "HEAD")
            (repository / "file.txt").write_text("changed\n", encoding="utf-8")
            _git(
                repository,
                "commit",
                "-am",
                f"change\n\nCo-authored-by: Claude <{OTHER_PRIVATE}>\n",
            )

            commits = branch_commits(repository, base)

            self.assertIn("Co-authored-by", commits[0]["message"])
            self.assertEqual(
                author_violations(
                    commits, Identity(name="wolfgang-aura", email=PRIVATE)
                ),
                [],
            )

    def test_a_noreply_address_passes_whoever_owns_it(self) -> None:
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            base = _git(repository, "rev-parse", "HEAD")
            apply_identity(
                repository, Identity(name="Someone Else", email=OTHER_PRIVATE)
            )
            (repository / "file.txt").write_text("changed\n", encoding="utf-8")
            _git(repository, "commit", "-am", "change")
            commits = branch_commits(repository, base)
            self.assertEqual(
                author_violations(
                    commits, Identity(name="wolfgang-aura", email=PRIVATE)
                ),
                [],
            )

    def test_without_a_configured_identity_a_personal_address_is_a_violation(
        self,
    ) -> None:
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            base = _git(repository, "rev-parse", "HEAD")
            (repository / "file.txt").write_text("changed\n", encoding="utf-8")
            _git(repository, "commit", "-am", "change")
            self.assertTrue(author_violations(branch_commits(repository, base), None))

    def test_an_unchanged_branch_has_nothing_to_check(self) -> None:
        with TemporaryDirectory() as name:
            repository = _repository(Path(name))
            base = _git(repository, "rev-parse", "HEAD")
            self.assertEqual(branch_commits(repository, base), [])


if __name__ == "__main__":
    unittest.main()
