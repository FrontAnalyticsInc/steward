"""Tests for the extraction step the wiki lost and got back.

Two properties carry the weight here, and both are about not trusting the model
rather than about prompting it well:

  * a "fact" copied out of the source is rejected, because storing prose
    verbatim is the exact defect this module was written to end; and
  * link syntax is written by us, never by the model, so a compromised or
    confused extraction cannot invent an edge to an arbitrary page.

The bounds are asserted separately from the prompt that also states them. The
prompt is how the model is asked; `validate` is what happens when it declines.
"""

from __future__ import annotations

import pytest

from app import fact_distill as F


class TestLinkTerms:
    def test_a_term_is_wrapped_where_it_appears(self):
        assert F.link_terms("Works at Kestrel.", ["Kestrel"]) == "Works at [[Kestrel]]."

    def test_a_term_that_does_not_appear_is_skipped(self):
        """The model names entities it believes are in the fact. When one is
        not, appending it anyway would assert a relationship the sentence does
        not make."""
        assert F.link_terms("Works at Kestrel.", ["Acme"]) == "Works at Kestrel."

    def test_the_longest_term_wins_so_a_name_is_not_split(self):
        linked = F.link_terms(
            "Runs Kestrel Underwriting.", ["Kestrel", "Kestrel Underwriting"]
        )
        assert linked == "Runs [[Kestrel Underwriting]]."

    def test_a_term_is_linked_once_even_when_repeated(self):
        linked = F.link_terms("Acme bought Acme.", ["Acme"])
        assert linked.count("[[") == 1

    def test_matching_is_case_insensitive_but_keeps_the_text_as_written(self):
        assert F.link_terms("works at kestrel.", ["Kestrel"]) == "works at [[kestrel]]."

    def test_no_terms_leaves_the_text_alone(self):
        assert F.link_terms("Plain sentence.", []) == "Plain sentence."


class TestModelOutputIsNotTrusted:
    def test_bracket_syntax_from_the_model_is_stripped(self):
        """Links are ours to write. A model that emits [[...]] directly could
        point a document at any page it liked."""
        assert "[" not in F._clean("Works at [[Anywhere I Choose]].")

    def test_whitespace_is_collapsed_so_a_wrapped_reply_is_one_line(self):
        assert F._clean("a\n  b\tc") == "a b c"


class TestCopyDetection:
    SOURCE = (
        "The newly printed compliance manual lands on the shift supervisor's desk "
        "with a heavy thud, a dense stack of laminated flowcharts and mandatory "
        "sign-offs birthed in the immediate aftermath of last month's breach."
    )

    def test_a_long_span_lifted_from_the_source_is_a_copy(self):
        assert F._is_copied(self.SOURCE[:160], self.SOURCE)

    def test_a_short_quote_is_not_a_copy(self):
        """A job title or product name legitimately appears verbatim."""
        assert not F._is_copied("Head of Data Engineering", self.SOURCE)

    def test_rewrapping_does_not_hide_a_copy(self):
        """Mail arrives hard-wrapped; a copy survives being rewrapped."""
        wrapped = self.SOURCE[:160].replace(" ", "\n", 4)
        assert F._is_copied(wrapped, self.SOURCE)

    def test_a_genuine_compression_is_not_a_copy(self):
        summary = (
            "Writes about how compliance processes introduced after an incident "
            "tend to entrench the behaviour they were meant to correct."
        )
        assert not F._is_copied(summary, self.SOURCE)


def _fact(text: str, confidence: float = 0.9, links=None) -> F.DistilledFact:
    return F.DistilledFact(fact=text, confidence=confidence, links=links or [])


