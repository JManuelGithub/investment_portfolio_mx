from __future__ import annotations

import ast
import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import fitz  # PyMuPDF


MODERN_LAYOUT_START = date(2022, 4, 1)

# January-September use one digit; October-December use two.
# Examples:
#   CB_20221_Contrato.pdf
#   CB_20229_Contrato.pdf
#   CB_202210_Contrato.pdf
FILE_PATTERN = re.compile(
    r"^CB_(?P<year>\d{4})(?P<month>1[0-2]|[1-9])_(?P<contract>.+)\.pdf$",
    re.IGNORECASE,
)

NUMBER_PATTERN = re.compile(
    r"^\(?-?\d[\d,]*(?:\.\d+)?\)?%?$"
)


@dataclass(frozen=True)
class StatementMetadata:
    path: Path
    year: int
    month: int
    contract: str
    period_start: date
    period_end: date

    @property
    def year_month(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def layout_version(self) -> str:
        """GBM changed the statement layout starting in April 2022."""
        if self.period_start < MODERN_LAYOUT_START:
            return "legacy_pre_2022_04"
        return "modern_2022_04_plus"


def project_root() -> Path:
    """Project root assuming these helpers live in <root>/scripts/."""
    return Path(__file__).resolve().parents[1]


def load_config(path: str | Path | None = None) -> dict[str, str]:
    """
    Read the project's simple .config file.

    Supports lines such as:
        path_gbm_edo_cta = "gbm/edo_cta"

    Blank lines and # comments are ignored.
    """
    config_path = Path(path) if path else project_root() / ".config"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            "Create .config in the project root."
        )

    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
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


def parse_statement_filename(path: str | Path) -> StatementMetadata:
    path = Path(path)
    match = FILE_PATTERN.match(path.name)
    if not match:
        raise ValueError(
            f"Invalid GBM statement filename: {path.name}. "
            "Expected CB_YYYYM_Contrato.pdf."
        )

    year = int(match.group("year"))
    month = int(match.group("month"))
    contract = match.group("contract")
    last_day = calendar.monthrange(year, month)[1]

    meta = StatementMetadata(
        path=path,
        year=year,
        month=month,
        contract=contract,
        period_start=date(year, month, 1),
        period_end=date(year, month, last_day),
    )

    return meta


def discover_statements(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"GBM statements folder not found: {folder}")

    candidates: list[tuple[date, Path]] = []
    for pdf in folder.rglob("CB_*.pdf"):
        try:
            meta = parse_statement_filename(pdf)
        except ValueError:
            continue
        candidates.append((meta.period_start, pdf))

    return [p for _, p in sorted(candidates, key=lambda item: item[0])]


def pdf_pages_as_lines(path: str | Path) -> list[list[str]]:
    """
    Extract embedded PDF text with PyMuPDF.
    OCR is intentionally not used because the supported GBM statements
    contain selectable text.
    """
    doc = fitz.open(path)
    try:
        pages: list[list[str]] = []
        for page in doc:
            lines = [
                line.strip()
                for line in page.get_text("text").splitlines()
                if line.strip()
            ]
            pages.append(lines)
        return pages
    finally:
        doc.close()


def flatten_pages(pages: Iterable[Iterable[str]]) -> list[str]:
    return [line for page in pages for line in page]


def normalize_text(value: str) -> str:
    replacements = {
        "Á": "A",
        "É": "E",
        "Í": "I",
        "Ó": "O",
        "Ú": "U",
        "Ü": "U",
        "Ñ": "N",
    }
    text = value.upper().strip()
    for source, target in replacements.items():
        text = text.replace(source, target)
    return re.sub(r"\s+", " ", text)


def parse_number(value: str) -> float:
    """
    Convert GBM-formatted values to float.

    Examples:
        1,234.56   -> 1234.56
        (51.33)    -> -51.33
        -336.32    -> -336.32
        7.69%      -> 7.69
    """
    text = value.strip().replace("$", "").replace("%", "").replace(",", "")
    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1]

    number = float(text)
    return -abs(number) if negative_parentheses else number


def is_number(value: str) -> bool:
    return bool(NUMBER_PATTERN.match(value.strip().replace("$", "")))


def find_line(lines: list[str], label: str, start: int = 0) -> int:
    wanted = normalize_text(label)
    for idx in range(start, len(lines)):
        if normalize_text(lines[idx]) == wanted:
            return idx
    raise ValueError(f"Section/label not found: {label}")


def find_line_contains(
    lines: list[str],
    label: str,
    start: int = 0,
) -> int:
    wanted = normalize_text(label)
    for idx in range(start, len(lines)):
        if wanted in normalize_text(lines[idx]):
            return idx
    raise ValueError(f"Section/label not found: {label}")


def following_numbers(
    lines: list[str],
    label: str,
    count: int,
    *,
    contains: bool = False,
) -> list[float]:
    finder = find_line_contains if contains else find_line
    idx = finder(lines, label)

    values: list[float] = []
    for line in lines[idx + 1:]:
        if is_number(line):
            values.append(parse_number(line))
            if len(values) == count:
                return values
        elif values:
            # Once numeric data starts, a new text label means this row ended.
            break

    raise ValueError(
        f"Expected {count} numeric values after '{label}', found {len(values)}."
    )


def extract_between_headers(
    lines: list[str],
    start_header: str,
    end_headers: Iterable[str],
) -> list[str]:
    """
    Locate a section by header text across the whole document.
    This deliberately does not rely on page numbers.
    """
    start = find_line_contains(lines, start_header) + 1

    normalized_ends = [normalize_text(h) for h in end_headers]
    end = len(lines)
    for idx in range(start, len(lines)):
        current = normalize_text(lines[idx])
        if any(end_header in current for end_header in normalized_ends):
            end = idx
            break

    return lines[start:end]


def parse_ddmmyyyy(value: str) -> date:
    return datetime.strptime(value, "%d/%m/%Y").date()


def safe_date(year: int, month: int, day: int) -> date:
    """
    Build a date defensively. If a statement has an unexpected operation day,
    clamp it to the valid days of that statement month.
    """
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(day, 1), max_day))


def write_table(df, output_base: str | Path) -> None:
    """
    Always write CSV. Also write Parquet when pyarrow/fastparquet is installed.
    Power BI can consume the CSV directly.
    """
    output_base = Path(output_base)
    output_base.parent.mkdir(parents=True, exist_ok=True)

    csv_path = output_base.with_suffix(".csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    try:
        parquet_path = output_base.with_suffix(".parquet")
        df.to_parquet(parquet_path, index=False)
    except (ImportError, ModuleNotFoundError):
        pass
