# Investment Portfolio MX

A local Python data pipeline for consolidating and analyzing a personal investment portfolio in Mexico.

The project currently supports:

- **GBM** monthly account statements in PDF format
- **CETESDirecto** monthly account statements in PDF format
- **Santander Mexico** monthly account statements in image-based PDF format
- **Multiple GBM contracts**
- Historical GBM statement layouts, including statements before and after the April 2022 format change
- Mexican equities and SIC-listed securities
- CETESDirecto fixed-income instruments and BONDDIA positions
- Santander checking, Dinero Creciente, and Mis Metas balances
- Santander all-page OCR extraction with configuration-aware cached page text
- Contributions, withdrawals, dividends, taxes, purchases, sales, interest, cashback, and other portfolio movements
- Monthly portfolio validation and reconciliation
- Inflation data preparation through INPC
- Preparation of clean analytical tables for a future **Power BI** model

The current objective is to build and validate the complete data layer first. Power BI should be treated as the final presentation layer after GBM, CETESDirecto, Santander, inflation, and inter-account / cross-institution transfers have been normalized and reconciled.

---

## 1. Project Architecture

The intended architecture is:

```text
                         Raw financial statements
                                  |
                +-----------------+-----------------+
                |                 |                 |
                v                 v                 v
               GBM           CETESDirecto       Santander
                |                 |                 |
                +-----------------+-----------------+
                                  |
                                  v
                          Python ETL pipeline
                                  |
                    +-------------+-------------+
                    |                           |
                    v                           v
              Normalized data               Validation
                    |                           |
                    +-------------+-------------+
                                  |
                                  v
                         Source histories
                                  |
                     +------------+------------+
                     |                         |
                     v                         v
              Transfer matching              INPC
                     |                         |
                     +------------+------------+
                                  |
                                  v
                    Consolidated portfolio layer
                                  |
                                  v
                         Analytical tables
                                  |
                                  v
                               Power BI
```

The Python layer is responsible for parsing, cleaning, validating, reconciling, classifying movements, and consolidating the source data.

GBM and CETESDirecto PDFs contain machine-readable text in the formats currently supported. Santander statements currently require OCR because the tested PDFs are image-based.

Power BI should consume already validated tables rather than containing the parsing, OCR, transfer matching, or financial reconciliation logic.

---

## 2. Recommended Folder Structure

```text
investment_portfolio_mx/
|
|-- .config
|-- .gitignore
|-- README.md
|
|-- scripts/
|   |
|   |-- gbm_utils.py
|   |-- gbm_extract_statements.py
|   |-- gbm_validate_statements.py
|   |-- gbm_build_history.py
|   |
|   |-- cetes_utils.py
|   |-- cetes_extract_statements.py
|   |-- cetes_validate_statements.py
|   |-- cetes_build_history.py
|   |
|   |-- santander_utils.py
|   |-- santander_extract_statements.py
|   |-- santander_validate_statements.py
|   |-- santander_build_history.py
|   |
|   |-- download_inpc.py
|   `-- calculate_inflation.py
|
|-- gbm/
|   |
|   |-- edo_cta/
|   |   |-- CB_20215_CONTRACT.pdf
|   |   |-- CB_20223_CONTRACT.pdf
|   |   |-- CB_20224_CONTRACT.pdf
|   |   |-- CB_20261_CONTRACT.pdf
|   |   `-- ...
|   |
|   `-- processed/
|
|-- cetes/
|   |
|   |-- edo_cta/
|   |   |-- 2026_02_Feb.pdf
|   |   |-- 2026_08_Ago.pdf
|   |   `-- ...
|   |
|   `-- processed/
|
|-- santander/
|   |
|   |-- edo_cta/
|   |   |-- 2026_01_Ene.pdf
|   |   |-- 2026_08_Ago.pdf
|   |   `-- ...
|   |
|   `-- processed/
|       `-- ocr_cache/
|
|-- inpc/
|   `-- ...
|
`-- venv/
```

Subdirectories may be created under the raw statement folders if desired. The parsers recursively discover supported PDF files where implemented.

