"""The six graded agent metrics, derived from a finished run.

The hackathon's technical-quality criterion asks for evidence that the system
works, not an assurance. Each metric here is computed from what a run actually
recorded — the counters lanes increment, the warnings they raise, and the final
status of the tasks the router committed to — so none of it can drift away from
the code that produced it.

===  ==========================  ==================================================
 #   Metric                      Read from
===  ==========================  ==================================================
 1   Schema validation pass rate LLM calls vs. those whose output would not parse
 2   Tool-call success rate      per-adapter call and failure counters
 3   Task completion rate        PlannedTask.status over the plan the router made
 4   Token cost per run          provider usage, summed across lanes
 5   Loop discipline             reflect iterations against the cap held in state
 6   Answer fidelity             claims dropped for citing a URL not in the hit set
===  ==========================  ==================================================

Metrics 1, 2, 3, 5 and 6 are computable offline and are exercised by the
evaluation suite. Metric 4 needs one live run with a key configured.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# Warnings that mean a model returned something the code could not use. Counted
# rather than swallowed: the point of the metric is that these are visible.
SCHEMA_FAILURE_MARKERS = (
    "malformed_model_output",
    "unrecoverable_model_output",
    "intake_llm_unparseable",
    "router_llm_unparseable",
    "research_llm_unparseable",
    "dropped_field:",
)
TOOL_FAILURE_MARKERS = (
    "gem_unreachable",
    "gem_http_",
    "gem_program_not_found",
    "coursefinder_unavailable",
    "wise_unavailable",
    "city_unresolved",
    "search_unreachable",
    "search_http_",
    "search_no_results",
)
LLM_FAILURE_MARKERS = ("intake_llm_failed", "router_llm_failed", "research_llm_failed")


def _pct(numerator: float, denominator: float) -> float | None:
    """A rate, or ``None`` when nothing happened. Zero attempts is not zero percent."""
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


@dataclass
class RunMetrics:
    """Everything one turn is graded on."""

    # 1. schema validation
    llm_calls: int = 0
    schema_failures: int = 0

    # 2. tool calls
    tool_calls: int = 0
    tool_failures: int = 0

    # 3. task completion
    tasks_planned: int = 0
    tasks_complete: int = 0
    tasks_skipped: int = 0
    tasks_failed: int = 0

    # 4. token cost
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    # 5. loop discipline
    reflect_iterations: int = 0
    reflect_cap: int = 0
    hit_cap: bool = False

    # 6. answer fidelity
    sources_emitted: int = 0
    sources_dropped_by_policy: int = 0
    ungrounded_findings_dropped: int = 0

    # context
    intent: str = "unknown"
    lanes: list[str] = field(default_factory=list)

    @property
    def schema_pass_rate(self) -> float | None:
        return _pct(self.llm_calls - self.schema_failures, self.llm_calls)

    @property
    def tool_success_rate(self) -> float | None:
        return _pct(self.tool_calls - self.tool_failures, self.tool_calls)

    @property
    def task_completion_rate(self) -> float | None:
        """Skipped tasks count against completion deliberately.

        A lane that declined because it had no source did not complete the
        student's request, and a metric that pretended otherwise would reward
        the system for having fewer capabilities.
        """
        return _pct(self.tasks_complete, self.tasks_planned)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def loop_discipline_ok(self) -> bool:
        """True when the run stayed inside the cap held in state."""
        return self.reflect_cap == 0 or self.reflect_iterations <= self.reflect_cap

    @property
    def grounded_rate(self) -> float | None:
        """Share of model claims that cited a URL the search actually returned."""
        attempted = self.sources_emitted + self.ungrounded_findings_dropped
        return _pct(self.sources_emitted, attempted)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            schema_pass_rate=self.schema_pass_rate,
            tool_success_rate=self.tool_success_rate,
            task_completion_rate=self.task_completion_rate,
            total_tokens=self.total_tokens,
            loop_discipline_ok=self.loop_discipline_ok,
            grounded_rate=self.grounded_rate,
        )
        return data


def _count_markers(warnings: Iterable[str], markers: tuple[str, ...]) -> int:
    return sum(1 for w in warnings if any(marker in w for marker in markers))


def from_state(state: dict) -> RunMetrics:
    """Read the metrics off a finished run. Never raises on a partial state."""
    counters: dict[str, int] = dict(state.get("counters") or {})
    warnings: list[str] = [str(w) for w in (state.get("warnings") or [])]
    tasks = state.get("planned_tasks") or []
    envelope = state.get("envelope")

    if envelope is not None and getattr(envelope, "planned_tasks", None):
        tasks = envelope.planned_tasks

    statuses = [getattr(t, "status", "pending") for t in tasks]
    sources = list(getattr(envelope, "sources", []) or []) if envelope is not None else []

    llm_calls = int(counters.get("llm_calls", 0))
    schema_failures = _count_markers(warnings, SCHEMA_FAILURE_MARKERS)
    # A transport failure is not a schema failure, but it is still a call that
    # produced nothing usable, so it counts against the pass rate.
    schema_failures += _count_markers(warnings, LLM_FAILURE_MARKERS)

    return RunMetrics(
        llm_calls=llm_calls,
        schema_failures=min(schema_failures, llm_calls) if llm_calls else schema_failures,
        tool_calls=int(counters.get("tool_calls", 0)),
        tool_failures=int(counters.get("tool_failures", 0))
        or _count_markers(warnings, TOOL_FAILURE_MARKERS),
        tasks_planned=len(tasks),
        tasks_complete=statuses.count("complete"),
        tasks_skipped=statuses.count("skipped"),
        tasks_failed=statuses.count("failed"),
        input_tokens=int(counters.get("input_tokens", 0)),
        output_tokens=int(counters.get("output_tokens", 0)),
        cache_read_tokens=int(counters.get("cache_read_tokens", 0)),
        reflect_iterations=int(state.get("reflect_iterations") or 0),
        reflect_cap=int(state.get("max_reflect_iterations") or 0),
        hit_cap=bool(
            state.get("reflect_iterations")
            and state.get("max_reflect_iterations")
            and int(state["reflect_iterations"]) >= int(state["max_reflect_iterations"])
        ),
        sources_emitted=len(sources),
        sources_dropped_by_policy=int(counters.get("sources_dropped_by_policy", 0)),
        ungrounded_findings_dropped=int(counters.get("ungrounded_dropped", 0)),
        intent=str(state.get("intent") or "unknown"),
        lanes=list(state.get("lanes") or []),
    )


@dataclass
class MetricsReport:
    """The six metrics across a set of runs, for the evaluation slide."""

    runs: list[RunMetrics] = field(default_factory=list)
    answer_fidelity_passed: int = 0
    answer_fidelity_total: int = 0

    def add(self, run: RunMetrics) -> None:
        self.runs.append(run)

    def _sum(self, attribute: str) -> int:
        return sum(getattr(r, attribute) for r in self.runs)

    @property
    def schema_pass_rate(self) -> float | None:
        calls = self._sum("llm_calls")
        return _pct(calls - self._sum("schema_failures"), calls)

    @property
    def tool_success_rate(self) -> float | None:
        calls = self._sum("tool_calls")
        return _pct(calls - self._sum("tool_failures"), calls)

    @property
    def task_completion_rate(self) -> float | None:
        return _pct(self._sum("tasks_complete"), self._sum("tasks_planned"))

    @property
    def mean_tokens_per_run(self) -> float | None:
        """None when no LLM ran at all.

        Zero tokens across zero calls is "not measured", and printing it as
        0 would claim a cost result the run never produced.
        """
        if not self.runs or self._sum("llm_calls") == 0:
            return None
        return round(self._sum("total_tokens") / len(self.runs), 1)

    @property
    def loop_discipline_rate(self) -> float | None:
        """Share of runs that respected the cap. Anything below 100% is a bug."""
        if not self.runs:
            return None
        return _pct(sum(1 for r in self.runs if r.loop_discipline_ok), len(self.runs))

    @property
    def runs_hitting_cap(self) -> int:
        return sum(1 for r in self.runs if r.hit_cap)

    @property
    def answer_fidelity_rate(self) -> float | None:
        return _pct(self.answer_fidelity_passed, self.answer_fidelity_total)

    @property
    def grounded_rate(self) -> float | None:
        emitted = self._sum("sources_emitted")
        return _pct(emitted, emitted + self._sum("ungrounded_findings_dropped"))

    def summary(self) -> dict[str, Any]:
        return {
            "runs": len(self.runs),
            "1_schema_validation_pass_rate": self.schema_pass_rate,
            "2_tool_call_success_rate": self.tool_success_rate,
            "3_task_completion_rate": self.task_completion_rate,
            "4_mean_tokens_per_run": self.mean_tokens_per_run,
            "5_loop_discipline_rate": self.loop_discipline_rate,
            "5_runs_hitting_cap": self.runs_hitting_cap,
            "6_answer_fidelity_rate": self.answer_fidelity_rate,
            "6_grounded_claim_rate": self.grounded_rate,
            "sources_dropped_by_policy": self._sum("sources_dropped_by_policy"),
            "ungrounded_findings_dropped": self._sum("ungrounded_findings_dropped"),
        }

    def render(self) -> str:
        """A plain-text table, for the README and the evaluation slide."""
        rows = [
            ("1. Schema validation pass rate", _fmt(self.schema_pass_rate, "%")),
            ("2. Tool-call success rate", _fmt(self.tool_success_rate, "%")),
            ("3. Task completion rate", _fmt(self.task_completion_rate, "%")),
            ("4. Mean tokens per run", _fmt(self.mean_tokens_per_run, "")),
            ("5. Loop discipline", _fmt(self.loop_discipline_rate, "%")),
            ("6. Answer fidelity", _fmt(self.answer_fidelity_rate, "%")),
            ("   Grounded claim rate", _fmt(self.grounded_rate, "%")),
        ]
        width = max(len(label) for label, _ in rows)
        lines = [f"Metrics over {len(self.runs)} run(s)", "-" * (width + 12)]
        lines += [f"{label.ljust(width)}  {value}" for label, value in rows]
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(self.summary(), indent=2)


def _fmt(value: float | None, suffix: str) -> str:
    """Not measured is not zero, and must not be printed as if it were."""
    return "not measured" if value is None else f"{value}{suffix}"
