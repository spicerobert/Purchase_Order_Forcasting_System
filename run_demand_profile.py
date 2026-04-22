"""
依「品號參數」工作表所列品號，從 ERP 取週需求並寫出需求分群（指標 + 建議模式 A–D）。

輸出位置（預設）：
    1) Google 試算表：config.ini [GOOGLE_SHEETS] spreadsheet_url 所指試算表，
       工作表名稱由 profile_sheet 決定（預設「需求分群」）。
    2) 本機 CSV：專案根目錄 demand_profile_export.csv（可用 --csv 改路徑；--no-csv 可略過）。

使用方式：
    uv run run_demand_profile.py              # 試算表 + CSV
    uv run run_demand_profile.py --no-csv     # 僅上傳試算表
    uv run run_demand_profile.py --csv D:/out.csv
    uv run run_demand_profile.py --no-sheets  # 僅本機 CSV，不寫試算表
"""
from __future__ import annotations

import argparse
import configparser
import csv
import logging
import sys
from pathlib import Path

from purchase_order.demand_profile import profile_items, profile_rows_to_matrix
from purchase_order.google_sheets_helper import GoogleSheetsHelper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.ini"


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerows(rows)
    logger.info("已寫入 CSV: %s", path)


def main() -> int:
    parser = argparse.ArgumentParser(description="品號需求分群（日曆淡旺季 + 指標）")
    parser.add_argument("--config", default=CONFIG_FILE, help="設定檔路徑")
    parser.add_argument("--csv", default="", help="另存 CSV 路徑（預設：專案根目錄 demand_profile_export.csv）")
    parser.add_argument("--no-csv", action="store_true", help="不寫入本機 CSV（僅上傳試算表）")
    parser.add_argument("--no-sheets", action="store_true", help="不寫入 Google Sheets")
    args = parser.parse_args()

    print("=" * 72)
    print("  需求分群 — 週需求與日曆旺季指標")
    print("=" * 72)

    try:
        print("\n[步驟 1/3] 讀取 Google Sheets「品號參數」...")
        sheets = GoogleSheetsHelper(config_file=args.config)
        item_params = sheets.read_item_params()
        if not item_params:
            print("[錯誤] 品號參數為空，請先於試算表填入品號。")
            return 1

        codes = [p.get("品號", "").strip() for p in item_params if p.get("品號", "").strip()]
        names = {p["品號"].strip(): p.get("品名", "").strip() for p in item_params if p.get("品號", "").strip()}
        print(f"[成功] 共 {len(codes)} 個品號")

        print("\n[步驟 2/3] 查詢 ERP 週需求並計算分群...")
        rows, baseline = profile_items(codes, names, config_file=args.config)
        print("\n" + baseline + "\n")

        print("[步驟 3/3] 輸出結果...")
        matrix = profile_rows_to_matrix(rows)
        cfg = configparser.ConfigParser()
        cfg.read(args.config, encoding="utf-8")
        sheet_url = ""
        if cfg.has_section("GOOGLE_SHEETS"):
            sheet_url = (cfg["GOOGLE_SHEETS"].get("spreadsheet_url") or "").strip()

        if not args.no_sheets:
            profile_sheet = "需求分群"
            if cfg.has_section("GOOGLE_SHEETS"):
                profile_sheet = cfg["GOOGLE_SHEETS"].get("profile_sheet", profile_sheet).strip() or profile_sheet
            sheets.clear_worksheet(profile_sheet)
            sheets.write_worksheet(profile_sheet, matrix)
            print(f"[成功] 已上傳至 Google 試算表，工作表「{profile_sheet}」")
            if sheet_url:
                print(f"      試算表網址：{sheet_url}")
            print("      （請在瀏覽器開啟同一個試算表即可看到「需求分群」分頁）")

        if not args.no_csv:
            csv_path = args.csv.strip()
            if not csv_path:
                csv_path = str(Path(__file__).resolve().parent / "demand_profile_export.csv")
            _write_csv(Path(csv_path), matrix)
            print(f"[成功] 已寫入本機 CSV：{csv_path}")

        # 終端摘要
        hdr = matrix[0]
        print("\n--- 摘要（建議模式）---")
        mode_idx = hdr.index("建議模式") if "建議模式" in hdr else -1
        if mode_idx >= 0:
            from collections import Counter

            c = Counter(r[mode_idx] for r in matrix[1:])
            for k, v in sorted(c.items(), key=lambda x: (-x[1], x[0])):
                print(f"  {k}: {v} 筆")

        print("\n" + "=" * 72)
        print("[完成]")
        print("=" * 72)
        return 0

    except FileNotFoundError as e:
        print(f"\n[錯誤] 找不到檔案：{e}")
        return 1
    except Exception as e:
        print(f"\n[錯誤] {e}")
        logger.exception("run_demand_profile 失敗")
        return 1


if __name__ == "__main__":
    sys.exit(main())
