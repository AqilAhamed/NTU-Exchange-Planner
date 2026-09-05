"use client";

import { useState } from "react";

type Topic = {
  id: string;
  label: string;
  kicker: string;
  title: string;
  body: string;
  helps: string[];
  source: string;
};

const TOPICS: Topic[] = [
  {
    id: "start",
    label: "Start here",
    kicker: "Intake",
    title: "Two facts, then a conversation",
    body: "To shortlist partners, say your NTU degree programme and whether you want Semester 1 or Semester 2. GEM Explorer is the overseas semester; SUSEP is the Singapore one. You cannot do both in the same term. Country, modules, GPA and budget are optional — add them now or in a follow-up.",
    helps: [
      "You do not fill a form. Type the way you would ask a senior.",
      "Follow-ups keep your programme, semester, filters and the university you last opened.",
      "Use New Chat when you want a clean profile.",
    ],
    source: "Your programme is matched against NTU degrees in Coursefinder, not invented.",
  },
  {
    id: "partners",
    label: "Find partners",
    kicker: "Approved mappings",
    title: "Shortlist universities that already map your degree",
    body: "The planner searches NTU Coursefinder for partner universities with approved host ↔ NTU module mappings for your programme. Results appear as compact cards. Open one to see the mapping table, with NTU modules grouped and searchable on both sides.",
    helps: [
      "Ask for a region or country, specific module codes, a GPA cap, or a monthly budget.",
      "Filter by any NTU module, then add, remove, or correct module choices later without losing your other filters.",
      "Earlier cards stay in the chat when you ask the next question.",
    ],
    source: "Read-only approved Coursefinder records. The count on a card matches what paging actually returns.",
  },
  {
    id: "workload",
    label: "Course load",
    kicker: "Host rules",
    title: "See how much you must take, in the host’s own units",
    body: "Each GEM Explorer brochure states course load differently — ECTS, credits, course counts, even a legal minimum. The planner reports that university’s own wording. It converts to AU or ECTS only when that brochure itself states an equivalence.",
    helps: [
      "Ask about one university after you have a shortlist, or name it directly.",
      "Published CGPA or entry bars are shown when the host writes them down.",
      "If a university publishes no readable load, you get that gap — not a guessed AU total.",
    ],
    source: "GEM Explorer programme brochures. No default “2 ECTS = 1 AU” rule is applied.",
  },
  {
    id: "cost",
    label: "Living costs",
    kicker: "Monthly budget",
    title: "Estimate a month abroad in Singapore dollars",
    body: "Ask what a host costs per month, or set a budget cap while shortlisting. The planner resolves the host city, then uses published living-cost figures in SGD, with the city and method named. If it cannot place the university, it refuses rather than costing a guessed city.",
    helps: [
      "Useful when you are comparing a few shortlisted partners, not shopping the whole world.",
      "Rent is called out when the estimate excludes it.",
      "A live exchange rate, when used, is shown with its date.",
    ],
    source: "Published living-cost figures for the resolved host city, labelled as an estimate.",
  },
  {
    id: "ntu",
    label: "NTU process",
    kicker: "Student guidance",
    title: "Applications, aid, nominations and credit transfer",
    body: "Questions about how GEM Explorer or SUSEP works for an NTU student are answered from the project’s NTU student reference collection: eligibility, applications, financial aid and funding, nominations, withdrawal, tuition and credit transfer. You do not need to repeat the programme name every turn.",
    helps: [
      "This is for NTU process, not for inventing host-university facts.",
      "Answers include numbered citations you can open to the indexed passage.",
      "If the reference set does not cover the point, you are pointed to OGEM rather than given a guess.",
    ],
    source: "Indexed NTU-student GEM Explorer and SUSEP reference documents.",
  },
];