The Santander `ocr_cache/` folder is generated automatically and stores OCR text keyed by PDF content, OCR language, DPI, PSM, and page number. This avoids unnecessary OCR work while preventing stale OCR results from being reused after OCR settings change.

---

## 3. Configuration

Create a `.config` file in the project root.

Example:

```ini
path_gbm_edo_cta = "gbm/edo_cta"
path_gbm_processed = "gbm/processed"

path_cetes_edo_cta = "cetes/edo_cta"
path_cetes_processed = "cetes/processed"

path_santander_edo_cta = "santander/edo_cta"
path_santander_processed = "santander/processed"

contract_validation = "error"

internal_transfer_tolerance_mxn = 1.00
internal_transfer_window_days = 3

santander_ocr_dpi = 180
santander_ocr_psm = 11

# Optional on Windows if Tesseract is not available on PATH:
# path_tesseract_exe = "C:/Program Files/Tesseract-OCR/tesseract.exe"
```

### GBM contract validation

If filenames contain the real GBM contract identifier, use:

```ini
contract_validation = "error"
```

If testing with anonymized filenames such as:

```text
CB_20269_XXX555.pdf
```

use:

```ini
contract_validation = "warn"
```

The GBM parser reads the contract identifier printed inside the PDF and uses it as the canonical contract ID.

### Santander OCR configuration

The Santander statements tested in this project are image-based PDFs. The parser therefore uses Tesseract OCR.

Recommended defaults:

```ini
santander_ocr_dpi = 180
santander_ocr_psm = 11
```

If Tesseract is installed in its standard Windows location, the scripts attempt to find it automatically. Otherwise configure:

```ini
path_tesseract_exe = "C:/Program Files/Tesseract-OCR/tesseract.exe"
```

OCR output is cached under:

```text
santander/processed/ocr_cache/
```

The cache normally does not need to be deleted. Changes to OCR language, DPI, or PSM generate a new cache key automatically. Delete the cache only when you intentionally want to force a complete OCR regeneration.

---

## 4. Python Environment

Python 3.10+ is recommended.

Create a virtual environment:

```powershell
python -m venv venv
```

Activate it in Windows PowerShell:

```powershell
.\venv\Scripts\Activate.ps1
```

Install the Python dependencies:

```powershell
pip install pandas pymupdf pyarrow pytesseract Pillow
```

`pyarrow` is optional but recommended because the pipelines can write Parquet files in addition to CSV.

### Tesseract OCR

Santander parsing requires the **Tesseract OCR application**, not only the `pytesseract` Python package.

After installing Tesseract, verify it from PowerShell:

```powershell
tesseract --version
```

If the command is not available on `PATH`, configure `path_tesseract_exe` in `.config` as shown above.

Santander OCR defaults to `spa+eng` and validates that the configured Tesseract languages are installed before parsing. The parser intentionally does not silently fall back to English-only OCR because that can create plausible-looking but financially incorrect movement data.

---

# 5. GBM Pipeline

## 5.1 Supported GBM statement files

Expected filename format:

```text
CB_YYYYM_Contrato.pdf
```

Examples:

```text
CB_20215_AVZ12345.pdf
CB_20229_AVZ12345.pdf
CB_202210_AVZ12345.pdf
```

Where:

- `YYYY` = four-digit year
- `M` = month
  - `1` through `9` for January through September
  - `10`, `11`, `12` for October through December
- the final token identifies the contract in the filename

The parser also reads the contract number directly from the PDF.

## 5.2 Supported GBM layouts

The GBM parser has been tested against:

- Legacy statements from 2021
- Statements immediately before the April 2022 layout change
- Modern statements from April 2022 onward
- Accounts containing debt/reportos
- Mexican equities under `ACCIONES`
- SIC securities under `ACCIONES DEL SIC`
- Multiple GBM contracts
- Statements where PyMuPDF merges more than one numeric table cell into one extracted text line

The holdings parser combines both:

```text
ACCIONES
ACCIONES DEL SIC
```

into the same normalized holdings table.

This is necessary for cases such as Mexican securities like `GRUMA B`.

---

## 5.3 GBM Execution Order

Always run the GBM pipeline in this order:

```powershell
python scripts\gbm_extract_statements.py
python scripts\gbm_validate_statements.py
python scripts\gbm_build_history.py
```

