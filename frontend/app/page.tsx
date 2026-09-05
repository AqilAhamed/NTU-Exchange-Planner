"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Composer from "./components/Composer";
import HomeHero from "./components/HomeHero";
import Sidebar from "./components/Sidebar";
import ThemeToggle from "./components/ThemeToggle";
import UniCard from "./components/UniversityCard";
import Markdown from "./components/Markdown";
import {
  BudgetPanel,
  CaveatList,
  ConfidenceBadge,
  SourceList,
  TaskProgress,
  WorkloadPanel,
} from "./components/Evidence";
import {
  api,
  streamChat,
  type ChatMessage,
  type ChatSummary,
  type Payload,
  type PlannedTask,
  type Profile,
  type UniversitiesPage,
  type UniversityCard,
} from "@/lib/api";

type View = "home" | "chat";

// External brochure/search lookups can take a while, but a browser request
// must never leave the composer permanently disabled.
const TURN_TIMEOUT_MS = 125_000;
const MIN_DRAWER_WIDTH = 360;
const MAX_DRAWER_WIDTH = 960;
const MIN_CHAT_WIDTH = 320;

function drawerWidthBounds(sidebarCollapsed = false): { min: number; max: number } {
  if (typeof window === "undefined") return { min: MIN_DRAWER_WIDTH, max: MAX_DRAWER_WIDTH };
  const sidebarWidth = sidebarCollapsed ? 72 : 280;
  const maxForChat = window.innerWidth - sidebarWidth - MIN_CHAT_WIDTH;
  return {
    min: Math.min(MIN_DRAWER_WIDTH, Math.max(280, window.innerWidth - sidebarWidth - 120)),
    max: Math.min(MAX_DRAWER_WIDTH, Math.max(360, maxForChat)),
  };
}

function defaultDrawerWidth(): number {
  if (typeof window === "undefined") return 560;
  const target = Math.round(window.innerWidth * 0.48);
  const bounds = drawerWidthBounds();
  return Math.min(bounds.max, Math.max(bounds.min, target));
}

function clampDrawerWidth(width: number, sidebarCollapsed = false): number {
  const { min, max } = drawerWidthBounds(sidebarCollapsed);
  return Math.min(max, Math.max(min, width));
}

