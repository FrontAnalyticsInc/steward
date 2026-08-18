"""Tests for what a model call cost, and the ceiling that stops a runaway.

Two guarantees are load-bearing here and neither is obvious from reading the
code:

  * a row we could not price must not read as free. The store sums `metered`
    rows only, so an unpriced call written as 0.0 would understate spend — and
    the cap is computed from exactly that sum, so it would also raise the real
    ceiling without anyone changing a setting.
  * bookkeeping must never fail a run. The work is already done by the time we
    price it; losing the receipt is bad, losing the outcome is worse.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import cost_ledger


def response(prompt_tokens=100, completion_tokens=50, **extra):
    """A completion shaped like LiteLLM's, which is what the callback receives."""
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            **extra,
        )
    )


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = cost_ledger.USAGE_DIR
        cost_ledger.USAGE_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        cost_ledger.USAGE_DIR = self._previous
        self._tmp.cleanup()

    def rows(self) -> list[dict]:
        written = list(Path(self._tmp.name).glob("usage-*.jsonl"))
        if not written:
            return []
        self.assertEqual(len(written), 1, "one file per UTC day")
        return [
            json.loads(line)
            for line in written[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class TestRecord(LedgerTestCase):
    def test_a_precomputed_cost_is_taken_as_metered(self):
        # LiteLLM has already priced the call by the time the success callback
        # fires. Pricing it a second time would be the same function, twice.
        row = cost_ledger.record(
            response=response(),
            model="anthropic/claude-opus-5",
            precomputed_cost=0.00055,
        )
        self.assertEqual(row["actual_cost_usd"], 0.00055)
        self.assertEqual(row["cost_status"], "metered")
        self.assertEqual(row["cost_source"], "litellm.response_cost")

    def test_estimated_cost_stays_null(self):
        # The store's whole reason for two columns is that one is measured and
        # one is guessed. Writing this figure into both erases the distinction.
        row = cost_ledger.record(
            response=response(), model="anthropic/claude-opus-5", precomputed_cost=0.001
        )
        self.assertIsNone(row["estimated_cost_usd"])

    def test_the_vendor_is_read_off_the_model_address(self):
        row = cost_ledger.record(
            response=response(), model="anthropic/claude-opus-5", precomputed_cost=0.0
        )
        self.assertEqual(row["billing_provider"], "anthropic")

    def test_a_bare_model_name_records_no_vendor_rather_than_a_wrong_one(self):
        row = cost_ledger.record(
            response=response(), model="claude-opus-5", precomputed_cost=0.0
        )
        self.assertIsNone(row["billing_provider"])

    def test_a_zero_cost_is_metered_not_unpriced(self):
        # A model that genuinely costs nothing and a model nobody could price
        # are different facts, and only one of them should be summable.
        row = cost_ledger.record(
            response=response(), model="anthropic/claude-opus-5", precomputed_cost=0.0
        )
        self.assertEqual(row["cost_status"], "metered")
        self.assertEqual(row["actual_cost_usd"], 0.0)

    def test_an_unpriceable_call_is_null_not_zero(self):
        row = cost_ledger.record(response=object(), model="not-a-real/model")
        self.assertIsNone(row["actual_cost_usd"])
        self.assertEqual(row["cost_status"], "unpriced")

    def test_tokens_are_read_from_either_naming_convention(self):
        row = cost_ledger.record(
            response=response(
                cache_read_input_tokens=17, cache_creation_input_tokens=3
            ),
            model="anthropic/claude-opus-5",
            precomputed_cost=0.0,
        )
        self.assertEqual(row["input_tokens"], 100)
        self.assertEqual(row["output_tokens"], 50)
        self.assertEqual(row["cache_read_tokens"], 17)
        self.assertEqual(row["cache_write_tokens"], 3)

    def test_a_dict_response_is_read_the_same_as_an_object(self):
        # A replayed or cached response arrives as a plain dict.
        row = cost_ledger.record(
            response={"usage": {"prompt_tokens": 7, "completion_tokens": 2}},
            model="anthropic/claude-opus-5",
            precomputed_cost=0.0,
        )
        self.assertEqual(row["input_tokens"], 7)
        self.assertEqual(row["output_tokens"], 2)

    def test_a_response_with_no_usage_records_the_call_anyway(self):
        row = cost_ledger.record(
            response=object(), model="anthropic/claude-opus-5", precomputed_cost=0.0
        )
        self.assertEqual(row["api_call_count"], 1)
        self.assertIsNone(row["input_tokens"])

    def test_an_unwritable_directory_does_not_fail_the_run(self):
        cost_ledger.USAGE_DIR = Path("/proc/nonexistent/usage")
        row = cost_ledger.record(
            response=response(), model="anthropic/claude-opus-5", precomputed_cost=0.5
        )
        self.assertEqual(row["actual_cost_usd"], 0.5)  # returned, just not stored

    def test_every_call_appends_one_line(self):
        for _ in range(3):
            cost_ledger.record(
                response=response(),
                model="anthropic/claude-opus-5",
                precomputed_cost=0.001,
            )
        self.assertEqual(len(self.rows()), 3)


class TestSpendToday(LedgerTestCase):
    def test_no_file_is_no_spend(self):
        self.assertEqual(cost_ledger.spend_today(), 0.0)

    def test_metered_rows_sum(self):
        for cost in (0.001, 0.002, 0.003):
            cost_ledger.record(
                response=response(),
                model="anthropic/claude-opus-5",
                precomputed_cost=cost,
            )
        self.assertAlmostEqual(cost_ledger.spend_today(), 0.006)

    def test_unpriced_rows_contribute_nothing(self):
        cost_ledger.record(
            response=response(), model="anthropic/claude-opus-5", precomputed_cost=0.004
        )
        cost_ledger.record(response=object(), model="not-a-real/model")
        self.assertAlmostEqual(cost_ledger.spend_today(), 0.004)

    def test_a_torn_final_line_does_not_hide_the_rest_of_the_day(self):
        # A crash mid-append leaves a partial line. Refusing to report any spend
        # because of it would disable the cap at exactly the wrong moment.
        cost_ledger.record(
            response=response(), model="anthropic/claude-opus-5", precomputed_cost=0.01
        )
        path = next(Path(self._tmp.name).glob("usage-*.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"actual_cost_usd": 0.9')
        self.assertAlmostEqual(cost_ledger.spend_today(), 0.01)


class TestCap(LedgerTestCase):
    def spend(self, amount: float) -> None:
        cost_ledger.record(
            response=response(),
            model="anthropic/claude-opus-5",
            precomputed_cost=amount,
        )

    def test_under_the_cap_reports_the_position(self):
        self.spend(0.25)
        position = cost_ledger.check_cap(1.0)
        self.assertTrue(position["enabled"])
        self.assertAlmostEqual(position["spent_today_usd"], 0.25)
        self.assertAlmostEqual(position["remaining_usd"], 0.75)

    def test_at_the_cap_refuses(self):
        # `>=`, not `>`: reaching the cap exactly must stop the next dispatch,
        # or a cap of $10 permits one more unbounded run at $10.00.
        self.spend(1.0)
        with self.assertRaises(cost_ledger.DailyCapExceeded):
            cost_ledger.check_cap(1.0)

    def test_the_refusal_says_what_to_do_about_it(self):
        self.spend(2.0)
        with self.assertRaises(cost_ledger.DailyCapExceeded) as caught:
            cost_ledger.check_cap(1.0)
        message = str(caught.exception)
        self.assertIn("WORKFLOWS_DAILY_COST_CAP_USD", message)
        self.assertEqual(caught.exception.cap, 1.0)
        self.assertAlmostEqual(caught.exception.spent, 2.0)

    def test_a_cap_of_zero_disables_the_check(self):
        self.spend(1000.0)
        position = cost_ledger.check_cap(0)
        self.assertFalse(position["enabled"])
        self.assertIsNone(position["remaining_usd"])

    def test_a_day_of_unpriced_calls_cannot_trip_the_cap(self):
        # Honest rather than safe, and deliberately so: we do not know what those
        # calls cost, so we cannot claim they exceeded a dollar figure. The
        # account-level limit at the vendor is what covers this case.
        for _ in range(50):
            cost_ledger.record(response=object(), model="not-a-real/model")
        cost_ledger.check_cap(0.01)  # must not raise


if __name__ == "__main__":
    unittest.main()