### Step 1 - Extract

```powershell
python scripts\gbm_extract_statements.py
```

Reads all supported GBM PDFs and creates normalized raw tables.

Typical outputs:

```text
gbm/processed/
|-- statement_summary.csv
|-- holdings.csv
|-- movements.csv
`-- monthly_returns_raw.csv
```

Depending on the data, the extractor may also create error or duplicate reports.

### Step 2 - Validate

```powershell
python scripts\gbm_validate_statements.py
```

Validation checks include:

- Contract/month uniqueness
- Filename contract vs PDF contract
- Filename month vs PDF period
- Parsed holdings vs reported variable-income value
- Monthly return availability
- Missing monthly statements within each contract history

Typical outputs:

```text
gbm/processed/
|-- validation_report.csv
`-- contract_coverage_report.csv
```

Do not proceed with unresolved validation errors.

Small rounding differences are expected in some GBM statements and are handled using a tolerance.

### Step 3 - Build History

```powershell
python scripts\gbm_build_history.py
```

Builds per-contract and consolidated historical tables.

Typical outputs include:

```text
gbm/processed/
|-- gbm_portfolio_monthly.csv
|-- portfolio_consolidated_monthly.csv
|-- dim_contract.csv
|-- movements_classified.csv
|-- internal_transfer_matches.csv
`-- internal_transfer_ambiguous.csv
```

Not every optional file will exist in every run.

---

## 5.4 GBM Internal Transfers

Transfers between owned GBM contracts should not be treated as external contributions or withdrawals at the consolidated portfolio level.

For example:

```text
Contract A  -> -100,000 MXN
Contract B  -> +100,000 MXN
```

The consolidated external flow should be:

```text
0 MXN
```

The GBM history builder attempts to match transfers using:

- Different contracts
- Similar transfer amounts
- A configurable date window

Configuration:

```ini
internal_transfer_tolerance_mxn = 1.00
internal_transfer_window_days = 3
```

Ambiguous transfers are intentionally left unresolved rather than being automatically classified incorrectly.

---

# 6. CETESDirecto Pipeline

## 6.1 Supported CETESDirecto statements

Place monthly CETESDirecto PDF statements under:

```text
cetes/edo_cta/
```

Example:

```text
2026_02_Feb.pdf
2026_08_Ago.pdf
```

The parser reads the actual statement period and CETESDirecto account/CLABE from the PDF rather than depending only on the filename.

The current parser handles portfolio positions such as:

- CETES
- BONOS
- BONDES
- UDIBONOS
- BONDDIA
- ENERFIN
- BPAG28
- BPAG91
- BPA182

It also preserves unknown issuers when they follow the same table structure.

---

## 6.2 CETESDirecto Execution Order

Always run:

```powershell
python scripts\cetes_extract_statements.py
python scripts\cetes_validate_statements.py
python scripts\cetes_build_history.py
```

### Step 1 - Extract

```powershell
python scripts\cetes_extract_statements.py
```

Typical outputs:

```text
cetes/processed/
|-- statement_summary.csv
|-- positions_final.csv
|-- positions_initial.csv
|-- movements.csv
`-- monthly_returns_raw.csv
```

The parser extracts:

- Initial portfolio value
- Final portfolio value
- Initial and final cash
- Total initial and final account value
- Portfolio positions
- Movements
- Contributions and withdrawals
- ISR
- Interest information
- Early-sale results
- BONDDIA/ENERFIN return information
- Gross and net monthly returns

### Step 2 - Validate

```powershell
python scripts\cetes_validate_statements.py
```

Validation checks include:

```text
sum(final positions)
    ~= final portfolio value

sum(initial positions)
    ~= initial portfolio value

portfolio + cash
    ~= total account value

parsed INGEFVO
    ~= reported cash inflows

parsed EGREFVO
    ~= reported cash outflows
```

### Step 3 - Build History

```powershell
python scripts\cetes_build_history.py
```

Typical outputs:

