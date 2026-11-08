# Investment Portfolio MX

A local Python data pipeline for consolidating and analyzing a personal investment portfolio in Mexico.

The project currently supports:

- **GBM** monthly account statements in PDF format
- **CETESDirecto** monthly account statements in PDF format
- **Multiple GBM contracts**
- Historical GBM statement layouts, including statements before and after the April 2022 format change
- Mexican equities and SIC-listed securities
- CETESDirecto fixed-income instruments and BONDDIA positions
- Contributions, withdrawals, dividends, taxes, purchases, sales, and other portfolio movements
- Monthly portfolio validation and reconciliation
- Inflation data preparation through INPC
- Preparation of clean analytical tables for a future **Power BI** model

The current objective is to build and validate the complete data layer first. Power BI should be treated as the final presentation layer after GBM, CETESDirecto, Santander, inflation, and inter-account transfers have been normalized and reconciled.

---

## 1. Project Architecture

The intended architecture is:

```text
Raw financial statements
        |
        +-------------------+
        |                   |
        v                   v
       GBM              CETESDirecto
        |                   |
        +---------+---------+
                  |
                  v
          Python ETL pipeline
                  |
        +---------+---------+
        |                   |
        v                   v
   Normalized data      Validation
        |                   |
        +---------+---------+
                  |
                  v
       Consolidated portfolio
                  |
            +-----+------+
            |            |
            v            v
           INPC       Future Santander
            |            |
            +-----+------+
                  |
                  v
            Analytical tables
                  |
                  v
               Power BI
```

The Python layer is responsible for parsing, cleaning, validating, reconciling, and consolidating the source data.

Power BI should consume already validated tables rather than containing the parsing or financial reconciliation logic.

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
|-- inpc/
|   `-- ...
|
`-- venv/
```

Subdirectories may be created under `gbm/edo_cta/` if desired. The GBM parser performs recursive discovery of supported statement files.

---

## 3. Configuration

Create a `.config` file in the project root.

Example:

```ini
path_gbm_edo_cta = "gbm/edo_cta"
path_gbm_processed = "gbm/processed"

path_cetes_edo_cta = "cetes/edo_cta"
path_cetes_processed = "cetes/processed"

contract_validation = "error"

internal_transfer_tolerance_mxn = 1.00
internal_transfer_window_days = 3
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

The parser reads the contract identifier printed inside the PDF and uses it as the canonical contract ID.

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

Install the main dependencies:

```powershell
pip install pandas pymupdf pyarrow
```

`pyarrow` is optional but recommended because the pipelines can write Parquet files in addition to CSV.

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
    = future cross-institution internal transfer

CETESDirecto -> GBM
    = future cross-institution internal transfer
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

# 9. Future Santander Integration

Santander should be integrated before the final Power BI model.

The future Santander pipeline should normalize:

- Monthly balances
- Deposits
- Withdrawals
- Transfers
- Interest
- Account classification

The final consolidated model must identify transfers such as:

```text
Santander -> GBM
Santander -> CETESDirecto
CETESDirecto -> Santander
GBM -> Santander
```

as internal movements when both accounts belong to the analyzed portfolio.

This prevents double counting contributions and withdrawals.

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

# 13. Running Both Existing Pipelines

From the project root:

```powershell
.\venv\Scripts\Activate.ps1
```

Then run GBM:

```powershell
python scripts\gbm_extract_statements.py
python scripts\gbm_validate_statements.py
python scripts\gbm_build_history.py
```

Then CETESDirecto:

```powershell
python scripts\cetes_extract_statements.py
python scripts\cetes_validate_statements.py
python scripts\cetes_build_history.py
```

After both pipelines pass validation, the resulting monthly tables can be used by the future cross-institution consolidation layer.

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

# Generated data
gbm/processed/
cetes/processed/

# Downloaded/raw INPC files where applicable
inpc/*.zip

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
| INPC ingestion | In progress / project-specific |
| Santander integration | Planned |
| Cross-institution transfer matching | Planned |
| Real return vs inflation | Planned |
| Power BI model | Final phase |

---

## Design Principle

**Parse first, validate second, consolidate third, visualize last.**

The reliability of the final Power BI dashboard depends on the quality and reconciliation of the underlying financial data.