export default function HomeHero() {
  const [activeId, setActiveId] = useState(TOPICS[0].id);
  const topic = TOPICS.find((item) => item.id === activeId) ?? TOPICS[0];
  const activeIndex = TOPICS.findIndex((item) => item.id === activeId);

  function move(delta: number) {
    const next = (activeIndex + delta + TOPICS.length) % TOPICS.length;
    setActiveId(TOPICS[next].id);
  }

  return (
    <div className="mx-auto flex min-h-full w-full max-w-[960px] flex-col justify-center px-4 pb-28 pt-4 sm:px-8 sm:pt-8">
      <div className="text-center">
        <h1 className="text-[28px] font-semibold tracking-[-0.03em] text-[var(--ink)] sm:text-[38px]">
          Your one-stop{" "}
          <span className="text-[var(--link)]">Agentic AI Tool</span>{" "}
          to plan your exchange
        </h1>
        <p className="mx-auto mt-3 max-w-[640px] text-[14px] leading-6 text-[var(--muted)] sm:text-[15px]">
          An NTU exchange planner for GEM Explorer and SUSEP. Start with your degree and
          semester, then ask about approved mappings, host course load, living
          costs, or NTU student process — in one conversation.
        </p>
      </div>

      <section
        className="glass mt-8 overflow-hidden rounded-[24px] sm:mt-10"
        aria-label="Guide to the planner"
      >
        <div className="flex flex-col lg:flex-row">
          <div
            role="tablist"
            aria-label="What this planner can do"
            aria-orientation="horizontal"
            className="flex gap-2 overflow-x-auto border-b border-[color:var(--line)] p-3 lg:w-[232px] lg:flex-col lg:overflow-visible lg:border-b-0 lg:border-r lg:p-4"
            onKeyDown={(event) => {
              if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                event.preventDefault();
                move(1);
              } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                event.preventDefault();
                move(-1);
              } else if (event.key === "Home") {
                event.preventDefault();
                setActiveId(TOPICS[0].id);
              } else if (event.key === "End") {
                event.preventDefault();
                setActiveId(TOPICS[TOPICS.length - 1].id);
              }
            }}
          >
            {TOPICS.map((item, index) => {
              const selected = item.id === topic.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  role="tab"
                  id={`guide-tab-${item.id}`}
                  aria-selected={selected}
                  aria-controls={`guide-panel-${item.id}`}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => setActiveId(item.id)}
                  className={`flex min-w-max items-center gap-2 rounded-[16px] px-3 py-2.5 text-left text-[13px] font-semibold transition lg:w-full ${
                    selected
                      ? "bg-[var(--active)] text-[var(--ink)]"
                      : "text-[var(--muted)] hover:bg-[var(--hover)] hover:text-[var(--ink)]"
                  }`}
                >
                  <span
                    className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] ${
                      selected
                        ? "bg-[var(--cta-bg)] text-[var(--cta-ink)]"
                        : "bg-[var(--hover)]"
                    }`}
                  >
                    {index + 1}
                  </span>
                  {item.label}
                </button>
              );
            })}
          </div>

          <div
            role="tabpanel"
            id={`guide-panel-${topic.id}`}
            aria-labelledby={`guide-tab-${topic.id}`}
            className="min-w-0 flex-1 p-5 sm:p-7"
          >
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[var(--link)]">
              {topic.kicker}
            </p>
            <h2 className="mt-2 text-[20px] font-semibold tracking-[-0.02em] text-[var(--ink)] sm:text-[22px]">
              {topic.title}
            </h2>
            <p className="mt-3 text-[14px] leading-6 text-[var(--muted)]">{topic.body}</p>
            <ul className="mt-4 space-y-2">
              {topic.helps.map((line) => (
                <li key={line} className="flex gap-2.5 text-[13px] leading-5 text-[var(--ink)]">
                  <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--link)]" />
                  <span>{line}</span>
                </li>
              ))}
            </ul>
            <p className="mt-4 text-[12px] leading-5 text-[var(--faint)]">{topic.source}</p>
          </div>
        </div>
      </section>

      <p className="mt-4 text-center text-[12px] leading-5 text-[var(--faint)]">
        Browse the guide, or type below. Every number comes from a named source;
        where it cannot verify something, it says so.
      </p>
    </div>
  );
}
