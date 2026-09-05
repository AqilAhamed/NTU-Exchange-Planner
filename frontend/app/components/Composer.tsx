"use client";

export default function Composer({
  value,
  onChange,
  onSend,
  onStop,
  loading,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  loading?: boolean;
  disabled?: boolean;
}) {
  return (
    <form
      className="glass mx-auto flex min-w-0 w-full max-w-[820px] items-center gap-2 rounded-full px-3 py-2 sm:gap-3 sm:px-5"
      onSubmit={(e) => {
        e.preventDefault();
        onSend();
      }}
    >
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="type your prompt here."
        className="h-11 min-w-0 flex-1 bg-transparent text-[14px] text-[var(--ink)] outline-none placeholder:text-[var(--faint)] sm:text-[15px]"
      />
      <button
        type={loading ? "button" : "submit"}
        onClick={loading ? onStop : undefined}
        disabled={disabled || (!loading && !value.trim())}
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-[var(--cta-bg)] text-[var(--cta-ink)] shadow-[0_0_20px_var(--send-glow)] transition hover:scale-[1.03] disabled:opacity-40"
        aria-label={loading ? "Stop response" : "Send"}
        title={loading ? "Stop response" : "Send"}
      >
        {loading ? <Square /> : <Arrow />}
      </button>
    </form>
  );
}

function Arrow() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

function Square() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </svg>
  );
}