class TestValidate:
    def test_a_low_confidence_fact_is_dropped(self):
        result = F.validate(F.Distillation(facts=[_fact("Guessing wildly here.", 0.2)]), "")
        assert result.facts == []

    def test_a_copied_fact_is_dropped(self):
        source = "x " * 200
        copied = ("x " * 100).strip()
        assert F.validate(F.Distillation(facts=[_fact(copied)]), source).facts == []

    def test_an_over_long_fact_is_truncated_not_dropped(self):
        long = "Runs data engineering " * 40
        result = F.validate(F.Distillation(facts=[_fact(long)]), "")
        assert len(result.facts) == 1
        assert len(result.facts[0].fact) <= F.MAX_FACT_CHARS + 1
        assert result.facts[0].fact.endswith("…")

    def test_duplicate_facts_collapse(self):
        pair = [_fact("Works at Acme."), _fact("works at acme.")]
        assert len(F.validate(F.Distillation(facts=pair), "").facts) == 1

    def test_no_more_than_the_ceiling_is_kept(self):
        many = [_fact(f"Distinct durable fact number {n}.") for n in range(50)]
        assert len(F.validate(F.Distillation(facts=many), "").facts) == F.MAX_FACTS

    def test_an_empty_fact_is_dropped(self):
        assert F.validate(F.Distillation(facts=[_fact("   ")]), "").facts == []


class TestParse:
    def test_plain_json_parses(self):
        parsed = F._parse('{"facts": [{"fact": "Works at Acme.", "confidence": 0.9}]}')
        assert parsed.facts[0].fact == "Works at Acme."

    def test_a_fenced_reply_parses(self):
        """Providers wrap JSON in a code fence routinely, schema or no schema."""
        parsed = F._parse('```json\n{"facts": [], "organization": "Acme"}\n```')
        assert parsed.organization == "Acme"

    def test_prose_around_the_object_is_tolerated(self):
        parsed = F._parse('Sure! {"facts": [], "organization": "Acme"} Hope that helps.')
        assert parsed.organization == "Acme"

    def test_a_reply_that_is_not_json_is_none(self):
        assert F._parse("I could not do that.") is None

    def test_one_malformed_fact_does_not_lose_the_others(self):
        parsed = F._parse(
            '{"facts": [{"fact": "Good.", "confidence": 0.9}, {"confidence": "banana"}]}'
        )
        assert [entry.fact for entry in parsed.facts] == ["Good."]

    def test_a_reply_with_no_usable_facts_at_all_is_none(self):
        assert F._parse('{"facts": [{"confidence": "banana"}], "x": 1}') is None


class TestDistill:
    def test_no_model_configured_returns_none_rather_than_raising(self, monkeypatch):
        """None means "no distillation happened", which sends the caller to the
        deterministic path. Distinct from an empty Distillation, which means the
        model read the material and found nothing durable."""
        monkeypatch.setattr(F.config, "model_available", lambda: False)
        assert F.distill("a@x.com", "some observations") is None

    def test_empty_observations_short_circuit(self):
        assert F.distill("a@x.com", "   ") is None

    def test_a_failing_model_call_returns_none_rather_than_raising(self, monkeypatch):
        """A refresh that cannot reach a model must not fail the run that asked
        for context."""
        monkeypatch.setattr(F.config, "model_available", lambda: True)
        monkeypatch.setattr(F.config, "model_string", lambda alias=None: "x/y")

        import litellm

        def boom(**kwargs):
            raise RuntimeError("upstream is down")

        monkeypatch.setattr(litellm, "completion", boom)
        assert F.distill("a@x.com", "observations") is None


class TestToLines:
    def test_links_are_applied_at_render_time(self):
        distillation = F.Distillation(
            facts=[_fact("Runs analytics at Kestrel.", links=["Kestrel"])]
        )
        assert F.to_lines(distillation) == ["Runs analytics at [[Kestrel]]."]

    def test_one_fact_can_carry_several_links(self):
        """The reason a distilled fact is not a triplet: a triplet has one
        object, and this line legitimately connects an employer and a topic."""
        distillation = F.Distillation(
            facts=[
                _fact(
                    "Leads platform work at Kestrel on supply chain simulation.",
                    links=["Kestrel", "supply chain simulation"],
                )
            ]
        )
        assert F.to_lines(distillation) == [
            "Leads platform work at [[Kestrel]] on [[supply chain simulation]]."
        ]
