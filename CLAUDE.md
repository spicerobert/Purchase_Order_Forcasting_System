# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Purchase Order Forecasting System（採購預測系統）— a Python application that reads item parameters from Google Sheets, queries an MS-SQL ERP database for historical demand and live inventory, applies statistical demand classification and safety stock formulas, then writes recommended purchase quantities back to Google Sheets.

**Project benefits**: Automates inventory replenishment decisions by combining 52-week demand history with real-time ERP stock data, classifying each item's demand pattern (stable / seasonal / inconsistent / intermittent) and calculating safety stock, reorder point, and suggested order quantity — eliminating manual spreadsheet work and reducing both stockouts and overstock.

---

## Commands

This project uses [`uv`](https://docs.astral.sh/uv/) as the package manager.

```bash
# Install dependencies
uv sync

# Full forecasting run (primary daily workflow)
uv run main.py

# Initialize Google Sheets worksheets (one-time setup)
uv run setup_sheets.py

# Demand segmentation analysis, outputs to Sheets + CSV
uv run run_demand_profile.py
uv run run_demand_profile.py --no-csv
uv run run_demand_profile.py --csv D:/out.csv
uv run run_demand_profile.py --no-sheets

# Quick inventory check (no calculations)
uv run query_inventory.py
```

No formal test suite exists; verification is done visually in Google Sheets or the terminal output.

---

## Configuration

Two files are required and **not committed to version control**:

| File | Purpose |
|------|---------|
| `config.ini` | All runtime parameters (copy from `config.ini.template`) |
| `service_account.json` | Google Cloud service account credentials |

Key `config.ini` sections:

- **`[ERP_DATABASE]`** — MS-SQL Server connection (leave username/password blank for Windows auth)
- **`[INVENTORY]`** — `warehouse_codes` list (28 default codes; overrides the hardcoded default)
- **`[GOOGLE_SHEETS]`** — `spreadsheet_url`, `credentials_file`, worksheet names (`品號參數` / `進貨建議` / `需求分群`)
- **`[SEASON_ANCHORS]`** — `peak_cycle_pairs` (Mid-Autumn ↔ CNY date pairs), `days_before_mid_autumn`
- **`[DEMAND_PROFILE]`** — Classification thresholds (seasonality ratio, CV ceiling, zero-week ratio, intermittent mean)
- **`[SAFETY_STOCK]`** — `service_level`, `avg_lead_time_weeks`, `std_lead_time_weeks`, `order_cycle_weeks`, `history_weeks`, overstock settings

---

## Architecture

### Module Layers

```
Entry Points (scripts)
  main.py · setup_sheets.py · run_demand_profile.py · query_inventory.py
        │
        ├─ google_sheets_helper.py   ← bidirectional Sheets I/O (gspread)
        │
        └─ safety_stock.py           ← calculation orchestrator
              ├─ demand_profile.py   ← demand classification (A/B/C/D)
              ├─ inventory_query.py  ← stock + transit aggregation
              └─ erp_db_helper.py    ← raw MS-SQL queries (pyodbc)
```

### ERP Tables

| Table | Contents |
|-------|----------|
| `INVMB` | Item master (name, unit, pack unit, pack qty) |
| `INVLC` | Period balances by warehouse |
| `INVLA` | Stock transactions (direction `LA005`: +1 in / −1 out) |
| `PURTD` | Purchase order details (confirmed, not yet closed/received) |

### Primary Data Flow (`main.py`)

1. **Read** item parameters from Google Sheets `品號參數` (item code, service level, lead time, MOQ, mode)
2. **Query ERP** — 52-week weekly demand (`INVLA`), current stock (`INVLC` + `INVLA`), in-transit POs (`PURTD`)
3. **Classify demand** per item into one of four modes:
   - **A** — stable year-round (CV ≤ 0.5, peak/off ratio ≈ 1)
   - **B** — calendar-driven seasonal (peak/off ratio ≥ 1.5, MA peak aligns with anchor dates)
   - **C** — inconsistent (detectable peaks but not calendar-aligned)
   - **D** — intermittent / low-frequency (≥ 35% zero-demand weeks or mean ≤ 3 units/week)
4. **Resolve demand D** — pick peak or off-season average (B), annual average (A/C), or trend-adjusted annual average (D) based on current calendar week
5. **Apply trend adjustment** — `adj = near4avg / prev4avg` clamped to `[0.5, 1.5]`; `d_for_ss = D × adj`
6. **Calculate**:
   - `SS = Z × √(STD² × L + STD_L² × d²)` (handles both demand and lead-time variability)
   - `ROP = d × L + SS`
   - `Suggested = max(0, (L + T) × d + SS − stock)` rounded up to MOQ
7. **Generate advice** — `庫存0` / `需要進貨` / `庫存偏高` / blank
8. **Write** 22-column results to Google Sheets `進貨建議`

### Key Design Decisions

- `ERPDBHelper` is a context manager (`with ERPDBHelper(...) as db`) — always use it this way to ensure the connection closes
- ODBC driver is auto-detected (prefers Driver 18 → 17 → 13)
- Demand classification thresholds all come from `config.ini [DEMAND_PROFILE]`; code has sensible fallback defaults
- Items marked `進貨模式 = 有單才進` (make-to-order) skip SS/ROP calculations entirely and receive advice `依訂單`
- Transit inventory unit conversion: purchase order unit → stock unit via `INVMB.MB073` (pack_qty)
