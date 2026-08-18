"""Tests for the outbound-mail markdown renderer.

The first class is the reason this module is hand-written: the text being
rendered is a model's prose about calendar invites written by other people, so
the question that matters is not "does it render bold" but "can input become a
tag". It cannot, because escaping happens before any tag is introduced.
"""

from __future__ import annotations

from app import markdown_email as M


class TestNothingInputBecomesMarkup:
    def test_html_in_the_text_is_shown_not_executed(self):
        out = M.to_html_fragment("Meeting with <script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_an_image_onerror_payload_is_inert(self):
        """The shape an attacker actually uses in an invite title.

        The words survive as visible text — that is correct, it is what the
        invite said. What must not survive is the tag.
        """
        out = M.to_html_fragment('<img src=x onerror="steal()">')
        assert "<img" not in out
        assert "&lt;img" in out

    def test_ampersands_and_quotes_survive_as_text(self):
        out = M.to_html_fragment("Q&A about \"scope\" & timing")
        assert "&amp;" in out
        assert "Q&A" not in out

    def test_a_full_document_is_still_escaped(self):
        out = M.to_html_document("<b>hi</b>", title="<evil>")
        assert "<b>hi</b>" not in out
        assert "&lt;b&gt;hi&lt;/b&gt;" in out
        assert "<title>&lt;evil&gt;</title>" in out


class TestBlocks:
    def test_headings_become_heading_tags(self):
        out = M.to_html_fragment("### Office Hours (Eric)")
        assert "<h3" in out and "Office Hours (Eric)" in out

    def test_bullets_become_a_list(self):
        out = M.to_html_fragment("* one\n* two")
        assert out.count("<li") == 2
        assert "<ul" in out and "</ul>" in out

    def test_an_indented_bullet_nests_once(self):
        out = M.to_html_fragment("* outer\n    * inner")
        assert out.count("<ul") == 2
        assert out.count("</ul>") == 2

    def test_bold_renders_inside_a_bullet(self):
        out = M.to_html_fragment("*   **Time:** 9:30 AM")
        assert "<strong>Time:</strong>" in out

    def test_paragraphs_are_separated_by_blank_lines(self):
        out = M.to_html_fragment("first para\n\nsecond para")
        assert out.count("<p") == 2

    def test_wrapped_lines_join_into_one_paragraph(self):
        out = M.to_html_fragment("a line\nthat wrapped")
        assert out.count("<p") == 1
        assert "a line that wrapped" in out

    def test_every_line_survives_somewhere(self):
        """The fallback is plain text, never a dropped line."""
        text = "### H\n\nintro\n\n* a\n* b\n\ntrailing"
        out = M.to_html_fragment(text)
        for token in ("H", "intro", "a", "b", "trailing"):
            assert token in out

    def test_empty_input_is_empty_output_not_a_crash(self):
        assert M.to_html_fragment("") == ""
        assert "<body" in M.to_html_document("")


class TestOrderedLists:
    """Without <ol> handling these fell through to the paragraph branch, where
    consecutive lines are joined with a space — a three-step agenda arrived as
    one run-on line."""

    def test_a_numbered_list_becomes_an_ordered_list(self):
        out = M.to_html_fragment("1. Check in\n2. Blockers\n3. Next steps")
        assert "<ol" in out and "</ol>" in out
        assert out.count("<li") == 3
        assert "<ul" not in out

    def test_the_numbers_are_not_repeated_as_text(self):
        """The marker comes from <ol>; leaving "1." in would double it."""
        out = M.to_html_fragment("1. Check in")
        assert ">Check in</li>" in out
        assert "1. Check in" not in out

    def test_close_paren_style_also_counts(self):
        out = M.to_html_fragment("1) Check in\n2) Blockers")
        assert out.count("<li") == 2 and "<ol" in out

    def test_numbered_steps_no_longer_collapse_into_one_paragraph(self):
        out = M.to_html_fragment("1. Check in\n2. Blockers")
        assert "<p" not in out

    def test_an_agenda_nested_under_a_bullet_closes_both_tags(self):
        out = M.to_html_fragment("* Suggested Agenda:\n    1. Check in\n    2. Blockers")
        assert "<ul" in out and "<ol" in out
        # </ol> must come before </ul>, or the nesting is malformed.
        assert out.index("</ol>") < out.index("</ul>")

    def test_switching_from_bullets_to_numbers_closes_the_first_list(self):
        out = M.to_html_fragment("* one\n* two\n1. first\n2. second")
        assert out.count("<ul") == 1 and out.count("<ol") == 1
        assert out.count("</ul>") == 1 and out.count("</ol>") == 1

    def test_a_year_does_not_open_a_list(self):
        """'2026. ' at the start of a line is prose, not an agenda step."""
        out = M.to_html_fragment("2026. A big year")
        assert "<ol" not in out

    def test_bullets_are_unaffected(self):
        out = M.to_html_fragment("* one\n* two")
        assert "<ul" in out and "<ol" not in out


class TestHeadingSpacing:
    def test_headings_carry_a_double_paragraph_gap_above(self):
        """Each meeting should start as a visibly separate block."""
        out = M.to_html_fragment("### Office Hours")
        assert "margin:2.4em" in out

    def test_the_gap_is_margin_not_padding(self):
        out = M.to_html_fragment("### Office Hours")
        assert "padding-top" not in out


class TestRealBriefing:
    BODY = (
        "Good morning Alton,\n\n"
        "You have two office hours scheduled today.\n\n"
        "### Office Hours (Eric Kebschull)\n"
        "*   **Time:** 9:30 AM - 10:00 AM\n"
        "*   **Suggested Agenda:**\n"
        "    *   Introductions\n"
        "    *   Q&A\n"
    )

    def test_the_shape_the_model_actually_emits(self):
        out = M.to_html_document(self.BODY, title="Briefing")
        assert "<h3" in out
        assert "<strong>Time:</strong>" in out
        assert out.count("<ul") == 2  # outer list plus the nested agenda
        assert "&amp;" in out  # the Q&A ampersand, escaped
        assert out.startswith("<!DOCTYPE html>")
