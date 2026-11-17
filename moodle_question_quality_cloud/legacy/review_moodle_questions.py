#!/usr/bin/env python3
"""Screen a Moodle question statistics CSV and create a review CSV + summary.

Uses only Python's standard library. This is a triage tool: metrics flag items
for attention but never assign Keep/Fix/Replace automatically.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
from pathlib import Path


FIELDS = {
    "question_id": ("Q#", "Question number", "Question #"),
    "question_type": ("Question type", "Type"),
    "question_name": ("Question name", "Name"),
    "attempts": ("Attempts", "Number of attempts"),
    "facility": ("Facility index", "Facility"),
    "random_guess": ("Random guess score", "Random guess"),
    "discrimination": ("Discrimination index", "Discrimination"),
    "efficiency": ("Discriminative efficiency", "Efficiency"),
}


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def find_column(header: list[str], aliases: tuple[str, ...]) -> int | None:
    names = {norm(name): i for i, name in enumerate(header)}
    for alias in aliases:
        if norm(alias) in names:
            return names[norm(alias)]
    return None


def parse_number(value: str, percent: bool = False) -> float | None:
    value = (value or "").strip().replace("\u00a0", "")
    if not value:
        return None
    if percent:
        value = value.replace("%", "").replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def read_items(path: Path) -> tuple[list[dict[str, str]], str]:
    # Moodle's full report has a quiz summary first, then a second header row
    # for item analysis. Find the real item header instead of trusting row 1.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    header_index = None
    column_indexes: dict[str, int] = {}
    for row_index, row in enumerate(rows):
        indexes = {key: find_column(row, aliases) for key, aliases in FIELDS.items()}
        if all(indexes[key] is not None for key in ("question_id", "question_type", "question_name", "attempts", "facility")):
            header_index = row_index
            column_indexes = {key: value for key, value in indexes.items() if value is not None}
            break
    if header_index is None:
        raise ValueError("Could not find the Moodle item-analysis header row (Q#, Question type, Attempts, Facility index).")

    items: list[dict[str, str]] = []
    for row in rows[header_index + 1 :]:
        if not row or not any(cell.strip() for cell in row):
            continue
        # Complete statistics exports append answer-response analysis after the
        # item-analysis table. Numeric answer strings can look like Q# values!
        # Stop at the explicit section header instead of treating answers as items.
        if len(row) >= 4 and (norm(row[0]) == "part of question" and
                                 norm(row[1]) in ("response", "model response") and
                                 norm(row[2]) == "partial credit"):
            break
        if len(row) >= 4 and (norm(row[0]) == "model response" and
                                 norm(row[1]) == "partial credit"):
            break
        def cell(key: str) -> str:
            index = column_indexes.get(key)
            return row[index].strip() if index is not None and index < len(row) else ""

        qid = cell("question_id")
        kind = cell("question_type")
        # Parent rows describe quiz positions, not question-bank items.
        if not qid or not re.fullmatch(r"\d+(?:\.\d+)?", qid) or norm(kind) == "random":
            continue
        if not cell("question_name") and not kind:
            continue
        items.append({
            "question_id": qid,
            "question_type": kind,
            "question_name": cell("question_name"),
            "attempts": cell("attempts"),
            "facility": cell("facility"),
            "random_guess": cell("random_guess"),
            "discrimination": cell("discrimination"),
            "efficiency": cell("efficiency"),
        })
    return items, "combined Moodle report" if header_index > 0 else "question stats export"


def assess(item: dict[str, str], minimum_attempts: int, low_facility: float, easy_facility: float) -> tuple[str, str]:
    attempts = parse_number(item["attempts"])
    facility = parse_number(item["facility"], percent=True)
    discrimination = parse_number(item["discrimination"], percent=True)

    if attempts is None:
        return "Check export", "Attempt count could not be read."
    if attempts < minimum_attempts:
        note = f"Only {int(attempts)} attempts; wait for more data before judging performance."
        if facility is not None and facility < low_facility:
            note += " Observed facility is low, so spot-check the question and grading."
        return "Insufficient data", note

    flags: list[str] = []
    if facility is not None and facility < low_facility:
        flags.append(f"low facility ({facility:g}%)")
    if discrimination is not None and discrimination <= 0:
        flags.append(f"non-positive discrimination ({discrimination:g}%)")
    if flags:
        return "Review", "Flagged: " + "; ".join(flags) + ". Inspect the prompt, expected answer, and responses before choosing Fix or Replace."
    if facility is not None and facility >= easy_facility:
        return "Check difficulty", f"Very high facility ({facility:g}%); confirm the item still provides useful challenge. This alone is not a reason to replace it."
    return "No statistical flag", "No configured statistical warning. Keep this as a provisional result; metrics do not verify content or answer-key correctness."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", help="Path to Moodle questionstats CSV; the full questionstats (1).csv export is recommended.")
    parser.add_argument("-o", "--output", help="Output CSV path (default: next to input, named *-review.csv).")
    parser.add_argument("--minimum-attempts", type=int, default=5, help="Below this count, mark as Insufficient data (default: 5).")
    parser.add_argument("--low-facility", type=float, default=50.0, help="Flag facility below this percentage (default: 50).")
    parser.add_argument("--easy-facility", type=float, default=95.0, help="Flag very high facility for difficulty check (default: 95).")
    args = parser.parse_args()

    source = Path(args.input_csv).expanduser().resolve()
    if not source.is_file():
        print(f"Input file not found: {source}", file=sys.stderr)
        return 2
    if args.minimum_attempts < 1:
        parser.error("--minimum-attempts must be at least 1")
    try:
        items, report_type = read_items(source)
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        print(f"Could not read Moodle export: {error}", file=sys.stderr)
        return 2
    if not items:
        print("No question-bank item rows were found. Use the full questionstats (1).csv export.", file=sys.stderr)
        return 2

    for item in items:
        item["assessment"], item["signal"] = assess(item, args.minimum_attempts, args.low_facility, args.easy_facility)
        observed_facility = parse_number(item["facility"], percent=True)
        if item["assessment"] in {"Review", "Check export"}:
            item["suggested_next_step"] = "Inspect this item in responses.csv"
        elif item["assessment"] == "Insufficient data" and observed_facility is not None and observed_facility < args.low_facility:
            item["suggested_next_step"] = "Optional spot-check in responses.csv (low observed facility; too few attempts to conclude)"
        elif item["assessment"] == "Insufficient data":
            item["suggested_next_step"] = "Wait for more attempts; no manual check needed yet"
        elif item["assessment"] == "Check difficulty":
            item["suggested_next_step"] = "Confirm the intended difficulty; no response-by-response check needed"
        else:
            item["suggested_next_step"] = "No immediate follow-up"
        item["decision_keep_fix_replace"] = ""
        item["review_note"] = ""

    output = Path(args.output).expanduser().resolve() if args.output else source.with_name(source.stem + "-review.csv")
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = ["question_id", "question_type", "question_name", "attempts", "facility_percent", "random_guess_percent", "discrimination_percent", "discriminative_efficiency_percent", "assessment", "signal", "suggested_next_step", "decision_keep_fix_replace", "review_note"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow({
                **item,
                "facility_percent": item["facility"],
                "random_guess_percent": item["random_guess"],
                "discrimination_percent": item["discrimination"],
                "discriminative_efficiency_percent": item["efficiency"],
            })

    counts: dict[str, int] = {}
    for item in items:
        counts[item["assessment"]] = counts.get(item["assessment"], 0) + 1
    summary = output.with_suffix(".md")
    with summary.open("w", encoding="utf-8") as handle:
        handle.write("# Moodle question review\n\n")
        handle.write(f"- Source: `{source.name}` ({report_type})\n- Question-bank items: {len(items)}\n")
        handle.write(f"- Rules: fewer than {args.minimum_attempts} attempts = insufficient data; facility below {args.low_facility:g}% or discrimination at/below 0% = review; facility at/above {args.easy_facility:g}% = difficulty check.\n\n")
        handle.write("## Triage counts\n\n")
        for label in ("Review", "Insufficient data", "Check difficulty", "No statistical flag", "Check export"):
            if counts.get(label):
                handle.write(f"- {label}: {counts[label]}\n")
        handle.write("\nThis is a screening report. It does not verify question wording, answer keys, curriculum fit, or Moodle grading. Check responses.csv for Review items and, optionally, low-facility items with too few attempts. Do not review every low-attempt item. Record Keep/Fix/Replace in the blank decision column after review.\n")
    html_path = output.with_suffix(".html")
    visible_order = ("Review", "Check difficulty", "Insufficient data", "No statistical flag", "Check export")
    cards = "".join(
        f'<div class="card"><strong>{counts.get(label, 0)}</strong><span>{html.escape(label)}</span></div>'
        for label in visible_order if counts.get(label, 0)
    )
    bars = "".join(
        '<div class="bar-row"><span>' + html.escape(label) + '</span><div class="track"><i class="bar bar-' +
        str(index) + '" style="width:' + str((counts.get(label, 0) / max(len(items), 1)) * 100) + '%"></i></div><b>' +
        str(counts.get(label, 0)) + '</b></div>'
        for index, label in enumerate(visible_order) if counts.get(label, 0)
    )
    table_rows = []
    for item in items:
        assessment = html.escape(item["assessment"])
        title = html.escape(item["question_name"])
        signal = html.escape(item["signal"])
        table_rows.append(
            '<tr data-assessment="' + assessment + '"><td>' + html.escape(item["question_id"]) +
            '</td><td>' + html.escape(item["question_type"]) + '</td><td>' + title +
            '</td><td>' + html.escape(item["attempts"]) + '</td><td>' + html.escape(item["facility"]) +
            '</td><td>' + html.escape(item["discrimination"]) + '</td><td><span class="badge">' + assessment +
            '</span></td><td>' + signal + '</td></tr>'
        )
    with html_path.open("w", encoding="utf-8") as handle:
        handle.write("""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moodle question review</title><style>
