from __future__ import annotations

import unittest

from mailman.redaction import changed_lines, diff_reveals_credential, redact


class RedactionTests(unittest.TestCase):
    def test_redacts_supported_secret_shapes(self) -> None:
        fake_github_token = "github_pat_" + "a" * 36
        fake_anthropic_key = "sk-ant-" + "b" * 36
        text = "\n".join(
            [
                "Authorization: Bearer token-value",
                "api_key=visible-value",
                fake_github_token,
                fake_anthropic_key,
            ]
        )
        result = redact(text)

        self.assertNotIn("token-value", result)
        self.assertNotIn("visible-value", result)
        self.assertNotIn(fake_github_token, result)
        self.assertNotIn(fake_anthropic_key, result)
        self.assertIn("[REDACTED", result)


class DiffScopeTests(unittest.TestCase):
    diff = "\n".join(
        [
            "diff --git a/client.py b/client.py",
            "index 1111111..2222222 100644",
            "--- a/client.py",
            "+++ b/client.py",
            "@@ -1,4 +1,4 @@",
            " def call(api_key):",
            "     return request(api_key=api_key)",
            "-    return 0",
            "+    return 1",
            "",
        ]
    )

    def test_changed_lines_excludes_context_and_file_headers(self) -> None:
        self.assertEqual(changed_lines(self.diff), ["    return 0", "    return 1"])

    def test_a_context_line_that_looks_like_a_credential_is_not_a_match(self) -> None:
        # The whole-diff scan this replaced refused exactly this shape.
        self.assertNotEqual(redact(self.diff), self.diff)
        self.assertFalse(diff_reveals_credential(self.diff))

    def test_an_added_line_that_looks_like_a_credential_is_a_match(self) -> None:
        diff = self.diff.replace("+    return 1", "+    api_key = 'literal-secret'")
        self.assertTrue(diff_reveals_credential(diff))

    def test_a_removed_line_that_looks_like_a_credential_is_a_match(self) -> None:
        diff = self.diff.replace("-    return 0", "-    api_key = 'literal-secret'")
        self.assertTrue(diff_reveals_credential(diff))


if __name__ == "__main__":
    unittest.main()
