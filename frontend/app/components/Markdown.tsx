"use client";

import { Fragment } from "react";
import type { SourceEvidence } from "@/lib/api";

/**
 * A deliberately small markdown renderer for assistant messages.
 *
 * The previous formatting strategy was `whitespace-pre-wrap` and nothing else,
 * so the answer's own bullet list rendered as literal `- **University**` text.
 * This covers exactly what the backend emits — paragraphs, bullets, bold and
 * links — and nothing more.
 *
 * It builds React elements rather than HTML strings: there is no
 * `dangerouslySetInnerHTML` anywhere, so a source title or an excerpt that
 * happens to contain markup is text, not markup. Calendar answers also use
 * headings and simple pipe tables, which are parsed here so they remain
 * responsive instead of appearing as literal Markdown on small screens.
 */

const INLINE = /(\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\)|`[^`]+`)/g;

function renderInline(text: string, keyPrefix: string) {
  return text.split(INLINE).map((piece, i) => {
    const key = `${keyPrefix}-${i}`;
    if (!piece) return null;

    if (piece.startsWith("**") && piece.endsWith("**")) {
      return (
        <strong key={key} className="font-semibold text-[var(--ink)]">
          {piece.slice(2, -2)}
        </strong>
      );
    }
    if (piece.startsWith("`") && piece.endsWith("`")) {
      return (
        <code key={key} className="rounded bg-[var(--hover)] px-1 py-0.5 text-[12px]">
          {piece.slice(1, -1)}
        </code>
      );
    }
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(piece);
    if (link) {
      const href = link[2];
      const isCitationNumber = /^\d+$/.test(link[1].trim());
      // Only http(s) and same-origin API paths. A "javascript:" href in model
      // output must never become a live link.
      const safe = /^https?:\/\//i.test(href) || href.startsWith("/");
      return safe ? (
        <a
          key={key}
          href={href}
          target={href.startsWith("/") ? undefined : "_blank"}
          rel="noopener noreferrer"
          className={isCitationNumber ? "app-link citation-link" : "app-link"}
        >
          {link[1]}
        </a>
      ) : (
        <Fragment key={key}>{link[1]}</Fragment>
      );
    }
    return <Fragment key={key}>{piece}</Fragment>;
  });
}

function normaliseLegacyCitations(text: string, sources: SourceEvidence[]): string {
  const numbers = new Map<string, number>();
  let index = 0;
  for (const source of sources) {
    if (!source.url || numbers.has(source.url)) continue;
    numbers.set(source.url, ++index);
  }

  // Older persisted answers used `([**source**](url))`. Rewrite that legacy
  // form at display time so reopened chats use the same numbering as Sources.
  const normalised = text.replace(
    /\(\s*\[\s*(?:\*\*)?source(?:\*\*)?\s*\]\((https?:\/\/[^)\s]+)\)\s*\)/gi,
    (whole, url: string) => {
      const number = numbers.get(url);
      return number ? `([${number}](${url}))` : whole;
    },
  );
  if (numbers.size !== 1) return normalised;
  // Reopened answers may still contain the old numbered or unnumbered source
  // marker. With one evidence item, the answer should contain no citation
  // text at all; the evidence remains available in the separate Source panel.
  const removeSingleSource = (whole: string, url: string) => numbers.has(url) ? "" : whole;
  return normalised
    .replace(
      /\(\s*\[\s*1\s*\]\(([^)\s]+)\)\s*\)/g,
      removeSingleSource,
    )
    .replace(
      /\[\s*1\s*\]\(([^)\s]+)\)/g,
      removeSingleSource,
    );
}

export default function Markdown({
  text,
  sources = [],
}: {
  text: string;
  sources?: SourceEvidence[];
}) {
  const source = normaliseLegacyCitations((text || "").replace(/\r\n/g, "\n"), sources);
  if (!source.trim()) return null;

  const blocks: React.ReactNode[] = [];
  const lines = source.split("\n");
  let bullets: string[] = [];
  let ordered: string[] = [];
  let paragraph: string[] = [];
  let tableRows: string[][] = [];

  const flushBullets = () => {
    if (!bullets.length) return;
    blocks.push(
      <ul key={`ul-${blocks.length}`} className="list-disc space-y-1 pl-5">
        {bullets.map((item, i) => (
          <li key={i}>{renderInline(item, `li-${blocks.length}-${i}`)}</li>
        ))}
      </ul>,
    );
    bullets = [];
  };

  const flushOrdered = () => {
    if (!ordered.length) return;
    blocks.push(
      <ol key={`ol-${blocks.length}`} className="list-decimal space-y-1 pl-5">
        {ordered.map((item, i) => (
          <li key={i}>{renderInline(item, `ol-${blocks.length}-${i}`)}</li>
        ))}
      </ol>,
    );
    ordered = [];
  };

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(
      <p key={`p-${blocks.length}`}>{renderInline(paragraph.join(" "), `p-${blocks.length}`)}</p>,
    );
    paragraph = [];
  };

  const flushTable = () => {
    if (!tableRows.length) return;
    const [header, ...rows] = tableRows;
    blocks.push(
      <div key={`table-${blocks.length}`} className="max-w-full overflow-x-auto rounded-xl border border-[color:var(--line)]">
        <table className="w-full min-w-[360px] border-collapse text-left text-[13px] leading-6">
          <thead className="bg-[var(--hover)] text-[var(--muted)]">
            <tr>
              {header.map((cell, i) => (
                <th key={i} className="whitespace-nowrap px-3 py-2 font-semibold">
                  {renderInline(cell, `th-${blocks.length}-${i}`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-t border-[color:var(--line)] align-top">
                {row.map((cell, i) => (
                  <td key={i} className="px-3 py-2 text-[var(--ink)]">
                    {renderInline(cell, `td-${blocks.length}-${rowIndex}-${i}`)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    );
    tableRows = [];
  };

  const flushAll = () => {
    flushTable();
    flushBullets();
    flushOrdered();
    flushParagraph();
  };

  const tableCells = (line: string): string[] | null => {
    if (!/^\s*\|.*\|\s*$/.test(line)) return null;
    return line
      .trim()
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((cell) => cell.trim());
  };

  const isTableDivider = (cells: string[]) =>
    cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));

  for (const raw of lines) {
    const line = raw.trimEnd();
    const cells = tableCells(line);
    if (cells) {
      flushBullets();
      flushOrdered();
      flushParagraph();
      if (!isTableDivider(cells)) tableRows.push(cells);
      continue;
    }
    flushTable();

    const heading = /^\s*(#{1,6})\s+(.+?)\s*$/.exec(line);
    if (heading) {
      flushBullets();
      flushOrdered();
      flushParagraph();
      const level = Math.min(heading[1].length, 4);
      const Heading = level === 1 ? "h1" : level === 2 ? "h2" : level === 3 ? "h3" : "h4";
      blocks.push(
        <Heading
          key={`heading-${blocks.length}`}
          className={
            level <= 2
              ? "text-[18px] font-semibold tracking-tight text-[var(--ink)]"
              : "text-[15px] font-semibold text-[var(--ink)]"
          }
        >
          {renderInline(heading[2], `heading-${blocks.length}`)}
        </Heading>,
      );
      continue;
    }
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      flushOrdered();
      bullets.push(bullet[1]);
      continue;
    }
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      flushBullets();
      flushParagraph();
      ordered.push(numbered[1]);
      continue;
    }
    // An indented line continues the bullet above it rather than starting a
    // paragraph, which is how the workload notes are emitted.
    if (bullets.length && /^\s{2,}\S/.test(raw)) {
      bullets[bullets.length - 1] += ` ${line.trim()}`;
      continue;
    }
    if (!line.trim()) {
      flushBullets();
      flushOrdered();
      flushParagraph();
      continue;
    }
    flushBullets();
    flushOrdered();
    paragraph.push(line.trim());
  }
  flushAll();

  return <div className="max-w-full space-y-3 break-words text-[15px] leading-7 text-[var(--ink)]">{blocks}</div>;
}
