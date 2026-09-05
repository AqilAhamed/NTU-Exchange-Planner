"""Build the project-local ChromaDB corpus for NTU-student guidance.

This is an offline setup script. The application does not import pypdf or read
the source directory at runtime; it only reads the generated Chroma artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any

from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = PROJECT_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from graph.config import general_questions_dataset_path

from data import general_questions_store as store

INGEST_VERSION = "section-aware-v7"
MAX_CHARS = 1000
TABLE_MAX_CHARS = 2400
OVERLAP = 120


def _clean(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").replace("\x00", " ").splitlines():
        # pypdf uses the replacement character for a handful of embedded PDF
        # glyphs. Keep numeric separators readable and remove the rest rather
        # than indexing strings such as ``3�000`` or ``Mob ity``.
        value = re.sub(r"(?<=\d)�(?=\d)", ",", line)
        value = value.replace("�", " ").replace("\u00ad", "")
        value = re.sub(r"[ \t]+", " ", value).strip()
        if value:
            lines.append(value)
    return "\n".join(lines)


def _blocks(text: str) -> list[str]:
    """Split a page on its semantic blank-line boundaries.

    Layout extraction is important here: it keeps a table's header and rows
    together, while ordinary prose still becomes paragraph-sized blocks. The
    old character-window splitter cut tables and sentences in half.
    """
    cleaned = _clean(text)
    if not cleaned:
        return []
    return [block.strip() for block in re.split(r"\n\s*\n", cleaned) if block.strip()]


def _is_table(block: str) -> bool:
    lower = block.lower()
    markers = (
        "category", "eligibility", "requirement", "application", "amount",
        "minimum", "maximum", "course load", "credits", "period",
    )
    return sum(marker in lower for marker in markers) >= 2 or block.count("\n") >= 8


def _split_prose(block: str) -> list[str]:
    prose = re.sub(r"\s+", " ", block).strip()
    if len(prose) <= MAX_CHARS:
        return [prose]
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", prose)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > MAX_CHARS:
            chunks.append(current)
            overlap = current[-OVERLAP:]
            while overlap and not overlap[0].isspace():
                overlap = overlap[1:]
            current = f"{overlap.strip()} {sentence}".strip()
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _page_chunks(text: str) -> list[tuple[str, str]]:
    """Return ``(chunk, kind)`` pairs with tables kept intact where possible."""
    chunks: list[tuple[str, str]] = []
    for block in _blocks(text):
        if _is_table(block) and len(block) <= TABLE_MAX_CHARS:
            chunks.append((block, "table"))
            continue
        if _is_table(block):
            # A very large table is still split only at its row boundaries,
            # never in the middle of a character window.
            rows = block.splitlines()
            current: list[str] = []
            for row in rows:
                candidate = "\n".join([*current, row]).strip()
                if current and len(candidate) > TABLE_MAX_CHARS:
                    chunks.append(("\n".join(current), "table"))
                    current = [row]
                else:
                    current.append(row)
            if current:
                chunks.append(("\n".join(current), "table"))
            continue
        chunks.extend((chunk, "prose") for chunk in _split_prose(block))
    return chunks


def _positioned_lines(page: Any) -> list[tuple[float, list[tuple[float, str]]]]:
    """Extract visual lines with x positions for table reconstruction."""
    items: list[tuple[float, float, str]] = []

    def visitor(text: str, _cm: Any, tm: list[float], _font: Any, _size: float) -> None:
        value = re.sub(r"(?<=\d)�(?=\d)", ",", text or "")
        value = re.sub(r"\s+", " ", value.replace("�", " ")).strip()
        if value:
            items.append((float(tm[4]), float(tm[5]), value))

    page.extract_text(visitor_text=visitor)
    lines: list[list[Any]] = []
    for x, y, text in sorted(items, key=lambda item: (-item[1], item[0])):
        line = next((line for line in lines if abs(line[0] - y) <= 2), None)
        if line is None:
            lines.append([y, [(x, text)]])
        else:
            line[1].append((x, text))
    return [
        (float(y), sorted(parts, key=lambda item: item[0]))
        for y, parts in lines
    ]


def _structured_table_text(page: Any) -> str | None:
    """Convert a visually arranged table into retrieval-friendly records.

    PDF text extraction often reads a multi-column table down the page and
    loses the relationship between a row label and its values. This parser uses
    the original x positions to recover column starts, then emits one labelled
    record per visual row. It is intentionally conservative: if a page does
    not have a recognisable multi-column header, the normal prose/table chunker
    remains the source of truth.
    """
    lines = _positioned_lines(page)
    # A PDF page contains many lines whose words happen to be spread across
    # the page.  Treating the first such line as a header creates fabricated
    # tables from ordinary prose (for example, a heading such as
    # "Frequently Asked Questions").  Require evidence from the following
    # visual lines that several column starts repeat.  This is deliberately
    # geometry-based rather than dependent on any document-specific labels.
    candidates: list[tuple[float, list[tuple[float, str]], float]] = []
    for y, parts in lines:
        span = max(x for x, _text in parts) - min(x for x, _text in parts)
        gaps = sum(
            right_x - left_x >= 40
            for (left_x, _left_text), (right_x, _right_text)
            in pairwise(parts)
        )
        if len(parts) < 3 or span < 120 or gaps < 2:
            continue
        if sum(len(text) <= 60 for _x, text in parts) < 3:
            continue

        provisional_starts: list[float] = []
        previous_x: float | None = None
        for x, _text in parts:
            if previous_x is None or x - previous_x >= 40:
                provisional_starts.append(x)
            previous_x = x
        if len(provisional_starts) < 3:
            continue
        gaps_between_starts = [
            right - left
            for left, right in pairwise(provisional_starts)
        ]
        median_gap = sorted(gaps_between_starts)[len(gaps_between_starts) // 2]
        regularity = sum(
            abs(gap - median_gap) <= max(10, median_gap * 0.2)
            for gap in gaps_between_starts
        ) / max(len(gaps_between_starts), 1)
        if len(provisional_starts) < 4 or regularity < 0.75:
            continue

        nearby_lines = [
            row_parts
            for row_y, row_parts in lines
            if y - 100 <= row_y < y - 4
        ]
        body_x = [x for row_parts in nearby_lines for x, _text in row_parts]
        starts = [
            min(
                (x for x in body_x if abs(x - start) <= 25),
                default=start,
            )
            for start in provisional_starts
        ]
        aligned_parts = 0
        total_parts = 0
        multi_column_rows = 0
        for row_parts in nearby_lines:
            occupied: set[int] = set()
            for x, _text in row_parts:
                total_parts += 1
                column = min(
                    range(len(starts)),
                    key=lambda index: abs(x - starts[index]),
                )
                if abs(x - starts[column]) <= 10:
                    aligned_parts += 1
                    occupied.add(column)
            if len(occupied) >= 3:
                multi_column_rows += 1

        alignment = aligned_parts / max(total_parts, 1)
        # Four or more repeating starts and a strong alignment score make this
        # conservative enough to reject normal paragraph text while retaining
        # the multi-column tables used by the source corpus.
        if (
            alignment >= 0.35
            and multi_column_rows >= 3
            and alignment * multi_column_rows * len(starts) >= 12
        ):
            score = alignment * multi_column_rows * len(starts)
            candidates.append((y, parts, score))

    header = max(candidates, key=lambda candidate: candidate[2], default=None)
    if header is None:
        return None
    header_y, _header_parts, _header_score = header
    header_parts = [
        (x, text)
        for y, parts in lines
        # Multi-line column headings are common in PDF tables. The selected
        # header row is the bottom line of the heading block, so include the
        # preceding visual lines as well.
        if header_y - 5 <= y <= header_y + 35
        for x, text in parts
    ]
    header_parts.sort(key=lambda item: item[0])
    # Words within a header cell are close together; the larger gaps separate
    # the table's columns without depending on the table's actual labels.
    starts: list[float] = []
    previous_x: float | None = None
    # Derive the grid from the selected row itself. The surrounding wrapped
    # heading lines are used for text only; deriving starts from them would
    # treat each wrapped word as a new column.
    for x, _ in _header_parts:
        if previous_x is None or x - previous_x >= 40:
            starts.append(x)
        previous_x = x
    if len(starts) < 3:
        return None

    def cells_for(parts: list[tuple[float, str]]) -> list[str]:
        cells = [[] for _ in starts]
        for x, text in parts:
            column = max(
                (index for index, start in enumerate(starts) if x >= start - 5),
                default=0,
            )
            cells[column].append(text)
        return [re.sub(r"\s+", " ", " ".join(cell)).strip() for cell in cells]

    # Header text is often inset relative to the body cells. Snap each column
    # start to the leftmost nearby x position in the table body so values such
    # as the first eligibility cell are assigned to the correct column.
    body_x = [x for y, parts in lines if header_y - 245 <= y < header_y - 4 for x, _ in parts]
    snapped_starts: list[float] = []
    for start in starts:
        nearby_x = [x for x in body_x if abs(x - start) <= 25]
        if not nearby_x:
            snapped_starts.append(start)
            continue
        # Prefer the most frequently reused x-position, then the closest one.
        # This avoids snapping a real column start to an indented wrapped word.
        unique_x = sorted({round(x, 1) for x in nearby_x})
        snapped_starts.append(
            max(
                unique_x,
                key=lambda x: (
                    sum(abs(candidate - x) <= 2 for candidate in nearby_x),
                    -abs(x - start),
                ),
            )
        )
    starts = snapped_starts
    header_cells = cells_for(header_parts)
    positioned_rows: list[tuple[float, list[str]]] = []
    for y, parts in lines:
        if not (header_y - 245 <= y < header_y - 4):
            continue
        cells = cells_for(parts)
        if any(cells):
            # Keep each visual line separate. Trying to assign wrapped cells to
            # a semantic row from PDF coordinates can silently attach an
            # eligibility value to the preceding category row. The LLM can
            # safely reason over these position-preserving records.
            positioned_rows.append((y, cells))
    if len(positioned_rows) < 3:
        return None

    columns = header_cells
    # Some PDFs repeat the generic column label before every wrapped option
    # name (for example, "Scholarship Alpha", "Scholarship Beta"). Remove
    # that shared presentation prefix while retaining the actual option names.
    option_columns = columns[1:]
    option_prefixes = [
        re.match(r"^([A-Za-z][A-Za-z0-9/&'()_-]*)\s+", option)
        for option in option_columns
    ]
    if option_columns and all(match for match in option_prefixes):
        prefix = option_prefixes[0].group(1)
        if all(match.group(1).lower() == prefix.lower() for match in option_prefixes):
            columns = [
                re.sub(
                    rf"\s+{re.escape(prefix)}\s+type$",
                    "",
                    columns[0],
                    flags=re.IGNORECASE,
                ),
                *[
                    re.sub(
                        rf"^{re.escape(prefix)}\s+",
                        "",
                        option,
                        flags=re.IGNORECASE,
                    ).strip()
                    for option in option_columns
                ],
            ]
    output = [
        "Structured table reconstructed from the source PDF:",
        "Columns: " + " | ".join(columns),
    ]
    output.extend(
        f"Visual row: {' | '.join(cells)}" for _y, cells in positioned_rows
    )

    # Keep a second, semantic representation of the table. PDF text order is
    # not a reliable row order: wrapped values can appear above or below the
    # left-hand row label. Detect label groups from the first table column and
    # use the midpoints between neighbouring groups as field boundaries. This
    # works for any multi-column table whose first column contains row labels.
    label_rows = [
        (y, cells[0])
        for y, cells in positioned_rows
        if cells and cells[0]
    ]
    label_groups: list[list[tuple[float, str]]] = []
    for y, label in label_rows:
        if label_groups and label_groups[-1][-1][0] - y <= 16:
            label_groups[-1].append((y, label))
        else:
            label_groups.append([(y, label)])

    group_centres = [
        sum(y for y, _label in group) / len(group)
        for group in label_groups
    ]
    for index, group in enumerate(label_groups):
        centre = group_centres[index]
        upper = (
            (group_centres[index - 1] + centre) / 2
            if index
            else centre + 40
        )
        lower = (
            (centre + group_centres[index + 1]) / 2
            if index + 1 < len(group_centres)
            else centre - 40
        )
        field = " ".join(label for _y, label in group)
        values = [[] for _ in columns[1:]]
        for y, cells in positioned_rows:
            if not (lower <= y <= upper):
                continue
            for column_index in range(1, min(len(cells), len(columns))):
                value = _clean(cells[column_index])
                value = re.sub(r"(?<=\d)['’](?=\d)", ",", value)
                value = re.sub(r"\(\s*['’]?\s*\)", "", value)
                value = re.sub(r"\s+", " ", value).strip()
                if value and value not in {"-", "�", "1"} and value not in values[column_index - 1]:
                    values[column_index - 1].append(value)
        records = [
            f"{_clean(column)}: {' '.join(parts)}"
            for column, parts in zip(columns[1:], values)
            if _clean(column) and parts
        ]
        if records:
            output.append(f"Table field: {field} | " + " | ".join(records))
    return "\n".join(output)


def _source_id(document: str, page: int, chunk_index: int) -> str:
    raw = f"{document}:{page}:{chunk_index}".encode()
    return f"gq-{hashlib.sha1(raw).hexdigest()}"


def ingest(source_dir: Path, reset: bool = False) -> tuple[int, int]:
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

    collection = store.reset_collection() if reset else store.collection()
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, str | int]] = []
    pdf_count = 0
    for path in sorted(source_dir.glob("*.pdf"), key=lambda item: item.name.lower()):
        pdf_count += 1
        reader = PdfReader(str(path))
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                page_text = page.extract_text() or ""
            page_chunks = _page_chunks(page_text)
            structured_table = _structured_table_text(page)
            if structured_table:
                page_chunks.append((structured_table, "structured_table"))
            for chunk_index, (chunk, chunk_kind) in enumerate(page_chunks):
                source_id = _source_id(path.name, page_number, chunk_index)
                ids.append(source_id)
                documents.append(chunk)
                # Store only stable document metadata, never the user's local
                # absolute path. This makes the collection portable to Bedrock.
                metadatas.append(
                    {
                        "document": path.name,
                        "page": page_number,
                        "chunk_index": chunk_index,
                        "chunk_kind": chunk_kind,
                        "ingest_version": INGEST_VERSION,
                        "source_id": source_id,
                    }
                )

    if not ids:
        raise RuntimeError("No extractable text was found in the PDF directory.")
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
    return pdf_count, len(ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=general_questions_dataset_path(),
        help="Directory containing the NTU intranet PDFs used for ingestion.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Replace the named Chroma collection before ingesting.",
    )
    args = parser.parse_args()
    pdf_count, chunk_count = ingest(args.source_dir.resolve(), reset=args.reset)
    print(f"Ingested {chunk_count} chunks from {pdf_count} PDFs.")
    print(f"ChromaDB: {store.general_questions_chroma_path()}")
    print(f"Embedding model cache: {store.general_questions_embedding_path()}")
    print(f"Collection count: {store.collection().count()}")


if __name__ == "__main__":
    main()
