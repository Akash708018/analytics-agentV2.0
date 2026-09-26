"""Formatting helpers. Every tool returns a string built here."""

MAX_ROWS = 50
MAX_COLS = 50


def format_table(rows, headers):
    """Render rows as a pipe-delimited markdown table.

    Pipes (not padded spaces) so every cell has an unambiguous boundary.
    Caps rows and columns, and says so when it truncates.
    """
    headers = list(headers)
    rows = [list(r) for r in rows]

    notes = []
    if len(headers) > MAX_COLS:
        notes.append(f"showing first {MAX_COLS} of {len(headers)} columns")
        headers = headers[:MAX_COLS]
        rows = [r[:MAX_COLS] for r in rows]
    if len(rows) > MAX_ROWS:
        notes.append(f"showing first {MAX_ROWS} of {len(rows)} rows")
        rows = rows[:MAX_ROWS]

    if not headers:
        return "(no columns)"

    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        cells = [("" if c is None else str(c)) for c in r]
        cells += [""] * (len(headers) - len(cells))
        out.append("| " + " | ".join(cells[:len(headers)]) + " |")

    if not rows:
        out.append("| " + " | ".join("" for _ in headers) + " |")

    text = "\n".join(out)
    if notes:
        text += "\n\n(" + "; ".join(notes) + ")"
    return text


def format_kv(pairs):
    """Render label/value pairs as aligned plain text."""
    pairs = list(pairs)
    if not pairs:
        return "(nothing to report)"
    width = max(len(str(k)) for k, _ in pairs)
    return "\n".join(f"{str(k):<{width}} : {v}" for k, v in pairs)