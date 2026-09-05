"use client";

import { Fragment, useEffect, useState } from "react";
import {
  api,
  type MappingRow,
  type UniversityBriefing,
  type UniversityCard,
} from "@/lib/api";

const SUSEP_COURSE_LOAD =
  "The total workload taken in the semester (including the workload in the host university) should not exceed the maximum semester academic load prescribed by the School if they were to spend their semester in NTU and not on Student Exchange Programme (SEP).";
const SUSEP_MINIMUM_CGPA = "3.5/5";

function mappingRank(m: MappingRow): number {
  const t = (m.ntu_module_type || "").toUpperCase().replace(/\s+/g, "");
  const code = (m.ntu_module_code || "").toUpperCase();
  if (t === "CORE" || t === "GER-CORE") return 0;
  if ((t.includes("MAJOR") && t.includes("PE")) || t === "2NDSPEC-PE" || t === "2ND-SPEC-PE") return 1;
  if (t === "BDE" || t === "UE" || code === "BDE") return 9;
  return 5;
}

function sortMappings(list: MappingRow[]): MappingRow[] {
  return [...list].sort((a, b) => {
    const d = mappingRank(a) - mappingRank(b);
    if (d) return d;
    return `${a.ntu_module_code}\0${a.host_module_code}`.localeCompare(
      `${b.ntu_module_code}\0${b.host_module_code}`,
    );
  });
}

function normaliseMappingLabel(value: string | null | undefined): string {
  return (value || "").trim().toUpperCase().replace(/\s+/g, " ");
}

function isExcludedMapping(mapping: MappingRow, excludedTypes: string[]): boolean {
  const type = normaliseMappingLabel(mapping.ntu_module_type);
  const code = normaliseMappingLabel(mapping.ntu_module_code);
  return excludedTypes.some((excluded) => {
    const target = normaliseMappingLabel(excluded);
    return (
      type === target ||
      type.startsWith(`${target} `) ||
      type.startsWith(`${target}-`) ||
      code === target ||
      code.startsWith(`${target} `) ||
      code.startsWith(`${target}-`)
    );
  });
}

function visibleMappings(list: MappingRow[], excludedTypes?: string[]): MappingRow[] {
  const excluded = excludedTypes || [];
  return sortMappings(list.filter((mapping) => !isExcludedMapping(mapping, excluded)));
}

