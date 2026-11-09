from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pymupdf
import pytesseract
from PIL import Image


SPANISH_MONTHS = {
    "ENE": 1, "ENERO": 1,
    "FEB": 2, "FEBRERO": 2,
    "MAR": 3, "MARZO": 3,
    "ABR": 4, "ABRIL": 4,
    "MAY": 5, "MAYO": 5,
    "JUN": 6, "JUNIO": 6,
    "JUL": 7, "JULIO": 7,
    "AGO": 8, "AGOSTO": 8,
    "SEP": 9, "SEPT": 9, "SEPTIEMBRE": 9,
    "OCT": 10, "OCTUBRE": 10,
    "NOV": 11, "NOVIEMBRE": 11,
    "DIC": 12, "DICIEMBRE": 12,
}

MONEY_RE = re.compile(r"(?<!\d)(-?\d{1,3}(?:,\d{3})*\.\d{2})(?!\d)")
ACCOUNT_RE = re.compile(r"\b\d{2}-\d{8}-\d\b")
DATE_RE = re.compile(
    # Tesseract sometimes confuses the final "O" in AGO with zero: AG0.
    r"\b(?P<day>\d{2})-(?P<month>[A-ZÁÉÍÓÚ0]{3})-(?P<year>\d{4})\b",
    re.IGNORECASE,
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: str | Path | None = None) -> dict[str, str]:
    config_path = Path(path) if path else project_root() / ".config"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Create .config in the project root."
        )

    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
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


