"""Every figure in the assistant's answer, looked for in what the tools replied (recheck, 25/09/2026).

The system prompt says every number comes from a tool reply. A prompt is a request, not a check:
the retail answer of 25/09/2026 got every revenue, cost and margin right and still said 3,470 twice
where the table holds 3,471 and 3,473 -- the figure came from a caveat the person typed into the
contract, which every result prints, and the model carried it as a finding. This module is the
check. It does no arithmetic of its own on the data; it reads two texts.

A figure in the answer is:

- **found** when a tool reply this turn holds it, as written or rounded to the digits the answer
  shows (12.9932 is "13.0%", 168,881,809.17 is "168.9 million" or "16.89 crore"; a fraction 0.1299
  is "13.0%");
- **worked out** when it is the sum, difference, ratio or percentage change of two figures a tool
  replied -- the model's own arithmetic, which is allowed and is said;
- **declared** when it is found only in a caveat the person wrote into the contract, printed with
  state.DECLARED: their statement, not the engine's measurement;
- **unsupported** otherwise.

Not looked at, on purpose: whole numbers 0 to 10 written without a unit ("3 regions", "top 5"),
years 1900-2100, dates and periods, and figures inside identifiers (E0029, L0000123). Each would
be found by coincidence or flagged for nothing. A figure the question itself holds is found.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from backend.engine.state import DECLARED

_SCALE = {"thousand": 1e3, "k": 1e3, "lakh": 1e5, "lakhs": 1e5, "million": 1e6, "mn": 1e6,
          "m": 1e6, "crore": 1e7, "crores": 1e7, "cr": 1e7, "billion": 1e9, "bn": 1e9, "b": 1e9}
_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-−+])?(?:[₹$€£¥]\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"(?P<pct>\s?%)?"
    r"(?:\s?(?P<scale>thousand|lakhs?|million|mn|crores?|cr|billion|bn|[kKmMbB])(?![\w]))?"
    r"(?![\w.]\d|[A-Za-z0-9])")
# Removed before numbers are read: dates, periods, clock times, and identifiers holding digits.
_NOT_FIGURES = re.compile(
    r"\b\d{4}-\d{2}(?:-\d{2})?(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b"   # 2025-11, 2025-11-03 08:04
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"                               # 01/12/2024
    r"|\b\d{4}-?Q[1-4]\b|\bQ[1-4][ -]?\d{4}\b"                     # 2025-Q4, Q4 2025
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"                               # 08:04
    r"|\b[A-Za-z_]+\d[\w]*\b|\b\d+(?:st|nd|rd|th)\b")                 # E0029, C003, 3rd
# A number followed by x/× is a multiple: "~20× list price". Kept, the sign read off.
_TIMES = re.compile(r"(\d)\s?[x×]\b")


@dataclass(frozen=True)
class Figure:
    text: str       # as the answer wrote it
    value: float    # scaled, sign dropped
    places: int     # decimal places shown
    scale: float
    percent: bool


@dataclass(frozen=True)
class Verification:
    """The check of one answer. `unsupported` and `declared` hold the figures as written."""

    checked: int = 0
    found: int = 0
    worked_out: list[str] = field(default_factory=list)
    declared: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.declared or self.unsupported)

    def summary(self) -> str:
        """One line for the person, under the answer."""
        if not self.checked:
            return "No figures in this answer to check."
        parts = [f"{self.found + len(self.worked_out)} of {self.checked} figure(s) match the "
                 f"tool replies"]
        if self.worked_out:
            parts.append(f"{len(self.worked_out)} of them worked out from two replied figures "
                         f"({', '.join(self.worked_out[:6])})")
        out = "; ".join(parts) + "."
        if self.declared:
            out += (f" Only in the contract's caveats, which the person wrote and the engine did "
                    f"not measure: {', '.join(self.declared)}.")
        if self.unsupported:
            out += (f" Not in any tool reply this turn: {', '.join(self.unsupported)} -- treat "
                    f"as unverified.")
        return out

    def correction(self) -> str:
        """What the model is told, once, when the answer is not clean."""
        lines = ["[system] Check before your answer is shown. Rewrite it with these corrections, "
                 "and change nothing else. No tool can be called."]
        if self.unsupported:
            lines.append(f"- Not in any tool reply: {', '.join(self.unsupported)}. Replace each "
                         f"with the figure a tool reply gives, or remove it.")
        if self.declared:
            lines.append(f"- Only in caveats the person declared in the contract, not measured: "
                         f"{', '.join(self.declared)}. Say 'the contract notes ...' for each, or "
                         f"give the measured figure where a tool reply has one.")
        return "\n".join(lines)


def figures(text: str) -> list[Figure]:
    cleaned = _TIMES.sub(r"\1 ", _NOT_FIGURES.sub(" ", text.replace(" ", " ")))
    out = []
    for m in _NUMBER.finditer(cleaned):
        num = m.group("num")
        digits = num.replace(",", "")
        value = float(digits)
        places = len(digits.split(".")[1]) if "." in digits else 0
        scale_word = (m.group("scale") or "").lower()
        scale = _SCALE.get(scale_word, 1.0)
        # A single-letter scale only counts where it is glued on ("₹16.9M", "3.2k"); "5 m" is
        # likelier metres or a stray letter, and "b" alone is a word boundary accident.
        if len(scale_word) == 1 and m.group(0).rstrip()[-2:-1].isspace():
            scale = 1.0
        percent = bool(m.group("pct"))
        out.append(Figure(m.group(0).strip(), value * scale, places, scale, percent))
    return out


def _worth_checking(f: Figure) -> bool:
    if f.percent or f.places or f.scale != 1.0:
        return True
    if f.value <= 10:
        return False
    if 1900 <= f.value <= 2100 and "," not in f.text:
        return False
    return True


class _Evidence:
    """The figures of the tool replies, sorted, with and without the declared caveats."""

    def __init__(self, measured: list[str], context: list[str]) -> None:
        values: list[float] = []
        declared: list[float] = []
        for text in measured:
            for line in text.splitlines():
                target = declared if DECLARED in line else values
                target.extend(abs(f.value) for f in figures(line))
        for text in context:
            values.extend(abs(f.value) for f in figures(text))
        self.values = sorted(set(values))
        self.declared = sorted(set(declared))

    @staticmethod
    def _near(pool: list[float], target: float, tol: float) -> bool:
        i = bisect.bisect_left(pool, target - tol)
        return i < len(pool) and pool[i] <= target + tol

    def holds(self, f: Figure, pool: list[float]) -> bool:
        tol = 0.5 * 10 ** -f.places * f.scale + 1e-9
        if self._near(pool, f.value, tol):
            return True
        # A percentage replied as a fraction (0.1299 -> 13.0%), or a fraction as a percentage.
        return f.percent and self._near(pool, f.value / 100, tol / 100)

    def derived(self, f: Figure) -> bool:
        """Sum, difference, ratio or percentage change of two measured figures. Bounded: the
        pools are a turn's replies, a few hundred numbers, and each test is a bisect."""
        tol = 0.5 * 10 ** -f.places * f.scale + 1e-9
        pool = self.values
        if len(pool) > 3000 or (f.value < 100 and not f.percent and not f.places):
            return False  # too many pairs to be evidence of anything, or too coarse to tell
        for a in pool:
            if a == 0:
                continue
            if (self._near(pool, a + f.value, tol) or self._near(pool, abs(a - f.value), tol)):
                return True
            if f.percent:
                # (b - a) / a and b / a, as percentages
                if (self._near(pool, a * (1 + f.value / 100), a * tol / 100)
                        or self._near(pool, a * (1 - f.value / 100), a * tol / 100)
                        or self._near(pool, a * f.value / 100, a * tol / 100)):
                    return True
            elif self._near(pool, a * f.value, a * tol):
                return True
        return False


def verify(answer: str, tool_replies: list[str], context: list[str] = ()) -> Verification:
    """Check `answer` against the replies the model read. `context` is the question and earlier
    messages: a figure the person gave is theirs to repeat."""
    ev = _Evidence(tool_replies, list(context))
    checked = found = 0
    worked, declared, unsupported = [], [], []
    for f in figures(answer):
        if not _worth_checking(f):
            continue
        checked += 1
        if ev.holds(f, ev.values):
            found += 1
        elif ev.holds(f, ev.declared):
            declared.append(f.text)
        elif ev.derived(f):
            worked.append(f.text)
        else:
            unsupported.append(f.text)
    return Verification(checked, found, list(dict.fromkeys(worked)),
                        list(dict.fromkeys(declared)), list(dict.fromkeys(unsupported)))


__all__ = ["Figure", "Verification", "figures", "verify"]
