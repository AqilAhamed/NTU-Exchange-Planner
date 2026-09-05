"use client";

import { useEffect, useState } from "react";
import type {
  BudgetEstimate,
  Confidence,
  EligibilityEvidence,
  PlannedTask,
  SourceEvidence,
  WorkloadEvidence,
} from "@/lib/api";

/* Shared chrome ---------------------------------------------------------- */

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="glass rounded-[18px] px-4 py-3">
      <h4 className="text-[11px] font-semibold uppercase tracking-wide text-[var(--link)]">
        {title}
      </h4>
      <div className="mt-2 space-y-2 text-[13px] leading-6 text-[var(--ink)]">{children}</div>
    </section>
  );
}

function Chip({ tone, children }: { tone: "good" | "bad" | "unknown" | "muted"; children: React.ReactNode }) {
  const tones: Record<string, string> = {
    good: "border-[color:var(--link)] text-[var(--link)]",
    bad: "border-[color:var(--danger)] text-[var(--danger)]",
    unknown: "border-[color:var(--line)] text-[var(--muted)]",
    muted: "border-[color:var(--line)] text-[var(--faint)]",
  };
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/* Live task progress ------------------------------------------------------ */

const TASK_LABELS: Record<PlannedTask["kind"], string> = {
  profile: "Reading your profile",
  course_matching: "Matching modules",
  workload: "Reading course loads",
  finance: "Estimating costs",
  research: "Researching",
  conversion: "Converting units",
  official_docs: "Checking NTU policy",
  general_questions: "Checking NTU student reference documents",
  clarification: "Asking a question",
};

const STATUS_MARK: Record<PlannedTask["status"], string> = {
  pending: "·",
  running: "◐",
  complete: "✓",
  skipped: "—",
  failed: "✕",
};

/**
 * The plan, as it happens.
 *
 * The router commits to these tasks before any lane runs, so this panel shows
 * the system deciding what to do and then doing it — rather than a spinner that
 * says nothing and then one blob of text.
 */
export function TaskProgress({ tasks, live }: { tasks: PlannedTask[]; live?: boolean }) {
  if (!tasks.length) return null;
  return (
    <section className="glass rounded-[18px] px-4 py-3">
      <h4 className="text-[11px] font-semibold uppercase tracking-wide text-[var(--link)]">
        {live ? "Working" : "What I did"}
      </h4>
      <ul className="mt-2 space-y-1">
        {tasks.map((task, i) => (
          <li key={`${task.kind}-${i}`} className="flex items-start gap-2 text-[12px]">
            <span
              aria-hidden
              className={
                task.status === "complete"
                  ? "text-[var(--link)]"
                  : task.status === "failed"
                    ? "text-[var(--danger)]"
                    : "text-[var(--muted)]"
              }
            >
              {STATUS_MARK[task.status]}
            </span>
            <span className="min-w-0 flex-1">
              <span className="text-[var(--ink)]">{TASK_LABELS[task.kind] ?? task.kind}</span>
              {task.description ? (
                <span className="text-[var(--muted)]"> — {task.description}</span>
              ) : null}
              {task.status === "skipped" ? (
                <span className="text-[var(--faint)]"> (not available)</span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/* Sources ----------------------------------------------------------------- */

const SOURCE_LABELS: Record<SourceEvidence["type"], string> = {
  coursefinder: "Coursefinder",
  gem_explorer: "GEM Explorer",
  ntu_intranet: "NTU intranet",
  official: "Official",
  web: "Web",
  reddit: "Forum",
  wise: "Wise",
  local: "Computed",
  unknown: "Unknown",
};

function when(iso: string): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleDateString();
}

/** Every claim the answer makes, with the link that proves it. */
export function SourceList({ sources }: { sources: SourceEvidence[] }) {
  const [open, setOpen] = useState(false);
  const uniqueSources = sources.filter(
    (source, index, all) => all.findIndex((candidate) => candidate.url === source.url) === index,
  );
  if (!uniqueSources.length) return null;
  return (
    <section className="glass rounded-[18px] px-4 py-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-3 text-left"
        aria-expanded={open}
      >
          <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--link)]">
          {uniqueSources.length === 1 ? "Source" : `Sources (${uniqueSources.length})`}
        </span>
        <DisclosureIcon open={open} />
      </button>
      {open ? (
        <div className="mt-2 space-y-2 text-[13px] leading-6 text-[var(--ink)]">
          <ul className="space-y-2">
            {uniqueSources.map((source, i) => (
              <li key={`${source.url}-${i}`} className="min-w-0">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  {uniqueSources.length > 1 ? (
                    <span
                      aria-label={`Source ${i + 1}`}
                      className="flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--active)] px-1.5 text-[11px] font-semibold text-[var(--link)]"
                    >
                      {i + 1}
                    </span>
                  ) : null}
                  <Chip tone="muted">{SOURCE_LABELS[source.type] ?? source.type}</Chip>
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="app-link min-w-0 break-words text-[12px] font-medium"
                  >
                    {source.title || source.url}
                  </a>
                  {when(source.retrieved_at) ? (
                    <span className="text-[11px] text-[var(--faint)]">
                      retrieved {when(source.retrieved_at)}
                    </span>
                  ) : null}
                </div>
                {source.excerpt ? (
                  <p className="mt-0.5 break-words text-[12px] text-[var(--muted)]">{source.excerpt}</p>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

/* Workload ---------------------------------------------------------------- */

function bounds(evidence: WorkloadEvidence): string {
  const unit = evidence.unit_label || "units";
  const { minimum_value: low, maximum_value: high } = evidence;
  if (low != null && high != null) return low === high ? `${low} ${unit}` : `${low}–${high} ${unit}`;
  if (high != null) return `up to ${high} ${unit}`;
  if (low != null) return `at least ${low} ${unit}`;
  return "no numeric bounds published";
}

/**
 * A host university's course load, in that university's own units.
 *
 * A converted figure appears only when `conversion_status` is "supported",
 * meaning the brochure itself stated the equivalence. Everywhere else the panel
 * says the conversion is not available and names what to verify — which is the
 * honest reading of a university that publishes ECTS or credits and says
 * nothing at all about NTU academic units.
 */
export function WorkloadPanel({ evidence }: { evidence: WorkloadEvidence[] }) {
  if (!evidence.length) return null;
  return (
    <Panel title="Course load, in each university's own units">
      {evidence.map((item, i) => {
        const basis = item.conversion_basis;
        const supported = item.conversion_status === "supported" && basis;
        return (
          <div key={`${item.university_name}-${i}`} className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-[var(--ink)]">{item.university_name}</span>
              <Chip tone={supported ? "good" : "unknown"}>
                {supported ? `converts to ${basis!.to_unit}` : "conversion not available"}
              </Chip>
            </div>
            <p>{bounds(item)}</p>
            {item.module_count_minimum != null || item.module_count_maximum != null ? (
              <p className="text-[12px] text-[var(--muted)]">
                {/(?:undergraduate|undergrad|bachelor)/i.test(item.raw_source_excerpt)
                  ? "Undergraduate course count: "
                  : "Course count: "}{item.module_count_minimum != null && item.module_count_maximum != null
                  ? item.module_count_minimum === item.module_count_maximum
                    ? `typically ${item.module_count_minimum}`
                    : `typically ${item.module_count_minimum}–${item.module_count_maximum}`
                  : item.module_count_minimum != null
                    ? `at least ${item.module_count_minimum}`
                    : `up to ${item.module_count_maximum}`} course{item.module_count_maximum !== 1 ? "s" : ""}
              </p>
            ) : null}
            {item.native_unit_text ? (
              <p className="text-[12px] text-[var(--muted)]">
                Published rule: {item.native_unit_text}
              </p>
            ) : null}
            {supported ? (
              <p className="text-[12px] text-[var(--muted)]">
                Converted on this university&apos;s own statement: “{basis!.source_excerpt}”
                {basis!.to_unit !== "AU"
                  ? ` — that is ${basis!.to_unit}, not NTU academic units.`
                  : ""}
              </p>
            ) : (
              <p className="text-[12px] text-[var(--muted)]">
                No AU figure is shown. This university publishes no equivalence to NTU academic
                units, so converting would mean inventing a factor. Verify the AU award with your
                school&apos;s exchange coordinator before you plan around it.
              </p>
            )}
            {item.planning_reference ? (
              <p className="rounded-xl border border-[color:var(--line)] bg-[var(--hover)] px-3 py-2 text-[12px] text-[var(--muted)]">
                {item.planning_reference}
              </p>
            ) : null}
            {item.source_url ? (
              <a
                href={item.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="app-link text-[12px]"
              >
                Read the brochure
              </a>
            ) : null}
          </div>
        );
      })}
    </Panel>
  );
}

/* Eligibility -------------------------------------------------------------- */

/**
 * A published entry requirement, checked against the student's own figure.
 *
 * Tri-state on purpose. "Not published" is not "fails": a university whose
 * requirement cannot be read stays on the shortlist, because silently dropping
 * it would hide an option the student is very possibly eligible for.
 */
export function EligibilityBadges({ evidence }: { evidence: EligibilityEvidence[] }) {
  if (!evidence.length) return null;
  return (
    <Panel title="Entry requirements">
      {evidence.map((item, i) => (
        <div key={`${item.university_name}-${i}`} className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-[var(--ink)]">{item.university_name}</span>
          <Chip tone={item.meets === true ? "good" : item.meets === false ? "bad" : "unknown"}>
            {item.meets === true ? "you meet this" : item.meets === false ? "below the minimum" : "not checked"}
          </Chip>
          <span className="text-[12px] text-[var(--muted)]">{item.requirement}</span>
          {item.source?.url ? (
            <a
              href={item.source.url}
              target="_blank"
              rel="noopener noreferrer"
              className="app-link text-[12px]"
            >
              source
            </a>
          ) : null}
          {item.notes.map((note) => (
            <span key={note} className="text-[11px] text-[var(--faint)]">
              {note}
            </span>
          ))}
        </div>
      ))}
    </Panel>
  );
}

/* Budget ------------------------------------------------------------------- */

function money(amount?: number | null, currency?: string | null): string {
  if (amount == null) return "—";
  const rounded = amount.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return currency ? `${rounded} ${currency}` : rounded;
}

/**
 * A monthly budget, with the provenance of every number attached.
 *
 * Two states matter and are shown differently. Figures the university itself
 * published are labelled as such; a cost-of-living estimate is labelled an
 * estimate and names the city it was costed against, because the city was
 * inferred rather than stated. A missing total is left blank instead of being
 * filled by summing across currencies — that number would exist nowhere in any
 * source.
 */
export function BudgetPanel({ budget }: { budget?: BudgetEstimate | null }) {
  const property = budget?.components.find((component) =>
    component.label.trim().toLowerCase().startsWith("property"),
  );
  const hasRentToggle = Boolean(
    property && budget?.monthly_total != null && property.amount != null,
  );
  const [includeRent, setIncludeRent] = useState(
    budget?.rent_included ?? property?.included ?? true,
  );

  // A single conversation can replace the budget prop with another university.
  // Reset the local toggle so the new card starts from the source's state.
  useEffect(() => {
    setIncludeRent(budget?.rent_included ?? property?.included ?? true);
  }, [
    budget?.university_name,
    budget?.monthly_total,
    budget?.rent_included,
    property?.label,
    property?.amount,
    property?.included,
  ]);

  // A budget with neither a total nor any components carries no information.
  // Rendering it shows an em-dash under an "estimate" badge, which reads as a
  // figure that failed to load rather than as a lane that had nothing to say.
  // The reason it had nothing is already in the caveats.
  if (!budget) return null;
  if (budget.monthly_total == null && budget.components.length === 0) return null;

  const sourceIncludesRent = budget.rent_included ?? property?.included ?? false;
  const monthlyTotal =
    budget.monthly_total != null && property?.amount != null && hasRentToggle
      ? sourceIncludesRent === includeRent
        ? budget.monthly_total
        : sourceIncludesRent
          ? budget.monthly_total - property.amount
          : budget.monthly_total + property.amount
      : budget.monthly_total;
  const counted = budget.components.filter((component) => {
    if (component === property) return hasRentToggle ? includeRent : component.included;
    return component.included;
  });
  const shown = budget.components.filter((component) => !counted.includes(component));
  const sourceLabel = budget.source_label?.replace(/,\s*(?:excluding|including) rent/gi, "");

  return (
    <Panel title="Monthly budget">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[18px] font-semibold text-[var(--ink)]">
          {money(monthlyTotal, budget.monthly_total_currency)}
        </span>
        <span className="text-[12px] text-[var(--muted)]">per month</span>
        <Chip tone={budget.fallback_used ? "unknown" : "good"}>
          {budget.fallback_used ? "estimate" : "published by the university"}
        </Chip>
        {sourceLabel ? (
          <span className="text-[11px] text-[var(--faint)]">{sourceLabel}</span>
        ) : null}
        {budget.rent_included != null || hasRentToggle ? (
          <Chip tone="muted">
            {hasRentToggle
              ? includeRent
                ? "rent included"
                : "rent not included"
              : budget.rent_included
                ? "rent included"
                : "rent not included"}
          </Chip>
        ) : null}
      </div>

      {hasRentToggle ? (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[12px] text-[var(--muted)]">Rent / property</span>
          <div
            className="inline-flex rounded-full border border-[color:var(--line)] bg-[var(--hover)] p-0.5"
            role="group"
            aria-label="Rent inclusion"
          >
            <button
              type="button"
              onClick={() => setIncludeRent(false)}
              aria-pressed={!includeRent}
              className={`rounded-full px-2.5 py-1 text-[11px] transition ${
                !includeRent
                  ? "bg-[var(--bg)] font-semibold text-[var(--ink)] shadow-sm"
                  : "text-[var(--muted)]"
              }`}
            >
              Exclude rent
            </button>
            <button
              type="button"
              onClick={() => setIncludeRent(true)}
              aria-pressed={includeRent}
              className={`rounded-full px-2.5 py-1 text-[11px] transition ${
                includeRent
                  ? "bg-[var(--bg)] font-semibold text-[var(--ink)] shadow-sm"
                  : "text-[var(--muted)]"
              }`}
            >
              Include rent
            </button>
          </div>
        </div>
      ) : null}

      {budget.monthly_total == null ? (
        <p className="text-[12px] text-[var(--muted)]">
          No single total is shown, because these figures cannot be added together honestly.
        </p>
      ) : null}

      {counted.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[260px] text-left text-[12px]">
            <thead>
              <tr className="text-[var(--faint)]">
                <th className="pb-1 font-medium">Item</th>
                <th className="pb-1 font-medium">Per month</th>
              </tr>
            </thead>
            <tbody>
              {counted.map((c, i) => (
                <tr key={`${c.label}-${i}`} className="border-t border-[color:var(--line)]">
                  <td className="py-1 pr-2">{c.label}</td>
                  <td className="py-1">{money(c.amount, c.currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {shown.length > 0 ? (
        <p className="text-[12px] text-[var(--muted)]">
          Also published, not added to the total: {shown.map((c) => c.label).join(", ")}.
        </p>
      ) : null}
    </Panel>
  );
}

/* Confidence and caveats --------------------------------------------------- */

export function ConfidenceBadge({ confidence }: { confidence?: Confidence }) {
  if (!confidence) return null;
  return (
    <Chip tone={confidence === "high" ? "good" : confidence === "low" ? "bad" : "unknown"}>
      {confidence} confidence
    </Chip>
  );
}

/** Everything the student needs to know before acting on the answer. */
export function CaveatList({ caveats }: { caveats: string[] }) {
  const [open, setOpen] = useState(false);
  if (!caveats.length) return null;
  return (
    <section className="rounded-[18px] border border-[color:var(--line)] bg-[var(--hover)] px-4 py-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-3 text-left"
        aria-expanded={open}
      >
        <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
          Worth knowing ({caveats.length})
        </span>
        <DisclosureIcon open={open} />
      </button>
      {open ? (
        <ul className="mt-1.5 space-y-1.5">
          {caveats.map((caveat, i) => (
            <li key={i} className="break-words text-[12px] leading-6 text-[var(--muted)]">
              {caveat}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function DisclosureIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      className={`shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
      aria-hidden="true"
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}