def config_int(config: dict[str, str], key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def config_float(config: dict[str, str], key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    return path.resolve()


def discover_statements(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"Santander statements folder not found: {folder}")

    return sorted(
        (p for p in folder.rglob("*.pdf") if p.is_file()),
        key=lambda p: str(p.resolve()).lower(),
    )


def normalize_text(value: str) -> str:
    replacements = {
        "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U", "Ü": "U", "Ñ": "N",
        "é": "e", "á": "a", "í": "i", "ó": "o", "ú": "u", "ñ": "n",
    }
    text = value.strip()
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = text.upper()
    return re.sub(r"\s+", " ", text)


def parse_money(value: str) -> float:
    return float(value.replace(",", "").replace("$", "").strip())


def money_values(text: str) -> list[float]:
    return [parse_money(x) for x in MONEY_RE.findall(text)]


def parse_spanish_date(value: str) -> date:
    match = DATE_RE.search(normalize_text(value))
    if not match:
        raise ValueError(f"Could not parse Santander date: {value}")

    # Common OCR correction: AG0 -> AGO.
    month_token = match.group("month").upper().replace("0", "O")
    month = SPANISH_MONTHS.get(month_token)
    if month is None:
        raise ValueError(
            f"Unknown Santander month abbreviation: {match.group('month')}"
        )

    return date(int(match.group("year")), month, int(match.group("day")))


def extract_statement_period(text: str) -> tuple[date, date]:
    normalized = normalize_text(text)
    match = re.search(
        r"PERIODO\s+DEL\s+(\d{2}-[A-Z]{3}-\d{4})\s+AL\s+(\d{2}-[A-Z]{3}-\d{4})",
        normalized,
    )
    if not match:
        raise ValueError("Could not locate Santander statement period.")

    return parse_spanish_date(match.group(1)), parse_spanish_date(match.group(2))


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def configure_tesseract(config: dict[str, str]) -> None:
    configured = config.get("path_tesseract_exe")
    if configured:
        exe = Path(configured).expanduser()
        if not exe.exists():
            raise FileNotFoundError(f"Tesseract executable not found: {exe}")
        pytesseract.pytesseract.tesseract_cmd = str(exe)
        return

    if shutil.which("tesseract"):
        return

    common_windows = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if common_windows.exists():
        pytesseract.pytesseract.tesseract_cmd = str(common_windows)
        return

    raise RuntimeError(
        "Tesseract OCR is required because Santander PDFs are image-based. "
        "Install Tesseract or set path_tesseract_exe in .config."
    )


def available_ocr_language(config: dict[str, str]) -> str:
    """
    Return the OCR language requested for Santander statements.

    Santander statements are Spanish-language financial documents. Falling
    back silently to English can produce plausible-looking but financially
    incorrect OCR results, especially in dense movement tables.

    Default:
        santander_ocr_language = "spa+eng"

    The function validates that every requested Tesseract language is installed.
    """
    requested = config.get("santander_ocr_language", "spa+eng").strip()

    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        raise RuntimeError(
            "Could not query installed Tesseract languages."
        ) from exc

    required = [part.strip() for part in requested.split("+") if part.strip()]
    missing = [lang for lang in required if lang not in installed]

    if missing:
        raise RuntimeError(
            "Santander OCR requires the configured Tesseract language(s), but "
            f"these are missing: {', '.join(missing)}. "
            f"Installed languages: {', '.join(sorted(installed))}. "
            "Install the Spanish language pack (spa) before parsing Santander "
            "statements, or explicitly change santander_ocr_language in .config."
        )

    return requested


def pdf_has_embedded_text(path: str | Path) -> bool:
    doc = pymupdf.open(path)
    try:
        return any(page.get_text("text").strip() for page in doc)
    finally:
        doc.close()


def extract_pdf_pages(
    path: str | Path,
    *,
    config: dict[str, str],
    cache_dir: str | Path,
    max_pages: int | None = None,
) -> list[str]:
    """
    Return page text.

    Santander statements in the known layout are image-only, so OCR is used
    when no embedded text is available. By default every page is processed;
    this is required because long monthly statements can span 9+ pages.
    OCR output is cached by PDF SHA-256 plus OCR language/DPI/PSM and page
    number to avoid stale-cache reuse.
    """
    path = Path(path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(path)
    try:
        page_count = len(doc) if max_pages is None else min(max_pages, len(doc))
        has_text = any(doc[i].get_text("text").strip() for i in range(page_count))

        if has_text:
            return [doc[i].get_text("text") for i in range(page_count)]

        configure_tesseract(config)
        lang = available_ocr_language(config)
        dpi = config_int(config, "santander_ocr_dpi", 180)
        psm = config_int(config, "santander_ocr_psm", 11)
        file_hash = file_sha256(path)
        matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)

        pages: list[str] = []
        for page_index in range(page_count):
            # Include OCR parameters in the cache key. This prevents stale
            # English-only OCR (or old DPI/PSM settings) from being reused after
            # the OCR configuration changes.
            cache_signature = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "_",
                f"lang-{lang}_dpi-{dpi}_psm-{psm}_v2",
            )
            cache_file = cache_dir / (
                f"{file_hash}_{cache_signature}_p{page_index + 1}.txt"
            )
            if cache_file.exists():
                pages.append(cache_file.read_text(encoding="utf-8"))
                continue

            pix = doc[page_index].get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            text = pytesseract.image_to_string(
                image,
                lang=lang,
                config=f"--psm {psm}",
            )
            cache_file.write_text(text, encoding="utf-8")
            pages.append(text)

        return pages
    finally:
        doc.close()


def extract_account_number(text: str) -> str | None:
    match = ACCOUNT_RE.search(text)
    return match.group(0) if match else None


def account_key(product_type: str, account_number: str | None, ordinal: int = 1) -> str:
    if account_number:
        return account_number
    return f"{product_type}_{ordinal}"


def write_table(df, output_base: str | Path) -> None:
    output_base = Path(output_base)
    output_base.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(
        output_base.with_suffix(".csv"),
        index=False,
        encoding="utf-8-sig",
    )

    try:
        df.to_parquet(output_base.with_suffix(".parquet"), index=False)
    except (ImportError, ModuleNotFoundError):
        pass