body{font:15px/1.45 system-ui,Segoe UI,sans-serif;color:#17202a;background:#f5f7fa;margin:0;padding:28px}main{max-width:1400px;margin:auto}h1{margin:0 0 6px}.muted{color:#586575}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.card{background:white;border:1px solid #dce2e9;border-radius:10px;padding:14px 18px;min-width:130px}.card strong{font-size:24px;display:block}.card span{color:#586575}.controls{display:flex;gap:10px;margin:16px 0}.controls input,.controls select{font:inherit;padding:9px 11px;border:1px solid #c8d0da;border-radius:7px;background:white}.controls input{flex:1}table{border-collapse:collapse;width:100%;background:white;border:1px solid #dce2e9}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e8ecf1;vertical-align:top}th{position:sticky;top:0;background:#edf1f6}td:nth-child(3){min-width:240px}.badge{display:inline-block;background:#e8edf3;border-radius:999px;padding:3px 9px;white-space:nowrap}tr[data-assessment="Review"] .badge{background:#ffe0df;color:#8b1b16}tr[data-assessment="Check difficulty"] .badge{background:#fff0cc;color:#725000}tr[data-assessment="Insufficient data"] .badge{background:#e9edf2;color:#4c5663}@media(max-width:800px){body{padding:12px}.table-wrap{overflow:auto}table{min-width:1000px}}
</style><style>.chart{max-width:760px;background:white;border:1px solid #dce2e9;border-radius:10px;padding:16px 18px;margin:18px 0}.chart h2{font-size:17px;margin:0 0 12px}.bar-row{display:grid;grid-template-columns:170px 1fr 38px;gap:12px;align-items:center;margin:9px 0;font-size:13px}.track{height:12px;border-radius:8px;background:#eef1f5;overflow:hidden}.bar{display:block;height:100%;border-radius:8px;background:#90a4b8}.bar-0{background:#d94f45}.bar-1{background:#dda51e}.bar-2{background:#8291a2}.bar-3{background:#39a77b}.bar-4{background:#d94f45}@media(max-width:800px){.bar-row{grid-template-columns:120px 1fr 28px}}</style><main>""")
        handle.write(f"<h1>Moodle question review</h1><div class=muted>Source: {html.escape(source.name)} · {len(items)} question-bank items</div>")
        handle.write(f'<section class="cards">{cards}</section>')
        handle.write(f'<section class="chart"><h2>Questions by review flag</h2>{bars}</section>')
        handle.write('<div class="controls"><input id="search" placeholder="Search question names or signals"><select id="filter"><option value="">All assessments</option>')
        for label in visible_order:
            if counts.get(label):
                safe_label = html.escape(label, quote=True)
                handle.write(f'<option value="{safe_label}">{html.escape(label)}</option>')
        handle.write('</select></div><div class="table-wrap"><table><thead><tr><th>Moodle ID</th><th>Type</th><th>Question</th><th>Attempts</th><th>Facility</th><th>Discrimination</th><th>Flag</th><th>Reason</th></tr></thead><tbody>')
        handle.write("".join(table_rows))
        handle.write("""</tbody></table></div><p class="muted">Flags are screening signals, not automatic Keep/Fix/Replace decisions. Review low-attempt items only when the observed facility is also low.</p>
<script>const q=document.querySelector('#search'),f=document.querySelector('#filter');function apply(){const s=q.value.toLocaleLowerCase();for(const r of document.querySelectorAll('tbody tr')){r.hidden=(f.value&&r.dataset.assessment!==f.value)||!r.innerText.toLocaleLowerCase().includes(s)}}q.addEventListener('input',apply);f.addEventListener('change',apply)</script></main></html>""")
    print(f"Read {len(items)} question-bank items from {source.name} ({report_type}).")
    print("Triage: " + ", ".join(f"{label} {count}" for label, count in counts.items()))
    print(f"CSV: {output}\nVisual report: {html_path}\nSummary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
