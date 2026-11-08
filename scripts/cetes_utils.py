from __future__ import annotations

import ast
import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pymupdf


FILENAME_PATTERN = re.compile(
    r"^(?P<year>\d{4})_(?P<month>0[1-9]|1[0-2])(?:_[^.]+)?\.pdf$",
    re.IGNORECASE,
)

SPANISH_MONTH_ABBR = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}

DATE_DMY4 = re.compile(r"(?P<d>\d{2})/(?P<m>\d{2})/(?P<y>\d{4})")
DATE_DMY2 = re.compile(r"^(?P<d>\d{2})/(?P<m>\d{2})/(?P<y>\d{2})$")

NUMBER_TOKEN_RE = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?")
MONEY_TOKEN_RE = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?")


@dataclass(frozen=True)
class StatementMetadata:
    path: Path
    year: int
    month: int
    period_start: date
    period_end: date
    account_id: str | None
    filename_year: int | None
    filename_month: int | None

    @property
    def year_month(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def filename_period_matches(self) -> bool | None:
        if self.filename_year is None or self.filename_month is None:
            return None
        return self.filename_year == self.year and self.filename_month == self.month


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: str | Path | None = None) -> dict[str, str]:
    config_path = Path(path) if path else project_root() / ".config"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    values: dict[str, str] = {}
    for raw in config_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        try:
            value = ast.literal_eval(raw_value)
        except (ValueError, SyntaxError):
            value = raw_value.strip('"').strip("'")
        values[key] = str(value)
    return values


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    return path.resolve()


def normalize_text(value: str) -> str:
    replacements = {
        "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U",
        "Ü": "U", "Ñ": "N",
    }
    text = value.upper().strip()
    for source, target in replacements.items():
        text = text.replace(source, target)
    return re.sub(r"\s+", " ", text)


def normalize_account_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", "", str(value)).strip().upper()
    return cleaned or None


def pdf_pages_as_lines(path: str | Path) -> list[list[str]]:
    doc = pymupdf.open(path)
    try:
        pages: list[list[str]] = []
        for page in doc:
            pages.append([
                line.strip()
                for line in page.get_text("text").splitlines()
                if line.strip()
            ])
        return pages
    finally:
        doc.close()


def parse_number(value: str) -> float:
    text = value.strip().replace("$", "").replace("%", "").replace(",", "")
    negative_parens = text.startswith("(") and text.endswith(")")
    if negative_parens:
        text = text[1:-1]
    number = float(text)
    return -abs(number) if negative_parens else number


def numeric_tokens(value: str) -> list[float]:
    return [parse_number(token) for token in NUMBER_TOKEN_RE.findall(value)]


def money_tokens(value: str) -> list[float]:
    return [parse_number(token) for token in MONEY_TOKEN_RE.findall(value)]


def extract_account_id(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        norm = normalize_text(line)
        if "CONTRATO/CUENTA CLABE" in norm:
            same = re.search(r"CONTRATO/CUENTA CLABE\s*:?\s*([A-Z0-9._/-]+)", norm)
            if same:
                return normalize_account_id(same.group(1))
            for nxt in lines[i + 1:i + 4]:
                candidate = normalize_account_id(nxt)
                if candidate and re.fullmatch(r"[A-Z0-9._/-]+", candidate):
                    return candidate
    return None


def extract_period(lines: list[str]) -> tuple[date, date] | None:
    combined = " ".join(lines)
    match = re.search(
        r"PER[IÍ]ODO\s+DEL\s*:?\s*(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})",
        combined,
        flags=re.IGNORECASE,
    )
    if not match:
        # PyMuPDF often emits 'Período del:' and the dates on the next line.
        match = re.search(
            r"PER[IÍ]ODO\s+DEL\s*:?\s+(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})",
            combined,
            flags=re.IGNORECASE,
        )
    if not match:
        # Fallback: first explicit dd/mm/yyyy al dd/mm/yyyy pair.
        match = re.search(
            r"(\d{2}/\d{2}/\d{4})\s+al\s+(\d{2}/\d{2}/\d{4})",
            combined,
            flags=re.IGNORECASE,
        )
    if not match:
        return None
    return (
        datetime.strptime(match.group(1), "%d/%m/%Y").date(),
        datetime.strptime(match.group(2), "%d/%m/%Y").date(),
    )


def parse_filename_period(path: str | Path) -> tuple[int | None, int | None]:
    match = FILENAME_PATTERN.match(Path(path).name)
    if not match:
        return None, None
    return int(match.group("year")), int(match.group("month"))


def statement_metadata(path: str | Path, page1_lines: list[str]) -> StatementMetadata:
    period = extract_period(page1_lines)
    if not period:
        raise ValueError(f"Could not extract statement period from {Path(path).name}")
    start, end = period
    account_id = extract_account_id(page1_lines)
    filename_year, filename_month = parse_filename_period(path)
    return StatementMetadata(
        path=Path(path),
        year=end.year,
        month=end.month,
        period_start=start,
        period_end=end,
        account_id=account_id,
        filename_year=filename_year,
        filename_month=filename_month,
    )


def discover_statements(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"CETES statements folder not found: {folder}")

    candidates: list[Path] = []
    for pdf in folder.rglob("*.pdf"):
        candidates.append(pdf)
    return sorted(candidates, key=lambda p: str(p.resolve()).lower())


def find_index(lines: list[str], label: str, start: int = 0, *, contains: bool = False) -> int:
    wanted = normalize_text(label)
    for idx in range(start, len(lines)):
        current = normalize_text(lines[idx])
        if (wanted in current) if contains else (wanted == current):
            return idx
    raise ValueError(f"Label not found: {label}")


def amount_after_label(lines: list[str], label: str, *, contains: bool = False) -> float | None:
    try:
        idx = find_index(lines, label, contains=contains)
    except ValueError:
        return None

    # Amount on the same line after the label.
    same_line = lines[idx]
    same_numbers = money_tokens(same_line)
    if same_numbers:
        return same_numbers[-1]

    # Otherwise scan a short distance, skipping a standalone '$'.
    for line in lines[idx + 1:idx + 5]:
        if line.strip() == "$":
            continue
        nums = money_tokens(line)
        if nums:
            return nums[0]
    return None


def parse_dmy2(value: str) -> date:
    match = DATE_DMY2.match(value.strip())
    if not match:
        raise ValueError(value)
    year2 = int(match.group("y"))
    year = 2000 + year2
    return date(year, int(match.group("m")), int(match.group("d")))


def write_table(df, output_base: str | Path) -> None:
    output_base = Path(output_base)
    output_base.parent.mkdir(parents=True, exist_ok=True)

    csv_path = output_base.with_suffix(".csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    try:
        df.to_parquet(output_base.with_suffix(".parquet"), index=False)
    except (ImportError, ModuleNotFoundError):
        pass
