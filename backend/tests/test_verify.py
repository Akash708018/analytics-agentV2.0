"""webapp/verify.py and the agent's one correction round (recheck, 25/09/2026).

The retail answer got revenue, cost and margin right and still said 3,470 twice: a caveat the
person typed into the contract, printed by every result, carried as a finding. Run:

    uv run pytest tests/test_verify.py -q
"""

from __future__ import annotations

from backend.engine.state import DECLARED
from backend.engine.webapp import agent
from backend.engine.webapp.llm import ProviderError, Reply
from backend.engine.webapp.verify import figures, verify

REPLY = ("| region | line_revenue | line_cost | margin_pct |\n"
         "| South | 168881809.17 | 146938638.13 | 12.9932 |\n"
         "| West | 168381684.49 | 147468661.80 | 12.4200 |\n"
         "120 row(s) are exact duplicates of another row.\n"
         f"{DECLARED}customer_age has 'unknown' in 3,470 rows")


def test_rounded_and_scaled_figures_are_found():
    v = verify("South: ₹168.9 million (13.0%), West 12.4%; 16.84 crore; 120 duplicates.", [REPLY])
    assert v.clean and v.checked == 5 and v.found == 5


def test_a_figure_only_a_declared_caveat_holds_is_named():
    v = verify("Data quality: 3,470 rows have an unknown age.", [REPLY])
    assert v.declared == ["3,470"] and not v.clean
    assert "did not measure: 3,470" in v.summary()


def test_the_measured_figure_wins_over_the_declared_one():
    v = verify("3,471 rows have an unknown age.", [REPLY, "3,471 did not parse as BIGINT"])
    assert v.clean


def test_an_invented_figure_is_unsupported():
    v = verify("Margins average 14.7% across regions.", [REPLY])
    assert v.unsupported == ["14.7%"]


def test_arithmetic_on_two_replied_figures_is_worked_out_not_unsupported():
    # 168,881,809.17 - 168,381,684.49 = 500,124.68
    v = verify("South leads West by 500,124.68.", [REPLY])
    assert v.worked_out == ["500,124.68"] and v.clean


def test_small_counts_years_dates_and_ids_are_not_checked():
    v = verify("Top 5 of 4 regions in 2025; E0029 on 2025-11-03 and Q4 2025, 3rd place.", [REPLY])
    assert v.checked == 0


def test_a_figure_from_the_question_is_the_persons():
    assert verify("You asked about 37,500 customers.", [REPLY], ["how many of 37,500?"]).clean


def test_figures_keep_what_was_written():
    assert [f.text for f in figures("₹3.2k, 12.5 %, 1,68,881")] == ["₹3.2k", "12.5 %", "1,68,881"]


class Session:
    def __init__(self, replies, fail=False):
        self.replies, self.sent, self.fail = list(replies), [], fail

    def step(self, final=False):
        if self.fail:
            raise ProviderError("gemini", "quota", retryable=True)
        return Reply(text=self.replies.pop(0))

    def add_user(self, text):
        self.sent.append(text)


def test_an_unclean_answer_is_sent_back_once_and_the_rewrite_shown():
    s = Session(["The contract notes about 3,471 unknown ages; South margin 13.0%."])
    text, v = agent._checked(s, "3,470 unknown ages; South margin 13.0%.", [REPLY + "\n3,471"], [])
    assert len(s.sent) == 1 and "3,470" in s.sent[0] and "contract notes" in s.sent[0]
    assert text.startswith("The contract notes") and v.clean


def test_a_clean_answer_costs_no_second_call():
    s = Session([])
    text, v = agent._checked(s, "South margin 13.0%.", [REPLY], [])
    assert s.sent == [] and v.clean


def test_a_failed_correction_keeps_the_answer_and_its_flags():
    s = Session([], fail=True)
    text, v = agent._checked(s, "Margins average 14.7%.", [REPLY], [])
    assert text == "Margins average 14.7%." and v.unsupported == ["14.7%"]


def test_a_rewrite_that_is_worse_is_not_shown():
    s = Session(["Margins average 14.7% and 15.2%, 3,470 unknown."])
    text, v = agent._checked(s, "Margins average 14.7%.", [REPLY], [])
    assert text == "Margins average 14.7%." and v.unsupported == ["14.7%"]


def test_a_declared_figure_is_still_declared_when_its_block_was_sent_once():
    """Step 2: a repeated caveat block reaches the model as one line; the check reads the block."""
    from backend.engine.webapp.agent import Gathered
    from backend.engine.webapp.llm import Call
    got = Gathered()
    block = f"  - {DECLARED}customer_state blank in 3,470 rows\n"
    got.add(Call("1", "x", {}), f"first\n{block}| North | 1,234.50 |")
    shown = got.add(Call("2", "x", {}), f"second\n{block}| South | 987.25 |")
    assert DECLARED not in shown
    checked = verify("South had 987.25; 3,470 rows lack a state.", got.seen[1:])
    assert checked.declared == ["3,470"] and not checked.unsupported
