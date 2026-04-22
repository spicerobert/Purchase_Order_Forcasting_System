"""
庫存查詢模組
提供即時庫存查詢介面，整合現有庫存與在途庫存，作為採購建議量計算的資料來源
"""
import logging
from collections import defaultdict
from typing import Dict, List

from .erp_db_helper import ERPDBHelper

logger = logging.getLogger(__name__)


class InventoryQuery:
    """
    庫存查詢高階介面

    整合 ERPDBHelper，提供：
    - 現有庫存查詢（依品號彙總或含倉庫明細）
    - 在途庫存查詢（已確認採購單但尚未到貨）
    - 合計庫存（現有 + 在途）
    """

    def __init__(self, config_file: str = "config.ini"):
        """
        Args:
            config_file: 設定檔路徑（預設 config.ini）
        """
        self._config_file = config_file

    # -------------------------------------------------------------------------
    # 內部工具方法
    # -------------------------------------------------------------------------

    @staticmethod
    def _enrich(rows: List[Dict], item_info: Dict) -> None:
        """
        將品名與單位注入各筆資料列（in-place）

        Args:
            rows:      erp_db_helper 回傳的原始列表
            item_info: get_item_info() 回傳的品號主檔字典
        """
        for row in rows:
            info = item_info.get(row["item_code"], {})
            row["item_name"] = info.get("item_name", "")
            row["unit"]      = info.get("unit", "")

    @staticmethod
    def _convert_transit_unit(rows: List[Dict], item_info: Dict) -> None:
        """
        將在途庫存的訂貨單位轉換為庫存單位（in-place）

        轉換規則：
        - TD009（order_unit）== MB004（庫存單位）→ 不需轉換
        - TD009（order_unit）== MB016（包裝單位）→ transit_qty × MB073（外包裝含商品數）
        - 其他情況                               → 記錄警告，數量維持原值

        Args:
            rows:      get_transit_inventory() 回傳的原始列表（含 order_unit）
            item_info: get_item_info() 回傳的品號主檔字典（含 pack_unit, pack_qty）
        """
        for row in rows:
            code       = row["item_code"]
            order_unit = row.get("order_unit", "")
            info       = item_info.get(code, {})
            stock_unit = info.get("unit", "")
            pack_unit  = info.get("pack_unit", "")
            pack_qty   = info.get("pack_qty", 1.0)

            if order_unit == stock_unit or not order_unit:
                pass  # 單位相同，無需轉換
            elif order_unit == pack_unit and pack_qty > 0:
                row["transit_qty"] *= pack_qty
                logger.debug(
                    f"品號 {code}：訂貨單位 [{order_unit}] 轉換為庫存單位 [{stock_unit}]，"
                    f"× {pack_qty}，在途數量調整為 {row['transit_qty']}"
                )
            else:
                logger.warning(
                    f"品號 {code}：訂貨單位 [{order_unit}] 與庫存單位 [{stock_unit}] "
                    f"及包裝單位 [{pack_unit}] 均不符，在途數量維持原值"
                )

    @staticmethod
    def _aggregate_stock(rows: List[Dict]) -> Dict[str, dict]:
        """依品號加總現有庫存（須先呼叫 _enrich）"""
        result: Dict[str, dict] = {}
        for row in rows:
            code = row["item_code"]
            if code not in result:
                result[code] = {
                    "item_code": code,
                    "item_name": row.get("item_name", ""),
                    "unit":      row.get("unit", ""),
                    "stock_qty": 0.0,
                }
            result[code]["stock_qty"] += row["qty"]
        return result

    @staticmethod
    def _aggregate_transit(rows: List[Dict]) -> Dict[str, float]:
        """依品號加總在途庫存數量"""
        result: Dict[str, float] = defaultdict(float)
        for row in rows:
            result[row["item_code"]] += row["transit_qty"]
        return result

    # -------------------------------------------------------------------------
    # 公開查詢方法
    # -------------------------------------------------------------------------

    def get_stock_inventory(self, by_warehouse: bool = False) -> List[Dict]:
        """
        查詢現有庫存（不含在途）

        Args:
            by_warehouse: True 回傳各倉庫明細；False（預設）依品號加總

        Returns:
            by_warehouse=False 時，格式:
            [{'item_code', 'item_name', 'qty', 'unit'}, ...]

            by_warehouse=True 時，格式:
            [{'item_code', 'item_name', 'warehouse_code', 'qty', 'unit'}, ...]
        """
        with ERPDBHelper(self._config_file) as db:
            rows      = db.get_inventory()
            item_info = db.get_item_info([r["item_code"] for r in rows])

        self._enrich(rows, item_info)

        if by_warehouse:
            return rows

        aggregated = self._aggregate_stock(rows)
        result = sorted(
            [
                {
                    "item_code": info["item_code"],
                    "item_name": info["item_name"],
                    "qty":       info["stock_qty"],
                    "unit":      info["unit"],
                }
                for info in aggregated.values()
            ],
            key=lambda x: x["item_code"],
        )
        logger.info(f"現有庫存查詢完成，共 {len(result)} 個品號")
        return result

    def get_transit_inventory(self, by_warehouse: bool = False) -> List[Dict]:
        """
        查詢在途庫存（已確認採購單但尚未到貨）

        Args:
            by_warehouse: True 回傳各倉庫明細；False（預設）依品號加總

        Returns:
            by_warehouse=False 時，格式:
            [{'item_code', 'item_name', 'transit_qty', 'unit'}, ...]

            by_warehouse=True 時，格式:
            [{'item_code', 'item_name', 'warehouse_code', 'transit_qty', 'unit'}, ...]
        """
        with ERPDBHelper(self._config_file) as db:
            rows      = db.get_transit_inventory()
            item_info = db.get_item_info([r["item_code"] for r in rows])

        self._convert_transit_unit(rows, item_info)
        self._enrich(rows, item_info)

        if by_warehouse:
            return rows

        transit = self._aggregate_transit(rows)
        # 取得品名單位（從已注入的 rows 中）
        info_map = {r["item_code"]: r for r in rows}
        result = sorted(
            [
                {
                    "item_code":   code,
                    "item_name":   info_map.get(code, {}).get("item_name", ""),
                    "transit_qty": qty,
                    "unit":        info_map.get(code, {}).get("unit", ""),
                }
                for code, qty in transit.items()
            ],
            key=lambda x: x["item_code"],
        )
        logger.info(f"在途庫存查詢完成，共 {len(result)} 個品號")
        return result

    def get_inventory(self, by_warehouse: bool = False) -> List[Dict]:
        """
        查詢合計庫存（現有庫存 + 在途庫存），共用單一資料庫連線

        品名與單位統一查詢一次 INVMB，涵蓋所有品號。

        Args:
            by_warehouse: True 回傳各倉庫明細；False（預設）依品號加總

        Returns:
            by_warehouse=False 時，格式:
            [
                {
                    'item_code', 'item_name', 'unit',
                    'stock_qty',    # 現有庫存
                    'transit_qty',  # 在途庫存
                    'total_qty'     # 合計
                },
                ...
            ]

            by_warehouse=True 時，格式:
            [{'item_code', 'item_name', 'warehouse_code', 'stock_qty',
              'transit_qty', 'total_qty', 'unit'}, ...]
        """
        with ERPDBHelper(self._config_file) as db:
            stock_rows   = db.get_inventory()
            transit_rows = db.get_transit_inventory()

            all_codes = list(
                {r["item_code"] for r in stock_rows} |
                {r["item_code"] for r in transit_rows}
            )
            item_info = db.get_item_info(all_codes)

        self._convert_transit_unit(transit_rows, item_info)
        self._enrich(stock_rows,   item_info)
        self._enrich(transit_rows, item_info)

        if by_warehouse:
            # 合併同一品號+倉庫的現有與在途數量
            combined: Dict[tuple, dict] = {}
            for row in stock_rows:
                key = (row["item_code"], row["warehouse_code"])
                combined[key] = {
                    "item_code":     row["item_code"],
                    "item_name":     row["item_name"],
                    "warehouse_code": row["warehouse_code"],
                    "unit":          row["unit"],
                    "stock_qty":     row["qty"],
                    "transit_qty":   0.0,
                }
            for row in transit_rows:
                key = (row["item_code"], row["warehouse_code"])
                if key not in combined:
                    combined[key] = {
                        "item_code":      row["item_code"],
                        "item_name":      row["item_name"],
                        "warehouse_code": row["warehouse_code"],
                        "unit":           row["unit"],
                        "stock_qty":      0.0,
                        "transit_qty":    0.0,
                    }
                combined[key]["transit_qty"] += row["transit_qty"]
            result = sorted(
                [
                    {**r, "total_qty": r["stock_qty"] + r["transit_qty"]}
                    for r in combined.values()
                ],
                key=lambda x: (x["item_code"], x["warehouse_code"]),
            )
        else:
            stock   = self._aggregate_stock(stock_rows)
            transit = self._aggregate_transit(transit_rows)

            all_codes_sorted = sorted(set(stock) | set(transit))
            result = []
            for code in all_codes_sorted:
                stock_info  = stock.get(code, {})
                stock_qty   = stock_info.get("stock_qty", 0.0)
                transit_qty = transit.get(code, 0.0)
                result.append({
                    "item_code":   code,
                    "item_name":   item_info.get(code, {}).get("item_name", ""),
                    "unit":        item_info.get(code, {}).get("unit", ""),
                    "stock_qty":   stock_qty,
                    "transit_qty": transit_qty,
                    "total_qty":   stock_qty + transit_qty,
                })

        logger.info(f"合計庫存查詢完成，共 {len(result)} 筆")
        return result

    def print_inventory_summary(self) -> None:
        """在終端機列印庫存彙總報表（供快速確認使用）"""
        print("\n" + "=" * 75)
        print("  採購預測系統 - 即時庫存報表（現有 + 在途）")
        print("=" * 75)

        data = self.get_inventory()

        if not data:
            print("  （無庫存資料）")
        else:
            print(
                f"{'品號':<12} {'品名':<20} "
                f"{'現有庫存':>10} {'在途庫存':>10} {'合計':>10} {'單位':<6}"
            )
            print("-" * 75)
            for row in data:
                print(
                    f"{row['item_code']:<12} "
                    f"{row['item_name']:<20} "
                    f"{row['stock_qty']:>10.0f} "
                    f"{row['transit_qty']:>10.0f} "
                    f"{row['total_qty']:>10.0f} "
                    f"{row['unit']:<6}"
                )

        print("=" * 75 + "\n")
