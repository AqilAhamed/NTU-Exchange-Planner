# Evaluation

## Status: not currently measured

**This project has no automated tests.** There is no `backend/tests/`
directory, no evaluation harness, and no reproducible metrics run.

Earlier versions of this page reported six graded metrics — schema validation
pass rate, tool-call success, task completion, tokens per run, loop discipline
and answer fidelity — as measured figures. Those numbers came from a suite that
is not in this repository, so nothing here can reproduce them and they have
been removed rather than left standing. A number you cannot re-derive is not
evidence, and this project's whole argument is that a figure without a source
does not get shown.

What follows is what is actually true today: the measurement *machinery*
exists and is real code, but nothing has been measured with it.

---

## The machinery that exists

`graph/metrics.py` computes six metrics from a finished run. It is not a
placeholder — every value is read off what a run actually recorded, so none of
it can drift from the code that produced it.

| # | Metric | Read from |
|---|---|---|
| 1 | Schema validation pass rate | LLM calls vs. those whose output would not parse |
| 2 | Tool-call success rate | per-adapter `tool_calls` / `tool_failures` counters |
| 3 | Task completion rate | `PlannedTask.status` over the plan the router committed to |
| 4 | Token cost per run | provider usage, summed across lanes |
| 5 | Loop discipline | reflect iterations against the cap held in state |
| 6 | Answer fidelity | claims dropped for citing a URL not in the search hit set |

`metrics.from_state(final_state)` returns a `RunMetrics`; `MetricsReport`
aggregates across runs and renders a table. Both are importable and working.
What is missing is the harness that drives a ground-truth set through them.

Every lane feeds the counters these metrics read, including the ChromaDB
general-questions lane, which previously reported none — so metrics 2 and 6
were blind to it while being presented as system-wide.

`_fmt` prints `not measured` rather than `0` for an absent metric, on purpose:
zero tokens across zero calls is not a cost result.

---

## What would have to be built

To make this page report figures again:

1. **A ground-truth set.** Question, expected lanes, and the property the
   answer must hold — not an expected string. Roughly 20–25 cases covering the
   shortlist, workload, finance, research, conversion, the disabled
   official-documents lane, and the RAG lane.
2. **A runner** that executes each case with the provider pinned off, collects
   `metrics.from_state`, and prints `MetricsReport.render()`.
3. **Hermetic fixtures.** Captured GEM brochures and search responses, with
   providers pinned explicitly. A previous suite had two cases that passed only
   because nothing was listening on port 7000 — the evaluation was reporting on
   the machine rather than on the code.

Until that exists, this page says so.

---

## Properties the system is built to hold

These are claims the code makes structurally. They are stated here as design
intent, **not** as verified results — nothing currently checks them.

- **No answer carries a blank source URL.** `policy.screen` drops a source with
  no URL and records why.
- **No source survives that the active lanes are not permitted to emit.**
  `policy.LANE_SOURCES` is an allow-list; `render_agent` is the single choke
  point that applies it.
- **No task the router planned is left `pending`.** `render_agent._apply_statuses`
  resolves every committed task to `complete`, `skipped` or `failed`.
- **The reflect loop is bounded by a counter in state**, and a retry may only
  run a lane that has not already run — so a loop cannot duplicate evidence or
  spin on a problem re-running cannot fix.
- **A shortlist cannot list one university twice.** Enforced by the
  `merge_universities` reducer, not by convention.
- **Mapping-type exclusions are applied inside SQL**, before grouping and
  pagination, so an excluded row cannot reappear in a detail table.

Each is a candidate for the first tests written.

---

## What is known to be unmeasured

Stated plainly, because a page that only reports wins is not evidence.

- **Everything above.** There is no test run behind any of it.
- **The GEM parser is validated against 6 brochures of 385**, by hand. It
  degrades honestly on what it cannot read — Macalester publishes nothing and
  the parser says so — but coverage across the full set is unknown.
- **GEM name matching reaches 525 of 558 universities (94%)**, measured once by
  hand. The remainder are SUSEP-only, historical, or genuinely ambiguous; those
  get a caveat rather than a guess.
- **Host-city resolution is 5 of 7 on the sample checked**, by hand. One case
  returns nothing (correct — no estimate is then given) and one resolves
  University of Waterloo to the neighbouring township of Wilmot. Every estimate
  names the city and the method that found it.
- **A CGPA-filtered shortlist checks at most 50 candidates**, because each costs
  a brochure read. When more partners match, the shortfall is stated in the
  response rather than folded into the total.
- **There are no frontend tests.** React rendering was verified by driving the
  running app in a browser.
