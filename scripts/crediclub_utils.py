
from __future__ import annotations

import configparser
import hashlib
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import pymupdf


MONTHS_ES = {
    "ENERO": 1,
    "FEBRERO": 2,
    "MARZO": 3,
    "ABRIL": 4,
    "MAYO": 5,
    "JUNIO": 6,
    "JULIO": 7,
    "AGOSTO": 8,
    "SEPTIEMBRE": 9,
    "SETIEMBRE": 9,
    "OCTUBRE": 10,
    "NOVIEMBRE": 11,
    "DICIEMBRE": 12,
}

MONTH_ABBR_ES = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "SET": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
}


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.upper()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def load_config(path: str | Path = ".config") -> dict[str, str]:
    """
    Load the project's simple key=value .config file.

    The existing project uses an INI-like file without a section header, so
    we prepend one for ConfigParser.
    """
    path = Path(path)
    if not path.exists():
        return {}

    raw = path.read_text(encoding="utf-8")
    parser = configparser.ConfigParser()
    parser.read_string("[root]\n" + raw)

    result: dict[str, str] = {}
    for key, value in parser["root"].items():
        cleaned = value.strip().strip('"').strip("'")
        result[key] = cleaned
    return result


def project_path(config: dict[str, str], key: str, default: str) -> Path:
    return Path(config.get(key, default)).expanduser()


def list_pdfs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*.pdf")
        if p.is_file()
    )


def _pdf_password(config: dict[str, str]) -> str | None:
    """
    Password lookup order:
      1. CREDICLUB_PDF_PASSWORD environment variable
      2. crediclub_pdf_password in .config
      3. crediclub_rfc in .config

    The password is never printed or written to output tables.
    """
    return (
        os.environ.get("CREDICLUB_PDF_PASSWORD")
        or config.get("crediclub_pdf_password")
        or config.get("crediclub_rfc")
        or None
    )


def open_pdf(path: Path, config: dict[str, str]) -> pymupdf.Document:
    doc = pymupdf.open(path)

    if doc.needs_pass:
        password = _pdf_password(config)
        if not password:
            doc.close()
            raise RuntimeError(
                f"{path.name} is password protected. Configure "
                "CREDICLUB_PDF_PASSWORD or crediclub_pdf_password/crediclub_rfc "
                "in .config."
            )

        if not doc.authenticate(password):
            doc.close()
            raise RuntimeError(
                f"Could not open password-protected Crediclub statement: {path.name}. "
                "Verify the configured RFC/password."
            )

    return doc


def parse_money(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    value = value.replace("$", "").replace(" ", "")
    value = value.replace("−", "-")

    # Legacy statements often render zero as ".00".
    if value in {".00", "+.00", "-.00"}:
        return 0.0

    value = value.rstrip(").,;:")
    value = value.lstrip("(")

    if not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})*(?:\.\d{2})|[+-]?\d+\.\d{2}", value):
        return None

    return float(value.replace(",", ""))


def parse_percent(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.replace("%", "").replace("*", "").strip()
    try:
        return float(value)
    except ValueError:
        return None


def parse_spanish_long_date(value: str) -> date:
    norm = normalize_text(value)
    m = re.search(
        r"(\d{1,2})\s+DE\s+([A-Z]+)\s+(?:DEL?|DE)?\s*(\d{4})",
        norm,
    )
    if not m:
        raise ValueError(f"Could not parse Spanish date: {value!r}")
    return date(int(m.group(3)), MONTHS_ES[m.group(2)], int(m.group(1)))


def parse_period(value: str) -> tuple[date, date]:
    norm = normalize_text(value)
    m = re.search(
        r"(\d{1,2})\s+AL\s+(\d{1,2})\s+DE\s+([A-Z]+)\s+(?:DEL?|DE)?\s*(\d{4})",
        norm,
    )
    if not m:
        raise ValueError(f"Could not parse statement period: {value!r}")
    month = MONTHS_ES[m.group(3)]
    year = int(m.group(4))
    return date(year, month, int(m.group(1))), date(year, month, int(m.group(2)))


def parse_compact_date(value: str, statement_year: int | None = None) -> date:
    value = value.strip()

    for fmt in ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    norm = normalize_text(value)
    m = re.fullmatch(r"(\d{1,2})\s+([A-Z]{3})", norm)
    if m and statement_year is not None:
        return date(
            statement_year,
            MONTH_ABBR_ES[m.group(2)],
            int(m.group(1)),
        )

    raise ValueError(f"Could not parse date: {value!r}")


def detect_layout(page1_text: str) -> str:
    norm = normalize_text(page1_text)

    if (
        "COMO ESTA DISTRIBUIDO TU DINERO AL CIERRE" in norm
        or "I. CUENTA DEBITO CREDICLUB" in norm
    ):
        return "modern_2026_06_plus"

    if (
        "RESUMEN DE MOVIMIENTOS EN CUENTA A LA VISTA" in norm
        or "SUPERTASAS.COM" in norm
    ):
        return "legacy_pre_2026_06"

    raise RuntimeError("Unsupported Crediclub/SuperTasas statement layout.")


def words_by_row(page: pymupdf.Page, y_tolerance: float = 1.8) -> list[list[tuple]]:
    words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
    rows: list[list[tuple]] = []

    for word in words:
        if not rows:
            rows.append([word])
            continue

        current_y = sum(item[1] for item in rows[-1]) / len(rows[-1])
        if abs(word[1] - current_y) <= y_tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])

    for row in rows:
        row.sort(key=lambda w: w[0])
    return rows


def row_text(row: list[tuple]) -> str:
    return " ".join(w[4] for w in row)


def write_table(df: pd.DataFrame, base_path: Path) -> None:
    """
    Write CSV always. Write Parquet when a supported engine is available.
    """
    base_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path = base_path.with_suffix(".csv")
    df.to_csv(csv_path, index=False)

    try:
        df.to_parquet(base_path.with_suffix(".parquet"), index=False)
    except Exception:
        pass


def filename_period(path: Path) -> tuple[int, int] | None:
    # Supports filenames such as 2026_08_Ago.pdf
    m = re.search(r"(?<!\d)(20\d{2})[_-](0?[1-9]|1[0-2])(?:[_-]|$)", path.stem)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def safe_round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)
