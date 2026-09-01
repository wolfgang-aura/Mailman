from __future__ import annotations

import unittest

from mailman.redaction import redact


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


if __name__ == "__main__":
    unittest.main()