function formatSgd(amount: number): string {
  return `S$${amount.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

type MappingGroup = {
  key: string;
  primary: MappingRow;
  alternatives: MappingRow[];
};

function groupMappings(list: MappingRow[]): MappingGroup[] {
  const groups = new Map<string, MappingGroup>();
  for (const mapping of list) {
    // The NTU module code is the stable identity here. A fallback keeps an
    // incomplete row from accidentally being merged with every other blank
    // code returned by a source.
    const code = normaliseMappingLabel(mapping.ntu_module_code);
    const key = code || `mapping-${mapping.mapping_id}`;
    const existing = groups.get(key);
    if (existing) {
      existing.alternatives.push(mapping);
    } else {
      groups.set(key, { key, primary: mapping, alternatives: [] });
    }
  }
  return [...groups.values()];
}

export default function UniCard({
  card,
  school,
  programmeType,
  term,
  moduleCodes,
  excludedModuleTypes,
  includedModuleTypes,
  restoredModuleTypes,
}: {
  card: UniversityCard;
  school: string;
  programmeType: string;
  term?: string | null;
  moduleCodes?: string[];
  excludedModuleTypes?: string[];
  includedModuleTypes?: string[];
  restoredModuleTypes?: string[];
}) {
  const [rows, setRows] = useState<MappingRow[]>(() =>
    visibleMappings(card.mappings_preview, excludedModuleTypes),
  );
  const [openId, setOpenId] = useState<number | null>(null);
  const [openGroupKey, setOpenGroupKey] = useState<string | null>(null);
  const [ntuQuery, setNtuQuery] = useState("");
  const [hostQuery, setHostQuery] = useState("");
  const [details, setDetails] = useState<Record<string, string> | null>(null);
  const [loading, setLoading] = useState(false);
  const [briefingOpen, setBriefingOpen] = useState(false);
  const [briefingLoading, setBriefingLoading] = useState(false);
  const [briefing, setBriefing] = useState<UniversityBriefing | null>(null);
  const [includeRent, setIncludeRent] = useState(true);
  const isSusep = programmeType.trim().toUpperCase() === "SUSEP";

  const previewSignature = card.mappings_preview.map((mapping) => mapping.mapping_id).join(",");
  const excludedSignature = (excludedModuleTypes || []).join(",");
  const moduleCodesSignature = (moduleCodes || []).join(",");
  const includedSignature = (includedModuleTypes || []).join(",");
  const restoredSignature = (restoredModuleTypes || []).join(",");
  useEffect(() => {
    // The same university can appear in consecutive answers with a different
    // module filter. Reset local state so rows from the previous answer
    // (including excluded BDEs) cannot survive the new response.
    const initialRows = visibleMappings(card.mappings_preview, excludedModuleTypes);
    setRows(initialRows);
    setOpenId(null);
    setOpenGroupKey(null);
    setNtuQuery("");
    setHostQuery("");
    setDetails(null);
    setBriefingOpen(false);
    setBriefing(null);
    setIncludeRent(true);
    setLoading(false);
    if (initialRows.length >= card.approved_count) return;

    // The chat response contains a small preview. Once this university is
    // opened, fetch all remaining pages automatically so there is no manual
    // "show more" step.
    const controller = new AbortController();
    let active = true;
    async function loadAllMappings() {
      setLoading(true);
      const allRows = [...initialRows];
      let offset = allRows.length;
      try {
        while (active && offset < card.approved_count) {
          const params = new URLSearchParams({
            school,
            programme_type: programmeType,
            offset: String(offset),
            limit: "50",
          });
          for (const value of moduleCodes || []) params.append("module_codes", value);
          for (const value of excludedModuleTypes || []) params.append("exclude_module_types", value);
          for (const value of includedModuleTypes || []) params.append("include_module_types", value);
          for (const value of restoredModuleTypes || []) params.append("restore_module_types", value);
          const data = await api<{ mappings: MappingRow[]; has_more: boolean }>(
            `/api/universities/${card.university_id}/mappings?${params.toString()}`,
            { signal: controller.signal },
          );
          if (!data.mappings.length) break;
          allRows.push(...data.mappings);
          offset += data.mappings.length;
          if (!data.has_more) break;
        }
        if (active) setRows(visibleMappings(allRows, excludedModuleTypes));
      } catch {
        if (active && !controller.signal.aborted) setRows(initialRows);
      } finally {
        if (active) setLoading(false);
      }
    }
    void loadAllMappings();
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    card.approved_count,
    card.mappings_preview,
    card.university_id,
    excludedSignature,
    includedSignature,
    restoredSignature,
    moduleCodesSignature,
    previewSignature,
    programmeType,
    school,
  ]);

  const normalisedNtuQuery = normaliseMappingLabel(ntuQuery);
  const normalisedHostQuery = normaliseMappingLabel(hostQuery);
  const filteredRows = rows.filter((mapping) => {
    const ntuText = `${mapping.ntu_module_code} ${mapping.ntu_module_title}`.toUpperCase();
    const hostText = `${mapping.host_module_code} ${mapping.host_module_title}`.toUpperCase();
    return (
      (!normalisedNtuQuery || ntuText.includes(normalisedNtuQuery)) &&
      (!normalisedHostQuery || hostText.includes(normalisedHostQuery))
    );
  });
  const groups = groupMappings(filteredRows);

  async function toggleDetails(id: number) {
    if (openId === id) {
      setOpenId(null);
      return;
    }
    setOpenId(id);
    setDetails(null);
    const data = await api<{ details: Record<string, string> }>(`/api/mappings/${id}/details`);
    setDetails(data.details);
  }

  async function knowMore() {
    const nextOpen = !briefingOpen;
    setBriefingOpen(nextOpen);
    if (!nextOpen || briefing || briefingLoading) return;
    setBriefingLoading(true);
    try {
      const data = await api<UniversityBriefing>("/api/research", {
        method: "POST",
        body: JSON.stringify({
          university_id: card.university_id,
          name: card.name,
          country: card.country,
          term: term || undefined,
          school,
          programme_type: programmeType,
        }),
      });
      setIncludeRent(data.cost_of_living?.rent_included ?? true);
      setBriefing(data);
    } catch {
      setBriefing({
        name: card.name,
        error: "Could not reach GEM Explorer just now.",
        module_conversions: [],
      });
    } finally {
      setBriefingLoading(false);
    }
  }

  const cost = briefing?.cost_of_living;
  const propertyItem = cost?.items?.find((item) =>
    item.label.trim().toLowerCase().startsWith("property"),
  );
  const hasRentToggle = Boolean(
    propertyItem && cost?.monthly_total_sgd != null && propertyItem.amount_sgd != null,
  );
  const sourceIncludesRent = cost?.rent_included ?? false;
  const displayedTotal =
    cost?.monthly_total_sgd != null && propertyItem?.amount_sgd != null && hasRentToggle
      ? sourceIncludesRent === includeRent
        ? cost.monthly_total_sgd
        : sourceIncludesRent
          ? cost.monthly_total_sgd - propertyItem.amount_sgd
          : cost.monthly_total_sgd + propertyItem.amount_sgd
      : cost?.monthly_total_sgd;
  const displayedSummary =
    displayedTotal != null
      ? `About ${formatSgd(displayedTotal)} per month${includeRent ? "" : " excluding rent"}${cost?.city ? ` in ${cost.city}` : ""}.`
      : cost?.summary;
  const visibleCostItems = cost?.items?.filter((item) => includeRent || item !== propertyItem) || [];

  return (
    <article className="glass overflow-hidden rounded-[22px]">
      <div className="flex items-start justify-between gap-3 px-4 py-4 sm:gap-4 sm:px-5">
        <div className="min-w-0">
          <h3 className="text-[16px] font-semibold text-[var(--ink)]">{card.name}</h3>
          <p className="mt-1 text-[12px] text-[var(--muted)]">
            {card.country} · {card.approved_count} approved mappings · {card.programme_type}
          </p>
        </div>
        <button
          type="button"
          onClick={knowMore}
          className="shrink-0 rounded-full bg-[var(--cta-bg)] px-3 py-1.5 text-[12px] font-medium text-[var(--cta-ink)] shadow-[0_0_14px_var(--send-glow)]"
        >
          {briefingOpen ? "Hide Details" : "More Details"}
        </button>
      </div>

      {briefingOpen && (
        <div className="mx-4 mb-4 space-y-3 rounded-2xl border border-[color:var(--line)] bg-[var(--hover)] p-4 text-[13px] leading-6 text-[var(--ink)] sm:mx-5">
          <div className="text-[12px] font-semibold uppercase tracking-wide text-[var(--link)]">
            University briefing
          </div>
          {briefingLoading && (
            <p className="text-[var(--muted)]">Reading this university&apos;s GEM Explorer brochure…</p>
          )}
          {/* A failed lookup must say only that. Rendering the "publishes no
              cost figures" and "does not publish a course load" copy alongside
              it asserts two facts about a brochure that was never read. */}
          {!briefingLoading && briefing?.error && (
            <p className="text-[13px] text-[var(--danger)]">{briefing.error}</p>
          )}
          {!briefingLoading && briefing && !briefing.error && (
            <>
              {briefing.gem_program && (
                <p className="text-[12px] text-[var(--muted)]">
                  GEM Explorer: {briefing.gem_program.replace(/^GEM Explorer:\s*/i, "")}
                  {briefing.term ? ` · ${briefing.term}` : ""}
                </p>
              )}
              {!isSusep && <section>
                <h4 className="text-[14px] font-semibold text-[var(--ink)]">
                  Cost of Living
                </h4>
                {displayedSummary ? (
                  <p className="mt-0.5">{displayedSummary}</p>
                ) : (
                  <p className="mt-0.5 text-[var(--muted)]">
                    {cost?.error || "No cost-of-living estimate is available yet."}
                  </p>
                )}
                {hasRentToggle ? (
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
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
                {!includeRent && hasRentToggle && (
                  <p className="mt-1 text-[12px] text-[var(--muted)]">Rent is excluded from this monthly figure.</p>
                )}
                {!!visibleCostItems.length && (
                  <div className="mt-2 overflow-x-auto rounded-xl border border-[color:var(--line)] bg-[var(--active)]">
                    <table className="w-full min-w-[260px] text-left text-[12px]">
                      <thead>
                        <tr className="text-[var(--faint)]">
                          <th className="px-3 py-2 font-medium">Monthly distribution</th>
                          <th className="px-3 py-2 text-right font-medium">SGD</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleCostItems.map((item) => (
                          <tr key={item.label} className="border-t border-[color:var(--line)]">
                            <td className="px-3 py-1.5">{item.label}</td>
                            <td className="px-3 py-1.5 text-right">{item.price}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {cost?.url && (
                  <a
                    href={cost.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  className="app-link mt-1 inline-block text-[12px]"
                  >
                    View cost-of-living source
                  </a>
                )}
              </section>}
              <section>
                <h4 className="text-[14px] font-semibold text-[var(--ink)]">Course Load</h4>
                {isSusep ? (
                  <p className="mt-0.5">{SUSEP_COURSE_LOAD}</p>
                ) : briefing.course_load_raw ? (
                  <p className="mt-0.5">{briefing.course_load_raw}</p>
                ) : (
                  <p className="mt-0.5 text-[var(--muted)]">
                    This university does not publish a course load in its GEM Explorer brochure.
                  </p>
                )}
                {briefing.planning_reference ? (
                  <p className="mt-2 rounded-xl border border-[color:var(--line)] bg-[var(--active)] px-3 py-2 text-[12px] leading-5 text-[var(--muted)]">
                    {briefing.planning_reference}
                  </p>
                ) : null}
                {!isSusep && (briefing.min_ects != null || briefing.max_ects != null) && (
                  <p className="mt-0.5 text-[12px] text-[var(--muted)]">
                    ECTS: {briefing.min_ects ?? "—"}
                    {briefing.max_ects != null && briefing.max_ects !== briefing.min_ects
                      ? `–${briefing.max_ects}`
                      : ""}
                  </p>
                )}
                {!isSusep && (briefing.min_au != null || briefing.max_au != null) ? (
                  <p className="mt-0.5 text-[12px] text-[var(--muted)]">
                    NTU AU: {briefing.min_au ?? "—"}
                    {briefing.max_au != null && briefing.max_au !== briefing.min_au
                      ? `–${briefing.max_au}`
                      : ""}
                  </p>
                ) : null}
                {!isSusep && briefing.au_note ? (
                  <p className="mt-1 text-[12px] text-[var(--muted)]">{briefing.au_note}</p>
                ) : null}
              </section>
              {(isSusep || briefing.gpa) && (
                <section>
                  <h4 className="text-[14px] font-semibold text-[var(--ink)]">Minimum CGPA Required</h4>
                  <p className="mt-0.5">{isSusep ? SUSEP_MINIMUM_CGPA : briefing.gpa}</p>
                </section>
              )}
              {briefing.brochure_url && (
                <a
                  href={briefing.brochure_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="app-link inline-block text-[12px]"
                >
                  Open GEM Explorer page
                </a>
              )}
            </>
          )}
        </div>
      )}

      <div className="px-4 pb-4 sm:px-5">
        <div className="mapping-table-container overflow-x-auto">
          <div className="mapping-mobile-filters mb-3 grid gap-2">
            <label className="text-[11px] font-semibold uppercase tracking-wide text-[var(--faint)]">
              NTU module
              <input
                value={ntuQuery}
                onChange={(event) => setNtuQuery(event.target.value)}
                placeholder="Code or name"
                aria-label="Search NTU modules by code or name"
                className="mt-1 h-9 w-full rounded-lg border border-[color:var(--line)] bg-[var(--hover)] px-2.5 text-[12px] font-normal normal-case tracking-normal text-[var(--ink)] outline-none placeholder:text-[var(--faint)] focus:border-[color:var(--link)]"
              />
            </label>
            <label className="text-[11px] font-semibold uppercase tracking-wide text-[var(--faint)]">
              Host module
              <input
                value={hostQuery}
                onChange={(event) => setHostQuery(event.target.value)}
                placeholder="Code or name"
                aria-label="Search host modules by code or name"
                className="mt-1 h-9 w-full rounded-lg border border-[color:var(--line)] bg-[var(--hover)] px-2.5 text-[12px] font-normal normal-case tracking-normal text-[var(--ink)] outline-none placeholder:text-[var(--faint)] focus:border-[color:var(--link)]"
              />
            </label>
          </div>
          <table className="mapping-table w-full min-w-0 table-fixed border-separate border-spacing-y-1 text-left text-[12px]">
            <colgroup>
              <col className="w-[34%]" />
              <col className="w-[42%]" />
              <col className="w-[54px]" />
              <col className="w-[76px]" />
            </colgroup>
            <thead>
              <tr className="text-[var(--muted)]">
                <th className="min-w-[190px] pb-2 pl-3 pr-3 align-top font-medium">
                  <label className="block">
                    <span>NTU module</span>
                    <input
                      value={ntuQuery}
                      onChange={(event) => setNtuQuery(event.target.value)}
                      placeholder="Search code or name"
                      aria-label="Search NTU modules by code or name"
                      className="mt-2 h-8 w-full rounded-lg border border-[color:var(--line)] bg-[var(--hover)] px-2.5 text-[11px] font-normal text-[var(--ink)] outline-none placeholder:text-[var(--faint)] focus:border-[color:var(--link)]"
                    />
                  </label>
                </th>
                <th className="min-w-[190px] pb-2 pr-3 align-top font-medium">
                  <label className="block">
                    <span>Host module</span>
                    <input
                      value={hostQuery}
                      onChange={(event) => setHostQuery(event.target.value)}
                      placeholder="Search code or name"
                      aria-label="Search host modules by code or name"
                      className="mt-2 h-8 w-full rounded-lg border border-[color:var(--line)] bg-[var(--hover)] px-2.5 text-[11px] font-normal text-[var(--ink)] outline-none placeholder:text-[var(--faint)] focus:border-[color:var(--link)]"
                    />
                  </label>
                </th>
                <th className="mapping-au-heading pb-2 pl-3 pr-3 font-medium">AU</th>
                <th className="mapping-details-heading pr-3" />
              </tr>
            </thead>
            <tbody>
              {groups.map((group, i) => {
                const m = group.primary;
                const stripe = i % 2 === 0 ? "bg-[var(--row-alt)]" : "bg-[var(--row)]";
                return (
                <Fragment key={group.key}>
                  <tr className="mapping-primary-row align-top">
                    <td className={`mapping-ntu-cell rounded-l-xl py-2.5 pl-3 pr-3 ${stripe}`}>
                      <div className="mapping-compact-label">NTU module</div>
                      <div className="font-medium text-[var(--ink)]">{m.ntu_module_code}</div>
                      {m.ntu_module_type && m.ntu_module_type.toUpperCase() !== m.ntu_module_code.toUpperCase() ? (
                        <div className="text-[11px] text-[var(--faint)]">{m.ntu_module_type}</div>
                      ) : null}
                      <div className="text-[var(--muted)]">{m.ntu_module_title}</div>
                    </td>
                    <td className={`mapping-host-cell py-2.5 pr-3 ${stripe}`}>
                      <div className="mapping-compact-label">Host module</div>
                      <div className="font-medium text-[var(--ink)]">{m.host_module_code}</div>
                      <div className="text-[var(--muted)]">{m.host_module_title}</div>
                      {group.alternatives.length > 0 ? (
                        <button
                          type="button"
                          onClick={() => setOpenGroupKey((key) => (key === group.key ? null : group.key))}
                          className="mt-1 app-link text-left text-[11px]"
                          aria-expanded={openGroupKey === group.key}
                        >
                          {openGroupKey === group.key ? "Hide" : "Show"} {group.alternatives.length} other host module{group.alternatives.length === 1 ? "" : "s"}
                        </button>
                      ) : null}
                    </td>
                    <td className={`mapping-au-cell align-middle py-2.5 pl-3 pr-3 text-[var(--ink)] ${stripe}`}>
                      <span className="mapping-au-content">
                        <span className="mapping-au-label">AU</span>
                        <span className="mapping-au-value">{m.credits || "—"}</span>
                      </span>
                    </td>
                    <td className={`mapping-actions-cell align-middle rounded-r-xl py-2.5 pr-3 text-right ${stripe}`}>
                      <button
                        type="button"
                        className="app-link text-[12px]"
                        onClick={() => toggleDetails(m.mapping_id)}
                      >
                        {openId === m.mapping_id ? "Hide" : "Details"}
                      </button>
                    </td>
                  </tr>
                  {openGroupKey === group.key && group.alternatives.length > 0 && (
                    <tr className="mapping-group-row">
                      <td colSpan={4} className={`mapping-group-cell rounded-xl px-3 pb-3 pt-1 ${stripe}`}>
                        <div className="space-y-2 rounded-2xl border border-[color:var(--line)] bg-[var(--hover)] p-3">
                          <div className="text-[11px] font-semibold uppercase tracking-wide text-[var(--link)]">
                            Other host modules for {m.ntu_module_code}
                          </div>
                          <div className="overflow-x-auto">
                            <div className="space-y-1.5">
                              {group.alternatives.map((alternative) => (
                                <div key={alternative.mapping_id}>
                                  <div className="mapping-alternative-row grid grid-cols-[minmax(0,1fr)_auto_auto] items-start gap-3 rounded-xl border border-[color:var(--line)] bg-[var(--row)] px-3 py-2">
                                    <div className="min-w-0">
                                      <div className="font-medium text-[var(--ink)]">{alternative.host_module_code}</div>
                                      <div className="break-words text-[var(--muted)]">{alternative.host_module_title}</div>
                                    </div>
                                    <div className="whitespace-nowrap pt-0.5 text-[var(--ink)]">
                                      <span className="mapping-alternative-au-label">AU</span>
                                      {" "}{alternative.credits || "—"}
                                    </div>
                                    <button
                                      type="button"
                                      className="app-link pt-0.5 text-[12px]"
                                      onClick={() => {
                                        setOpenGroupKey(group.key);
                                        toggleDetails(alternative.mapping_id);
                                      }}
                                    >
                                      {openId === alternative.mapping_id ? "Hide" : "Details"}
                                    </button>
                                  </div>
                                  {openId === alternative.mapping_id && (
                                    <dl className="ml-2 mr-2 min-w-0 rounded-b-2xl border-x border-b border-[color:var(--line)] bg-[var(--hover)] p-3 text-[12px] text-[var(--ink)]">
                                      {!details ? (
                                        <p className="text-[var(--muted)]">Loading details…</p>
                                      ) : (
                                        Object.entries(details).map(([k, v]) =>
                                          v ? (
                                            <div key={k} className="mb-2 min-w-0 last:mb-0">
                                              <dt className="font-semibold text-[var(--ink)]">{k}</dt>
                                              <dd className="mt-0.5 min-w-0 whitespace-pre-wrap break-all [overflow-wrap:anywhere]">
                                                {v}
                                              </dd>
                                            </div>
                                          ) : null,
                                        )
                                      )}
                                    </dl>
                                  )}
                                </div>
                              ))}
                            </div>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  {openId === m.mapping_id && (
                    <tr className="mapping-detail-row">
                      <td colSpan={4} className={`rounded-xl px-3 pb-3 pt-1 ${stripe}`}>
                        <dl className="min-w-0 max-w-full space-y-2 overflow-hidden rounded-2xl border border-[color:var(--line)] bg-[var(--hover)] p-4 text-[12px] text-[var(--ink)]">
                          {!details ? (
                            <p className="text-[var(--muted)]">Loading details…</p>
                          ) : (
                            Object.entries(details).map(([k, v]) =>
                              v ? (
                                <div key={k} className="min-w-0">
                                  <dt className="font-semibold text-[var(--ink)]">{k}</dt>
                                  <dd className="mt-0.5 min-w-0 whitespace-pre-wrap break-all [overflow-wrap:anywhere]">
                                    {v}
                                  </dd>
                                </div>
                              ) : null,
                            )
                          )}
                        </dl>
                      </td>
                    </tr>
                  )}
                </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        {loading ? (
          <p className="mt-3 text-[12px] text-[var(--muted)]">Loading all mapped modules…</p>
        ) : null}
        {!loading && rows.length > 0 && groups.length === 0 ? (
          <p className="mt-3 rounded-xl border border-[color:var(--line)] bg-[var(--hover)] px-3 py-2 text-[12px] text-[var(--muted)]">
            No mapped modules match these searches.
          </p>
        ) : null}
      </div>
    </article>
  );
}