```text
cetes/processed/
|-- cetes_portfolio_monthly.csv
|-- cetes_consolidated_monthly.csv
`-- dim_cetes_account.csv
```

The monthly history includes:

- Previous account value
- Current account value
- Contributions
- Withdrawals
- Net external flows
- Cumulative contributions
- Cumulative withdrawals
- Cumulative net contributed capital
- Reported net return

---

# 7. Movement Classification

The ETL distinguishes portfolio activity from actual changes in invested capital.

Examples:

```text
Salary -> investment account
    = external contribution

Investment account -> bank account
    = external withdrawal

GBM contract A -> GBM contract B
    = internal transfer

Santander -> CETESDirecto
    = cross-institution transfer candidate

CETESDirecto -> Santander
    = cross-institution transfer candidate

CETESDirecto -> GBM
    = cross-institution transfer candidate

GBM -> Santander
    = cross-institution transfer candidate
```

This distinction is essential because portfolio growth must not be overstated by counting transfers between owned accounts as new capital.

---

# 8. Inflation / INPC

The project already reserves an `inpc/` area and may use:

```text
scripts/download_inpc.py
scripts/calculate_inflation.py
```

The intended final calculation is not simply:

```text
current portfolio value - nominal deposits
```

Each external contribution should eventually be adjusted from its contribution date to the analysis date using INPC.

Conceptually:

```text
inflation-adjusted contribution
    =
original contribution
    x
current INPC / contribution-date INPC
```

This will allow the project to calculate:

- Nominal contributed capital
- Inflation-adjusted contributed capital
- Nominal gain
- Real gain
- Real gain percentage

---

# 9. Santander Pipeline

## 9.1 Santander Statement Characteristics

Place monthly Santander PDF statements under:

```text
santander/edo_cta/
```

Example filenames:

```text
2026_01_Ene.pdf
2026_08_Ago.pdf
```

The filename is primarily organizational. The parser reads the statement period from the PDF itself.

The Santander statements tested so far are **image-only PDFs**. Because no reliable embedded text is available, the parser renders **all statement pages** and runs Tesseract OCR. Processing all pages is necessary because longer statements can extend beyond page 5.

The tested Santander statement layout contains multiple products in the same monthly statement, including:

- `SUPERCUENTA CHEQUES-PAGO FIJO`
- `DINERO CRECIENTE SANTANDER`
- `MIS METAS SANTANDER`

These are modeled as separate account/subaccount records rather than as one undifferentiated balance.

---

## 9.2 Santander Execution Order

Current tested Santander parser version: `2026-11-08.5`.

Always run the Santander pipeline in this order:

```powershell
python scripts\santander_extract_statements.py
python scripts\santander_validate_statements.py
python scripts\santander_build_history.py
```

### Step 1 - Extract

```powershell
python scripts\santander_extract_statements.py
```

Typical outputs:

```text
santander/processed/
|-- statement_summary.csv
|-- account_snapshots.csv
|-- movements.csv
|-- parse_errors.csv
`-- ocr_cache/
```

`parse_errors.csv` is created only when parsing errors occur.

The extractor captures:

- Statement start and end dates
- Previous total Santander balance
- Current total Santander balance
- Product/subaccount balances
- Reported deposits and withdrawals
- Average balances where available
- Dinero Creciente interest summary
- Checking-account transactions
- Dinero Creciente transactions
- Transaction balances
- Transaction descriptions and folios
- Counterparty bank hints when OCR provides them
- Internal Santander transfer candidates
- External transfer candidates

The movement parser uses the transaction amount and description as the primary evidence for direction, then validates the result against the running balance equation:

```text
previous balance + signed transaction amount
    ~= new balance
```

When OCR damages the printed balance but the transaction amount and direction are reliable, the parser reconstructs the resulting balance instead of creating a false large movement.

The parser also normalizes known OCR artifacts observed in Santander statements, including:

- `AG0` instead of `AGO`
- dates and folio/description emitted on separate OCR lines
- punctuation attached to monetary values
- thousands separators read as periods
- damaged balances that can be safely reconstructed from validated movement amounts

---

## 9.3 Santander Movement Classification

The Santander parser classifies movements into categories such as:

```text
interest
tax_withholding
cashback
payroll
credit_card_payment
loan_principal_payment
loan_interest_payment
service_payment
santander_internal_transfer
transfer_in
transfer_out
other_inflow
other_outflow
```

