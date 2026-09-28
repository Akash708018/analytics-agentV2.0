"""Ground facts for Phase 11 (charts), measured on the installed matplotlib.

Nothing here is recalled. Every value this file prints was produced by a run recorded in
docs/decisions.md under "Phase 11, Step 1". Assertions are structural where the behaviour is
certain and printed where it is version-dependent, the same split tests/test_stats_facts.py
uses, and for the same reason: a bound that passes is not a measurement.

No numpy. matplotlib depends on it and this engine does not import it -- an analysis returns
Output(headers, rows, ...) whose rows are lists of scalars read with fetchall, so every input
below is a plain Python list. Testing matplotlib on numpy arrays would measure a calling
convention the chart layer will never use.

The backend is set to Agg before pyplot is imported. The guide names this as the Phase 11 trap:
a GUI backend can hang the server on macOS, and pyplot picks its backend at import time, so
`matplotlib.use` after `import matplotlib.pyplot` is too late to be a guarantee.

Run with -s. Without it the measurements are swallowed and only the assertions remain.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  -- must follow the backend choice
import pytest  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def say(label: str, value: object) -> None:
    """Print one measurement. The label is what the decisions entry will quote."""
    print(f"    {label:<48} {value}")


@pytest.fixture(autouse=True)
def _no_figures_survive():
    """Every fact below closes what it opened, so an accumulation seen is one a test made."""
    plt.close("all")
    yield
    plt.close("all")


def test_the_backend_is_agg_and_no_gui_toolkit_is_loaded():
    """The guide's trap. Agg is headless; a GUI backend wants a main loop this server has not."""
    say("matplotlib.__version__", matplotlib.__version__)
    say("get_backend()", repr(matplotlib.get_backend()))
    assert matplotlib.get_backend().lower() == "agg"

    import sys
    gui = sorted(m for m in sys.modules if m.split(".")[0] in ("tkinter", "PyQt5", "PyQt6", "PySide6"))
    say("GUI toolkits imported", gui or "none")
    assert not gui, f"a GUI toolkit reached sys.modules: {gui}"


def test_savefig_writes_a_real_png(tmp_path):
    """A path is only worth returning if something openable is behind it."""
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [4, 5, 6])
    out = tmp_path / "line.png"
    fig.savefig(out)
    plt.close(fig)

    head = out.read_bytes()[:8]
    say("first 8 bytes", head)
    say("file size (bytes)", out.stat().st_size)
    assert head == PNG_MAGIC
    assert out.stat().st_size > 1000


def test_every_kind_the_guide_names_plots_from_plain_lists(tmp_path):
    """Eight kinds, guide line 943. Each is drawn from lists and saved, and the point is that
    none of them needs a conversion step the engine would have to own."""
    x = [1, 2, 3, 4, 5]
    y = [2.0, 4.5, 3.25, 6.0, 5.5]
    y2 = [1.0, 3.0, 2.0, 4.5, 4.0]
    grid = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]

    made: dict[str, int] = {}

    def save(name: str, draw) -> None:
        fig, ax = plt.subplots()
        draw(ax)
        out = tmp_path / f"{name}.png"
        fig.savefig(out)
        plt.close(fig)
        assert out.read_bytes()[:8] == PNG_MAGIC, f"{name} is not a PNG"
        made[name] = out.stat().st_size

    save("line", lambda ax: ax.plot(x, y))
    save("bar", lambda ax: ax.bar(x, y))
    save("grouped_bar", lambda ax: (ax.bar([i - 0.2 for i in x], y, width=0.4),
                                    ax.bar([i + 0.2 for i in x], y2, width=0.4)))
    save("scatter", lambda ax: ax.scatter(x, y))
    save("histogram", lambda ax: ax.hist(y, bins=4))
    save("box", lambda ax: ax.boxplot([y, y2]))
    save("heatmap", lambda ax: ax.imshow(grid))
    save("waterfall", lambda ax: ax.bar(x, y, bottom=[0.0, 2.0, 6.5, 9.75, 15.75]))

    for name, size in made.items():
        say(f"{name} png bytes", size)
    assert len(made) == 8, f"only {sorted(made)} were drawn"


def test_what_a_none_does_in_a_line_and_in_a_bar(tmp_path):
    """Decides who screens nulls: the chart layer or matplotlib.

    Every analysis in this engine can return a None cell -- base.number() keeps None as None by
    P8-D9 -- so a series reaching render_chart may hold one. Printed rather than asserted
    exactly, because the behaviour differs between artists and is what this fact is for.
    """
    x = [1, 2, 3, 4]
    holed = [1.0, None, 3.0, 4.0]

    fig, ax = plt.subplots()
    try:
        ax.plot(x, holed)
        fig.savefig(tmp_path / "line_none.png")
        line_result = "drew, a gap where the None was"
    except Exception as exc:  # noqa: BLE001
        line_result = f"{type(exc).__name__}: {exc}"
    finally:
        plt.close(fig)
    say("plot() with a None", line_result)

    fig, ax = plt.subplots()
    try:
        ax.bar(x, holed)
        fig.savefig(tmp_path / "bar_none.png")
        bar_result = "drew, the None bar absent or zero"
    except Exception as exc:  # noqa: BLE001
        bar_result = f"{type(exc).__name__}: {exc}"
    finally:
        plt.close(fig)
    say("bar() with a None", bar_result)

    assert line_result and bar_result


def test_strings_on_the_x_axis_keep_the_order_they_were_given(tmp_path):
    """A dimension's members arrive already ordered -- ranked_totals sorts biggest first -- so a
    chart that re-sorted them alphabetically would contradict the table beside it."""
    names = ["south", "north", "east", "west"]
    values = [4.0, 3.0, 2.0, 1.0]

    fig, ax = plt.subplots()
    ax.bar(names, values)
    labels = [t.get_text() for t in ax.get_xticklabels()]
    fig.savefig(tmp_path / "cats.png")
    plt.close(fig)

    say("x tick labels as drawn", labels)
    assert labels == names, "matplotlib reordered the categories"


def test_figures_accumulate_until_they_are_closed():
    """The leak that matters in a long-running MCP server: pyplot holds every figure it makes."""
    plt.close("all")
    before = len(plt.get_fignums())
    for _ in range(3):
        plt.figure()
    during = len(plt.get_fignums())
    plt.close("all")
    after = len(plt.get_fignums())

    say("fignums before / during / after", f"{before} / {during} / {after}")
    assert before == 0 and during == 3 and after == 0


def test_two_saves_of_the_same_data_are_byte_identical(tmp_path):
    """Decides whether a test can assert a chart by digest, or only by shape."""
    def draw(path: Path) -> None:
        fig, ax = plt.subplots()
        ax.plot([1, 2, 3], [4, 5, 6])
        fig.savefig(path)
        plt.close(fig)

    a, b = tmp_path / "a.png", tmp_path / "b.png"
    draw(a)
    draw(b)
    same = a.read_bytes() == b.read_bytes()
    say("two saves byte-identical", same)
    say("sizes", f"{a.stat().st_size} / {b.stat().st_size}")
    assert isinstance(same, bool)
