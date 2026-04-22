"""
庫存查詢入口腳本
執行方式：uv run query_inventory.py
"""
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 確保套件路徑正確
sys.path.insert(0, str(Path(__file__).parent))

from purchase_order.inventory_query import InventoryQuery


def main():
    config_file = Path(__file__).parent / "config.ini"

    if not config_file.exists():
        print(f"錯誤：找不到設定檔 {config_file}")
        print("請複製 config.ini.template 為 config.ini 並填入資料庫連線資訊")
        sys.exit(1)

    print("連線至 ERP 資料庫中...")
    query = InventoryQuery(config_file=str(config_file))
    query.print_inventory_summary()


if __name__ == "__main__":
    main()