It also assigns a preliminary flow scope.

Examples:

```text
Checking -> Dinero Creciente
    = internal Santander transfer

Dinero Creciente -> Checking
    = internal Santander transfer

Interest credit
    = earnings

ISR withholding / RETENCION
    = tax

Cashback
    = earnings

External SPEI received
    = external candidate

External SPEI sent
    = external candidate
```

Internal Santander transfers are matched using:

- Same date
- Same folio
- Opposite signed amount
- Different Santander subaccounts

Matched pairs receive:

```text
flow_scope = internal_santander
```

and are not treated as external contributed capital.

---

## 9.4 Cross-Institution Transfer Candidates

Transfers involving other institutions cannot be finalized correctly using Santander data alone.

For example:

```text
CETESDirecto -> Santander
GBM -> Santander
Santander -> CETESDirecto
Santander -> GBM
```

may look like external deposits or withdrawals when viewing Santander in isolation, but they are internal transfers when the full owned portfolio is consolidated.

The Santander history builder therefore creates:

```text
santander/processed/cross_institution_transfer_candidates.csv
```

for transfer movements that should later be reconciled against GBM and CETESDirecto.

This conservative design prevents the Santander parser from incorrectly treating owned-account transfers as new wealth.

---

## 9.5 Santander Validation

Run:

```powershell
python scripts\santander_validate_statements.py
```

Typical output:

```text
santander/processed/validation_report.csv
```

Validation includes:

```text
sum(opening subaccount balances)
    ~= reported Santander previous total

sum(closing subaccount balances)
    ~= reported Santander current total

opening balance + reported deposits - reported withdrawals
    ~= closing balance

parsed transaction deposits
    ~= reported account deposits

parsed transaction withdrawals
    ~= reported account withdrawals

parsed Dinero Creciente gross interest
    ~= reported gross interest

parsed ISR withholding
    ~= reported ISR

parsed net interest
    ~= reported net interest

where:

net interest
    = gross interest - ISR withholding
```

This separation is important because ISR withholding is a tax movement, not an external withdrawal from the portfolio.

All currently tested Santander monthly statements from **January through August 2026** pass the implemented validation checks.

OCR-derived data should not be used downstream until these reconciliations pass.

---

## 9.6 Santander Monthly History

Run:

```powershell
python scripts\santander_build_history.py
```

Typical outputs:

```text
santander/processed/
|-- santander_account_monthly.csv
|-- santander_consolidated_monthly.csv
|-- dim_santander_account.csv
`-- cross_institution_transfer_candidates.csv
```

`dim_santander_account.csv` maps Santander products into broad account classes such as:

```text
CHECKING      -> cash
DINERO_CREC   -> savings
MIS_METAS     -> savings
```

The current Santander builder intentionally uses names such as:

```text
external_contribution_candidate_mxn
external_withdrawal_candidate_mxn
net_external_flow_candidate_mxn
```

because those flows are not final until cross-institution transfer matching is performed.

The monthly history also tracks tax-aware Santander earnings fields such as:

```text
earnings_mxn
tax_withheld_mxn
net_earnings_after_tax_mxn
```

This prevents ISR withholding from being misclassified as an external portfolio withdrawal.

---

# 10. Future Common Data Model

The final source-independent model should converge toward tables such as:

## Account snapshots

```text
date
institution
account_id
asset_class
value_mxn
```

## External and internal flows

```text
date
source_account
destination_account
amount_mxn
flow_scope
```

## Positions

```text
date
institution
account_id
instrument
quantity
market_value_mxn
```

## Account dimension

```text
account_id
institution
account_name
account_type
include_in_portfolio
```

Possible account types include:

```text
investment
fixed_income
savings
emergency_fund
operating_cash
```

---

# 11. Power BI

Power BI is intentionally the final step.

The goal is to provide Power BI with validated analytical tables rather than raw PDFs.

The final reporting model should support three time granularities:

### Historical years

```text
2021
2022
2023
2024
2025
2026
```

Use year-end portfolio snapshots.

### Current year

```text
Jan
Feb
Mar
...
Dec
```

Use month-end portfolio snapshots.

### Current month

Use daily portfolio valuations.

The final dashboard is expected to compare:

- Total portfolio value
- Nominal cumulative contributions
- Inflation-adjusted contributions
- Nominal gain
- Real gain
- Inflation
- Investment returns

Potential performance metrics include:

- Time-Weighted Return (TWR)
- XIRR
- Modified Dietz return
- Real return after inflation

---

# 12. Recommended Development Workflow

When adding new statement files:

```text
1. Copy PDFs into the corresponding edo_cta folder
2. Run the extractor
3. Run validation
4. Review warnings/errors
5. Fix parser issues before continuing
6. Run the history builder
7. Reconcile totals against the original statements
8. Commit code only after validation passes
```

Do not silently ignore parser discrepancies.

The source statement remains the authoritative reconciliation reference.

---

# 13. Running the Existing Pipelines

From the project root:

```powershell
.\venv\Scripts\Activate.ps1
```

Run GBM:

```powershell
python scripts\gbm_extract_statements.py
python scripts\gbm_validate_statements.py
python scripts\gbm_build_history.py
```

Run CETESDirecto:

```powershell
python scripts\cetes_extract_statements.py
python scripts\cetes_validate_statements.py
python scripts\cetes_build_history.py
```

Run Santander:

```powershell
python scripts\santander_extract_statements.py
python scripts\santander_validate_statements.py
python scripts\santander_build_history.py
```

A recommended full refresh sequence is therefore:

```powershell
python scripts\gbm_extract_statements.py
python scripts\gbm_validate_statements.py
python scripts\gbm_build_history.py

