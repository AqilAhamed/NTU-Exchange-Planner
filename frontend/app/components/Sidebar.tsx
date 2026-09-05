"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { ChatSummary } from "@/lib/api";

function dayLabel(iso: string): "Today" | "Yesterday" | "Earlier" {
  const d = new Date(iso);
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const then = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diff = (start.getTime() - then.getTime()) / 86400000;
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  return "Earlier";
}

export default function Sidebar({
  chats,
  activeId,
  search,
  homeActive,
  open,
  collapsed = false,
  onToggle,
  onSearch,
  onHome,
  onNew,
  onSelect,
  onDelete,
  onClose,
}: {
  chats: ChatSummary[];
  activeId: string | null;
  search: string;
  homeActive?: boolean;
  open?: boolean;
  collapsed?: boolean;
  onToggle: () => void;
  onSearch: (v: string) => void;
  onHome: () => void;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onClose?: () => void;
}) {
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(true), []);

  const filtered = chats.filter((c) =>
    c.title.toLowerCase().includes(search.toLowerCase()),
  );
  const groups = useMemo(() => {
    const next: Record<string, ChatSummary[]> = { Today: [], Yesterday: [], Earlier: [] };
    if (!ready) return next;
    for (const c of filtered) {
      next[dayLabel(c.updated_at)].push(c);
    }
    return next;
  }, [filtered, ready]);

  return (
    <aside
      className={`glass-nav fixed inset-y-0 left-0 z-50 flex h-full w-[min(86vw,280px)] shrink-0 flex-col overflow-hidden rounded-r-[24px] text-[var(--ink)] transition-[transform,width] duration-200 md:static md:translate-x-0 md:rounded-none ${
        collapsed ? "md:w-[72px]" : "md:w-[280px]"
      } ${
        open ? "translate-x-0" : "-translate-x-full pointer-events-none md:pointer-events-auto md:translate-x-0"
      }`}
    >
      <div className="flex items-center justify-between px-4 pt-5 md:hidden">
        <span className="text-[13px] font-semibold">Menu</span>
        <button
          type="button"
          onClick={onClose}
          className="rounded-full px-3 py-1 text-[13px] text-[var(--muted)]"
          aria-label="Close menu"
        >
          Close
        </button>
      </div>
      <div className="hidden justify-end px-3 pt-4 md:flex">
        <button
          type="button"
          onClick={onToggle}
          className="rounded-full p-2 text-[var(--muted)] transition hover:bg-[var(--hover)] hover:text-[var(--ink)]"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          <ChevronIcon collapsed={collapsed} />
        </button>
      </div>
      <div className="flex flex-col gap-3 px-4 pt-4 md:pt-5">
        <button
          type="button"
          onClick={onNew}
          className={`flex h-11 items-center justify-center rounded-full bg-[var(--cta-bg)] text-[14px] font-semibold text-[var(--cta-ink)] shadow-[0_0_18px_var(--send-glow)] transition hover:opacity-90 ${
            collapsed ? "md:mx-auto md:w-11" : ""
          }`}
          title={collapsed ? "New chat" : undefined}
        >
          <span className={collapsed ? "md:hidden" : ""}>+&nbsp; New Chat</span>
          {collapsed ? <span className="hidden md:inline">+</span> : null}
        </button>
        <label className={`flex h-10 items-center gap-2 rounded-full border border-[color:var(--line)] bg-[var(--hover)] px-4 text-[var(--muted)] ${
          collapsed ? "md:mx-auto md:w-11 md:justify-center md:px-0" : ""
        }`}>
          <SearchIcon />
          <input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Search"
            className={`w-full bg-transparent text-[13px] text-[var(--ink)] outline-none placeholder:text-[var(--faint)] ${
              collapsed ? "md:hidden" : ""
            }`}
          />
        </label>
      </div>

      <nav className="mt-5 px-3">
        <NavItem
          active={!!homeActive}
          label="Home"
          onClick={onHome}
          icon={<HomeIcon />}
          collapsed={collapsed}
        />
      </nav>

      <div className={`mt-6 flex-1 overflow-y-auto px-2 pb-6 ${collapsed ? "md:hidden" : ""}`}>
        <div className="px-3 pb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-[var(--faint)]">
          Chats
        </div>
        {ready &&
          (["Today", "Yesterday", "Earlier"] as const).map((g) =>
            groups[g].length ? (
              <div key={g} className="mb-3">
                <div className="px-3 py-1 text-[11px] text-[var(--faint)]">{g}</div>
                {groups[g].map((c) => (
                  // Two sibling buttons, not a click handler nested inside a
                  // button. The previous markup put a <span onClick> inside the
                  // <button>, which is invalid HTML and unreachable by keyboard,
                  // so a keyboard user could never delete a chat.
                  <div key={c.session_id} className="group relative">
                    <button
                      type="button"
                      onClick={() => onSelect(c.session_id)}
                      className={`flex w-full items-center gap-2 rounded-xl px-3 py-2 pr-9 text-left text-[13px] ${
                        activeId === c.session_id ? "bg-[var(--active)]" : "hover:bg-[var(--hover)]"
                      }`}
                    >
                      <span className="text-[var(--muted)]">
                        <BubbleIcon />
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[var(--ink)]">{c.title}</span>
                    </button>
                    <button
                      type="button"
                      aria-label={`Delete chat: ${c.title}`}
                      title="Delete chat"
                      onClick={() => onDelete(c.session_id)}
                      className="absolute right-1 top-1/2 -translate-y-1/2 rounded-lg px-2 py-1 text-[var(--muted)] opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            ) : null,
          )}
      </div>
    </aside>
  );
}

function NavItem({
  label,
  icon,
  active,
  onClick,
  collapsed,
}: {
  label: string;
  icon: ReactNode;
  active?: boolean;
  onClick: () => void;
  collapsed?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`mb-1 flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-[14px] ${
        collapsed ? "md:justify-center" : ""
      } ${
        active ? "bg-[var(--active)] text-[var(--ink)]" : "text-[var(--muted)] hover:bg-[var(--hover)]"
      }`}
    >
      {icon}
      <span className={collapsed ? "md:hidden" : ""}>{label}</span>
    </button>
  );
}

function SearchIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20L17 17" />
    </svg>
  );
}
function HomeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z" />
    </svg>
  );
}
function ChevronIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d={collapsed ? "m9 6 6 6-6 6" : "m15 6-6 6 6 6"} />
    </svg>
  );
}
function BubbleIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M4 6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H9l-5 4V6z" />
    </svg>
  );
}
