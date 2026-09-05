"use client";

import { useEffect, useState } from "react";

type Theme = "light" | "dark";

function readTheme(): Theme {
  const t = document.documentElement.getAttribute("data-theme");
  return t === "light" ? "light" : "dark";
}

function applyTheme(next: Theme) {
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem("nex-theme", next);
  } catch {
    /* ignore */
  }
  window.dispatchEvent(new Event("nex-theme"));
}

function useTheme() {
  const [theme, setTheme] = useState<Theme>("dark");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const sync = () => setTheme(readTheme());
    sync();
    setReady(true);
    window.addEventListener("nex-theme", sync);
    return () => window.removeEventListener("nex-theme", sync);
  }, []);

  function toggle() {
    applyTheme(theme === "dark" ? "light" : "dark");
  }

  return { theme, ready, toggle };
}

export default function ThemeToggle() {
  const { theme, ready, toggle } = useTheme();

  return (
    <button
      type="button"
      onClick={toggle}
      className="glass flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-[var(--ink)]"
      aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
      title={ready && theme === "dark" ? "Light mode" : "Dark mode"}
    >
      {ready && theme === "dark" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3v2M12 19v2M5 12H3M21 12h-2M6.2 6.2 4.8 4.8M19.2 19.2l-1.4-1.4M17.8 6.2l1.4-1.4M6.2 17.8l-1.4 1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4 7 7 0 0 0 20 14.5z" />
    </svg>
  );
}