python scripts\cetes_extract_statements.py
python scripts\cetes_validate_statements.py
python scripts\cetes_build_history.py

python scripts\santander_extract_statements.py
python scripts\santander_validate_statements.py
python scripts\santander_build_history.py
```

Only after all three source pipelines pass validation should their outputs be used by the cross-institution consolidation layer. The currently tested GBM, CETESDirecto, and Santander source parsers are intended to remain source-specific; cross-institution transfer resolution belongs in the common consolidation layer.

---

# 14. Git / Privacy

Financial statements contain sensitive personal and financial data and should not be committed to Git.

Recommended `.gitignore` entries:

```gitignore
# Virtual environment
venv/
.venv/

# Python
__pycache__/
*.pyc

# Local configuration
.config
.env

# Raw financial statements
gbm/edo_cta/
cetes/edo_cta/
santander/edo_cta/

# Generated data
gbm/processed/
cetes/processed/
santander/processed/

# Downloaded/raw INPC files where applicable
inpc/*.zip

# Santander OCR cache contains statement-derived text
santander/processed/ocr_cache/

# IDE
.vscode/
```

If VS Code project settings are intentionally shared, remove `.vscode/` from the ignore list and commit only safe settings.

---

# 15. Current Project Status

| Component | Status |
|---|---|
| GBM PDF extraction | Implemented |
| GBM legacy statement support | Implemented |
| GBM modern statement support | Implemented |
| Multiple GBM contracts | Implemented |
| GBM domestic equities | Implemented |
| GBM SIC positions | Implemented |
| GBM validation | Implemented |
| GBM monthly consolidation | Implemented |
| CETESDirecto extraction | Implemented |
| CETESDirecto validation | Implemented |
| CETESDirecto monthly consolidation | Implemented |
| Santander OCR extraction | Implemented |
| Santander account/subaccount parsing | Implemented |
| Santander all-page OCR / cache invalidation | Implemented |
| Santander internal transfer matching | Implemented |
| Santander validation | Implemented — Jan–Aug 2026 tested statements pass |
| Santander monthly consolidation | Implemented |
| Santander cross-institution transfer candidate export | Implemented |
| INPC ingestion | In progress / project-specific |
| Cross-institution GBM / CETES / Santander matching | Planned |
| Real return vs inflation | Planned |
| Power BI model | Final phase |

---

## Design Principle

**Parse first, validate second, reconcile transfers third, consolidate fourth, visualize last.**

The reliability of the final Power BI dashboard depends on the quality and reconciliation of the underlying financial data.