export default function Page() {
  const [view, setView] = useState<View>("home");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [extraCards, setExtraCards] = useState<UniversityCard[]>([]);
  // What a paged fetch could not cover, e.g. a CGPA check bounded to the top
  // candidates. Shown beside the cards rather than dropped.
  const [cardNotes, setCardNotes] = useState<string[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [selectedUniversityId, setSelectedUniversityId] = useState<number | null>(null);
  const [selectedUniversity, setSelectedUniversity] = useState<UniversityCard | null>(null);
  const [selectedUniversityProfile, setSelectedUniversityProfile] = useState<Profile | null>(null);
  const [drawerWidth, setDrawerWidth] = useState(defaultDrawerWidth);
  const [resizingDrawer, setResizingDrawer] = useState(false);
  const [liveTasks, setLiveTasks] = useState<PlannedTask[]>([]);
  const scroller = useRef<HTMLDivElement>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const pendingCancellation = useRef<Promise<void> | null>(null);
  const resizeStart = useRef<{ clientX: number; width: number } | null>(null);

  function clearSelectedUniversity() {
    setSelectedUniversityId(null);
    setSelectedUniversity(null);
    setSelectedUniversityProfile(null);
  }

  const lastPayload: Payload | undefined = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].payload) return messages[i].payload;
    }
    return undefined;
  }, [messages]);

  async function refreshChats(signal?: AbortSignal) {
    const data = await api<{ chats: ChatSummary[] }>("/api/chats", { signal });
    setChats(data.chats);
  }

  useEffect(() => {
    refreshChats().catch(() => {});
  }, []);

  useEffect(() => {
    return () => activeRequest.current?.abort();
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const latestPayloadIndex = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].payload) return i;
    }
    return -1;
  }, [messages]);

  useEffect(() => {
    if (selectedUniversityId == null) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") clearSelectedUniversity();
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selectedUniversityId]);

  useEffect(() => {
    function keepDrawerInBounds() {
      setDrawerWidth((width) => clampDrawerWidth(width, sidebarCollapsed));
    }
    window.addEventListener("resize", keepDrawerInBounds);
    return () => window.removeEventListener("resize", keepDrawerInBounds);
  }, [sidebarCollapsed]);

  useEffect(() => {
    if (!resizingDrawer) return;

    function resize(event: PointerEvent) {
      const start = resizeStart.current;
      if (!start) return;
      // The divider moves left when the drawer grows and right when it shrinks.
      setDrawerWidth(clampDrawerWidth(start.width + start.clientX - event.clientX, sidebarCollapsed));
    }
    function finishResize() {
      resizeStart.current = null;
      setResizingDrawer(false);
    }

    window.addEventListener("pointermove", resize);
    window.addEventListener("pointerup", finishResize);
    window.addEventListener("pointercancel", finishResize);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    return () => {
      window.removeEventListener("pointermove", resize);
      window.removeEventListener("pointerup", finishResize);
      window.removeEventListener("pointercancel", finishResize);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [resizingDrawer, sidebarCollapsed]);

  function closeMenu() {
    setMenuOpen(false);
  }

  function cancelPendingRequest() {
    const controller = activeRequest.current;
    const activeSessionId = sessionId;
    controller?.abort();
    activeRequest.current = null;
    setLoading(false);
    setLiveTasks([]);

    // Aborting fetch stops the browser from reading the stream, but it does
    // not guarantee that the server has released its in-flight turn marker
    // before the next message is submitted. Tell the server to cancel only
    // this turn; the chat and its saved history must remain intact.
    if (controller && activeSessionId) {
      const cancellation = api(`/api/chats/${encodeURIComponent(activeSessionId)}/cancel`, {
        method: "POST",
      })
        .then(() => undefined)
        .catch(() => undefined);
      pendingCancellation.current = cancellation;
      void cancellation.finally(() => {
        if (pendingCancellation.current === cancellation) {
          pendingCancellation.current = null;
        }
      });
    }
  }

  async function send(text: string, existingSession?: string | null) {
    const trimmed = text.trim();
    if (!trimmed || loading) return;

    // A stop followed immediately by a new message uses the same session.
    // Wait for the cancellation endpoint to finish releasing the previous
    // stream so the new turn is not mistaken for a deleted chat.
    await pendingCancellation.current;

    setInput("");
    setView("chat");
    setExtraCards([]);
    setCardNotes([]);
    clearSelectedUniversity();
    setMenuOpen(false);
    setMessages((m) => [...m, { role: "user", content: trimmed }]);
    setLiveTasks([]);
    setLoading(true);

    const body = { message: trimmed, session_id: existingSession ?? sessionId };
    const controller = new AbortController();
    activeRequest.current?.abort();
    activeRequest.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), TURN_TIMEOUT_MS);
    const ownsRequest = () => activeRequest.current === controller;
    let delivered = false;

    try {
      await streamChat(body, (event) => {
        if (!ownsRequest()) return;
        if (event.type === "open") {
          setSessionId(event.session_id);
        } else if (event.type === "plan") {
          setLiveTasks(event.planned_tasks);
        } else if (event.type === "task") {
          // Match on kind: the router emits each lane once, so a status update
          // replaces that lane's row rather than appending a duplicate.
          setLiveTasks((tasks) =>
            tasks.map((task) =>
              task.kind === event.kind
                ? { ...task, status: event.status, description: event.description || task.description }
                : task,
            ),
          );
        } else if (event.type === "done") {
          delivered = true;
          setSessionId(event.payload.session_id);
          applyMessages(event.payload, trimmed);
        } else if (event.type === "error") {
          throw new Error(event.message);
        }
      });

      // A stream that closed without a `done` frame has not answered.
      if (!delivered) {
        const data = await api<Payload & { session_id: string; messages: ChatMessage[] }>(
          "/api/chat",
          { method: "POST", body: JSON.stringify(body), signal: controller.signal },
        );
        if (!ownsRequest()) return;
        setSessionId(data.session_id);
        applyMessages(data, trimmed);
      }
      await refreshChats(controller.signal).catch(() => {});
    } catch (err) {
      if (!ownsRequest()) return;
      const message = controller.signal.aborted
        ? "This request took too long to finish. The chat is ready for you to try again."
        : err instanceof Error
          ? err.message
          : "The planner could not answer just then.";
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: message,
        },
      ]);
    } finally {
      window.clearTimeout(timeout);
      if (ownsRequest()) {
        activeRequest.current = null;
        setLoading(false);
        setLiveTasks([]);
      }
    }
  }

  function applyMessages(
    data: Payload & { session_id: string; messages: ChatMessage[] },
    sent: string,
  ) {
    // `data.messages || [fallback]` does not fire on an empty array, so a turn
    // that legitimately returned no history wiped the conversation instead of
    // falling back. Check the length.
    const history = data.messages;
    if (Array.isArray(history) && history.length > 0) {
      setMessages(history);
      return;
    }
    setMessages((m) => [
      ...m.filter((entry, i) => !(i === m.length - 1 && entry.role === "user" && entry.content === sent)),
      { role: "user", content: sent },
      {
        role: "assistant",
        content: data.narration || data.answer?.summary || data.clarification || "",
        payload: data,
      },
    ]);
  }

  async function onNew() {
    cancelPendingRequest();
    try {
      const created = await api<{ session_id: string }>("/api/chats", { method: "POST" });
      setSessionId(created.session_id);
      setMessages([]);
      setExtraCards([]);
    setCardNotes([]);
      clearSelectedUniversity();
      setView("home");
      closeMenu();
      await refreshChats();
    } catch {
      // A failed create leaves the current chat alone rather than blanking it.
    }
  }

  async function onSelect(id: string) {
    cancelPendingRequest();
    try {
      const chat = await api<{ session_id: string; messages: ChatMessage[] }>(`/api/chats/${id}`);
      setSessionId(id);
      setMessages(chat.messages || []);
      setExtraCards([]);
    setCardNotes([]);
      clearSelectedUniversity();
      setView(chat.messages?.length ? "chat" : "home");
      closeMenu();
    } catch {
      await refreshChats().catch(() => {});
    }
  }

  async function onDelete(id: string) {
    if (sessionId === id) cancelPendingRequest();
    try {
      await api(`/api/chats/${id}`, { method: "DELETE" });
      if (sessionId === id) {
        setSessionId(null);
        setMessages([]);
        clearSelectedUniversity();
        setView("home");
      }
    } finally {
      await refreshChats().catch(() => {});
    }
  }

  async function showMoreUnis() {
    const p = lastPayload?.profile;
    if (!p) return;
    const offset = (lastPayload?.cards?.length || 0) + extraCards.length;
    const country = p.destination_pref
      ? `&country=${encodeURIComponent(p.destination_pref)}`
      : "";
    const countries = (p.destination_countries || [])
      .map((value) => `&countries=${encodeURIComponent(value)}`)
      .join("");
    const moduleCodes = (p.module_codes || [])
      .map((value) => `&module_codes=${encodeURIComponent(value)}`)
      .join("");
    const excludedTypes = (p.excluded_module_types || [])
      .map((value) => `&exclude_module_types=${encodeURIComponent(value)}`)
      .join("");
    const includedTypes = (p.included_module_types || [])
      .map((value) => `&include_module_types=${encodeURIComponent(value)}`)
      .join("");
    const restoredTypes = (p.restored_module_types || [])
      .map((value) => `&restore_module_types=${encodeURIComponent(value)}`)
      .join("");
    const cgpa = p.cgpa != null
      ? `&cgpa=${encodeURIComponent(String(p.cgpa))}`
      : "";
    try {
      const data = await api<UniversitiesPage>(
        `/api/universities?school=${encodeURIComponent(p.school_code)}&programme_type=${encodeURIComponent(p.programme_type)}&offset=${offset}${country}${countries}${moduleCodes}${excludedTypes}${includedTypes}${restoredTypes}${cgpa}`,
      );
      setExtraCards((c) => [...c, ...data.cards]);
      setCardNotes(data.notes || []);
    } catch {
      // Keep the cards already on screen.
    }
  }

  const profile = lastPayload?.profile;

  return (
    <div className="relative flex h-dvh overflow-hidden bg-[var(--bg)]">
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="glow-orb -left-24 -top-16 h-72 w-72 bg-[var(--orb-1)] opacity-45" />
        <div className="glow-orb -right-10 top-24 h-64 w-64 bg-[var(--orb-2)] opacity-30" />
        <div className="glow-orb bottom-[-5rem] left-1/3 h-56 w-56 bg-[var(--orb-3)] opacity-40" />
      </div>

      {menuOpen && (
        <button
          type="button"
          aria-label="Close menu overlay"
          className="fixed inset-0 z-40 md:hidden"
          style={{ background: "var(--overlay)" }}
          onClick={closeMenu}
        />
      )}

      <Sidebar
        chats={chats}
        activeId={view === "home" ? null : sessionId}
        search={search}
        homeActive={view === "home"}
        open={menuOpen}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed((value) => !value)}
        onSearch={setSearch}
        onHome={() => {
          cancelPendingRequest();
          setView("home");
          closeMenu();
        }}
        onNew={onNew}
        onSelect={onSelect}
        onDelete={onDelete}
        onClose={closeMenu}
      />

      <main className={`relative z-10 flex min-w-0 flex-1 flex-col ${resizingDrawer ? "select-none" : ""}`}>
        <header className="relative z-10 flex items-center gap-3 px-4 py-4 sm:px-8 sm:py-5">
          <button
            type="button"
            className="glass flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-[var(--ink)] md:hidden"
            onClick={() => setMenuOpen(true)}
            aria-label="Open menu"
          >
            <MenuIcon />
          </button>
          <ThemeToggle />
          <div className="min-w-0 flex-1 truncate text-[18px] font-semibold tracking-tight text-[var(--ink)] sm:text-[22px]">
            NTU Exchange Planner
          </div>
        </header>

        <div ref={scroller} className="relative z-10 flex-1 overflow-y-auto">
          {view === "home" ? (
            <HomeHero />
          ) : (
            <div className="mx-auto w-full max-w-[820px] space-y-5 px-4 pb-32 pt-2 sm:px-6 sm:pt-4">
              {messages.map((m, i) => {
                const messageCards = [
                  ...(m.payload?.cards || []),
                  ...(i === latestPayloadIndex ? extraCards : []),
                ];
                const messageProfile = m.payload?.profile;
                return (
                  <div
                    key={i}
                    className={m.role === "user" ? "flex min-w-0 justify-end" : "min-w-0"}
                  >
                    {m.role === "user" ? (
                      <div className="max-w-[88%] rounded-2xl bg-[var(--user-bg)] px-4 py-3 text-[14px] text-[var(--user-ink)] sm:max-w-[80%]">
                        {m.content}
                      </div>
                    ) : (
                      <div className="min-w-0 max-w-full space-y-4 break-words">
                        {m.payload?.confidence ? (
                          <ConfidenceBadge confidence={m.payload.confidence} />
                        ) : null}
                        <Markdown
                          text={m.payload?.answer?.summary || m.content}
                          sources={m.payload?.sources || []}
                        />
                        {m.payload?.answer?.body ? (
                          <Markdown text={m.payload.answer.body} sources={m.payload?.sources || []} />
                        ) : null}
                        <WorkloadPanel evidence={m.payload?.workload_evidence || []} />
                        {m.payload?.budgets?.length ? (
                          <div className="space-y-4">
                            {m.payload.budgets.map((budget, budgetIndex) => (
                              <BudgetPanel key={`${budget.university_name || "budget"}-${budgetIndex}`} budget={budget} />
                            ))}
                          </div>
                        ) : (
                          <BudgetPanel budget={m.payload?.budget} />
                        )}
                        <CaveatList caveats={m.payload?.caveats || []} />
                        <SourceList sources={m.payload?.sources || []} />
                        {m.payload?.planned_tasks?.length ? (
                          <TaskProgress tasks={m.payload.planned_tasks} />
                        ) : null}
                        {(m.payload?.answer?.recommendations || []).length > 0 ? (
                        <Markdown
                          text={(m.payload!.answer!.recommendations || [])
                            .map((r) => `- ${r}`)
                            .join("\n")}
                          sources={m.payload?.sources || []}
                        />
                        ) : null}
                        {messageCards.length > 0 && messageProfile ? (
                          <div className="space-y-3">
                            {messageCards.map((c) => (
                              <button
                                key={c.university_id}
                                type="button"
                                onClick={() => {
                                  setSelectedUniversityId(c.university_id);
                                  setSelectedUniversity(c);
                                  setSelectedUniversityProfile(messageProfile || null);
                                  closeMenu();
                                }}
                                className="glass group flex w-full items-center gap-4 rounded-[20px] px-4 py-4 text-left transition hover:-translate-y-0.5 hover:border-[color:var(--accent)] sm:px-5"
                                aria-label={`Open details for ${c.name}`}
                              >
                                <span className="min-w-0 flex-1">
                                  <span className="block truncate text-[15px] font-semibold text-[var(--ink)]">
                                    {c.name}
                                  </span>
                                </span>
                                <span className="shrink-0 text-[var(--muted)] transition group-hover:translate-x-0.5 group-hover:text-[var(--ink)]">
                                  <ChevronRightIcon />
                                </span>
                              </button>
                            ))}
                            {i === latestPayloadIndex &&
                              (lastPayload?.has_more || extraCards.length > 0) &&
                              (lastPayload?.total_universities || 0) > messageCards.length && (
                                <button
                                  type="button"
                                  onClick={showMoreUnis}
                                  className="glass rounded-full px-4 py-2 text-[13px] font-medium text-[var(--ink)]"
                                >
                                  Show more universities
                                </button>
                              )}
                            {i === latestPayloadIndex &&
                              cardNotes.map((note) => (
                                <p
                                  key={note}
                                  className="w-full text-[12px] leading-relaxed text-[var(--muted)]"
                                >
                                  ⚠️ {note}
                                </p>
                              ))}
                          </div>
                        ) : null}
                      </div>
                    )}
                  </div>
                );
              })}

              {loading ? (
                liveTasks.length ? (
                  <TaskProgress tasks={liveTasks} live />
                ) : (
                  <div className="text-[13px] text-[var(--muted)]">Planning…</div>
                )
              ) : null}
            </div>
          )}
        </div>

        <div className="relative z-10 px-3 pb-[max(1rem,env(safe-area-inset-bottom))] sm:px-6 sm:pb-6">
          <Composer
            value={input}
            onChange={setInput}
            onSend={() => send(input)}
            onStop={cancelPendingRequest}
            loading={loading}
          />
        </div>
      </main>

      {selectedUniversity && selectedUniversityProfile ? (
        <aside
          className="fixed inset-y-0 right-0 z-[60] flex max-w-[94vw] flex-col border-l border-[color:var(--line)] bg-[var(--bg)] shadow-2xl md:relative md:inset-auto md:z-20 md:max-w-none"
          style={{ width: drawerWidth }}
          aria-label={`${selectedUniversity.name} details`}
        >
          <div
            role="separator"
            tabIndex={0}
            aria-label="Resize university details panel"
            aria-orientation="vertical"
            aria-valuemin={drawerWidthBounds(sidebarCollapsed).min}
            aria-valuemax={drawerWidthBounds(sidebarCollapsed).max}
            aria-valuenow={Math.round(drawerWidth)}
            className="group absolute inset-y-0 -left-1 z-10 hidden w-2 cursor-col-resize touch-none items-center justify-center md:flex"
            onPointerDown={(event) => {
              event.preventDefault();
              resizeStart.current = { clientX: event.clientX, width: drawerWidth };
              setResizingDrawer(true);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowLeft") {
                event.preventDefault();
                setDrawerWidth((width) => clampDrawerWidth(width + 24, sidebarCollapsed));
              } else if (event.key === "ArrowRight") {
                event.preventDefault();
                setDrawerWidth((width) => clampDrawerWidth(width - 24, sidebarCollapsed));
              } else if (event.key === "Home") {
                event.preventDefault();
                setDrawerWidth(drawerWidthBounds(sidebarCollapsed).min);
              } else if (event.key === "End") {
                event.preventDefault();
                setDrawerWidth(drawerWidthBounds(sidebarCollapsed).max);
              }
            }}
          >
            <span className="h-16 w-1 rounded-full bg-[var(--line)] transition group-hover:bg-[var(--accent)]" />
          </div>
          <div className="flex shrink-0 items-center justify-between gap-3 border-b border-[color:var(--line)] px-4 py-4 sm:px-6">
            <div className="min-w-0">
              <div className="truncate text-[16px] font-semibold text-[var(--ink)]">University details</div>
              <div className="truncate text-[12px] text-[var(--muted)]">{selectedUniversity.name}</div>
            </div>
            <button
              type="button"
              onClick={clearSelectedUniversity}
              className="shrink-0 rounded-full px-3 py-1.5 text-[13px] text-[var(--muted)] transition hover:bg-[var(--hover)] hover:text-[var(--ink)]"
              aria-label="Close university details"
            >
              Close
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3 sm:p-5">
            <UniCard
              card={selectedUniversity}
              school={selectedUniversityProfile.school_code}
              programmeType={selectedUniversityProfile.programme_type}
              term={selectedUniversityProfile.preferred_semester}
              moduleCodes={selectedUniversityProfile.module_codes}
              excludedModuleTypes={selectedUniversityProfile.excluded_module_types}
              includedModuleTypes={selectedUniversityProfile.included_module_types}
              restoredModuleTypes={selectedUniversityProfile.restored_module_types}
            />
          </div>
        </aside>
      ) : null}
    </div>
  );
}

function MenuIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

function ChevronRightIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}
