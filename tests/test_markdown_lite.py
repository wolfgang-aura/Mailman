from __future__ import annotations

import unittest

from mailman.markdown_lite import render_markdown


class MarkdownTests(unittest.TestCase):
    def test_headings_start_below_the_page_headings(self) -> None:
        # A report's own `#` must not compete with the page's section headings.
        self.assertEqual(render_markdown("# Summary"), "<h3>Summary</h3>")
        self.assertEqual(render_markdown("## Summary"), "<h4>Summary</h4>")

    def test_inline_spans(self) -> None:
        rendered = render_markdown("**bold** and *thin* and `code`")

        self.assertIn("<strong>bold</strong>", rendered)
        self.assertIn("<em>thin</em>", rendered)
        self.assertIn("<code>code</code>", rendered)

    def test_an_asterisk_inside_a_word_is_left_alone(self) -> None:
        rendered = render_markdown("the glob is file*.py and stays")

        self.assertNotIn("<em>", rendered)
        self.assertIn("file*.py", rendered)

    def test_bullets_and_numbers_become_the_matching_list(self) -> None:
        bullets = render_markdown("- one\n- two")
        numbers = render_markdown("1. first\n2. second")

        self.assertEqual(bullets, "<ul><li>one</li><li>two</li></ul>")
        self.assertEqual(numbers, "<ol><li>first</li><li>second</li></ol>")

    def test_a_wrapped_bullet_stays_in_its_item(self) -> None:
        rendered = render_markdown("- a finding that runs\n  onto a second line\n- next")

        self.assertIn("<li>a finding that runs onto a second line</li>", rendered)
        self.assertIn("<li>next</li>", rendered)

    def test_a_fenced_block_keeps_its_line_breaks(self) -> None:
        rendered = render_markdown("```\nfirst\nsecond\n```")

        self.assertIn("<pre class=\"block\"><code>first\nsecond</code></pre>", rendered)

    def test_a_table_becomes_a_table(self) -> None:
        rendered = render_markdown("| a | b |\n| --- | --- |\n| 1 | 2 |")

        self.assertIn("<th>a</th>", rendered)
        self.assertIn("<td>2</td>", rendered)

    def test_links_are_linked_whether_or_not_they_are_written_as_links(self) -> None:
        rendered = render_markdown(
            "see [the issue](https://example.invalid/1) and https://example.invalid/2"
        )

        self.assertIn('<a href="https://example.invalid/1">the issue</a>', rendered)
        self.assertIn(
            '<a href="https://example.invalid/2">https://example.invalid/2</a>', rendered
        )

    def test_markup_in_a_report_is_escaped_before_anything_else(self) -> None:
        rendered = render_markdown("<script>alert('x')</script> and `<b>literal</b>`")

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("<code>&lt;b&gt;literal&lt;/b&gt;</code>", rendered)

    def test_a_blockquote_and_a_rule(self) -> None:
        self.assertIn("<blockquote>quoted</blockquote>", render_markdown("> quoted"))
        self.assertIn("<hr>", render_markdown("---"))

    def test_plain_prose_is_one_paragraph_per_blank_line(self) -> None:
        rendered = render_markdown("first line\nsame paragraph\n\nsecond paragraph")

        self.assertEqual(
            rendered, "<p>first line same paragraph</p><p>second paragraph</p>"
        )


if __name__ == "__main__":
    unittest.main()
