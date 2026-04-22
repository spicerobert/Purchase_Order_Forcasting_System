"""
採購預測系統 - 主程式

執行流程：
    1. 從 Google Sheets「品號參數」讀取品號清單及採購參數
    2. 從 ERP INVLA 查詢過去一年週需求歷史（自動計算 avg_D / STD）
    3. 從 ERP 查詢現有庫存 + 在途庫存
    4. 計算安全庫存（SS）、補貨點（ROP）、建議採購量
    5. 將結果寫回 Google Sheets「進貨建議」

使用方式：
    uv run main.py
"""
import logging
import sys

from purchase_order.google_sheets_helper import GoogleSheetsHelper
from purchase_order.safety_stock import SafetyStockCalculator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.ini"


def main() -> bool:
    print("=" * 70)
    print("  採購預測系統 - 安全庫存 / 補貨點 / 建議採購量計算")
    print("=" * 70)

    try:
        # ------------------------------------------------------------------
        # 步驟 1：連接 Google Sheets，讀取品號參數
        # ------------------------------------------------------------------
        print("\n[步驟 1/3] 連接 Google Sheets，讀取品號參數...")
        sheets = GoogleSheetsHelper(config_file=CONFIG_FILE)
        item_params = sheets.read_item_params()

        if not item_params:
            print("[警告] 「品號參數」工作表中無有效資料，請先填入品號後再執行。")
            return False

        print(f"[成功] 已讀取 {len(item_params)} 個品號：")
        for p in item_params:
            code = p.get("品號", "")
            sl   = p.get("服務水準", "-")
            lt   = p.get("前置天數(週)", "-")
            oc   = p.get("到貨週期(週)", "-")
            print(f"  ├─ {code}  服務水平={sl}%  提前期={lt}週  到貨週期={oc}週")

        # ------------------------------------------------------------------
        # 步驟 2：查詢 ERP 需求歷史 + 庫存，計算安全庫存
        # ------------------------------------------------------------------
        print("\n[步驟 2/3] 連接 ERP，計算安全庫存...")
        calc    = SafetyStockCalculator(config_file=CONFIG_FILE)
        results = calc.calculate(item_params)

        if not results:
            print("[警告] 計算結果為空，請確認 ERP 連線及品號是否正確。")
            return False

        # 終端機預覽摘要
        print(f"\n[成功] 計算完成，共 {len(results)} 個品號")
        print()
        header = (
            f"{'品號':<12} {'品名':<10} {'模式':>4} "
            f"{'adj':>6} {'採用D':>7} "
            f"{'前4':>7} {'近4':>7} "
            f"{'年週均':>7} {'STD':>6} "
            f"{'SS':>7} {'ROP':>7} "
            f"{'合計':>7} {'建議':>7} {'進貨建議':>8}"
        )
        print(header)
        print("-" * len(header))
        for r in results:
            adj_v = r.get("趨勢係數_adj", "")
            adj_str = f"{float(adj_v):>6.2f}" if adj_v != "" and adj_v is not None else "    -"
            adv = str(r.get("進貨建議", "") or "")
            mode = str(r.get("進貨模式", "") or "")[:4]
            print(
                f"{str(r['品號']):<12} "
                f"{str(r['品名']):<10} "
                f"{mode:>4} "
                f"{adj_str} "
                f"{r['採用週需求_D']:>7.1f} "
                f"{r['前4週均(再往前4週)']:>7.1f} "
                f"{r['近4週均(不含最新週)']:>7.1f} "
                f"{r['週平均(年)']:>7.1f} "
                f"{r['標準差(年)']:>6.1f} "
                f"{r['安全庫存_SS']:>7.1f} "
                f"{r['補貨點_ROP']:>7.1f} "
                f"{r['合計庫存']:>7.0f} "
                f"{r['建議採購量']:>7.0f} "
                f"{adv:>8}"
            )

        # ------------------------------------------------------------------
        # 步驟 3：寫回 Google Sheets
        # ------------------------------------------------------------------
        print("\n[步驟 3/3] 寫回 Google Sheets「進貨建議」...")
        sheets.write_results(results)
        print("[成功] 結果已寫入 Google Sheets！")
        print()
        import configparser as _cp
        _cfg = _cp.ConfigParser()
        _cfg.read(CONFIG_FILE, encoding="utf-8")
        _url = _cfg.get("GOOGLE_SHEETS", "spreadsheet_url", fallback="")
        if _url:
            print(f"請開啟以下連結查看結果：\n  {_url}")

    except FileNotFoundError as e:
        print(f"\n[錯誤] 找不到設定檔或憑證檔案：{e}")
        print("請確認 config.ini 與 service_account.json 存在於專案根目錄。")
        return False
    except Exception as e:
        print(f"\n[錯誤] 執行失敗：{e}")
        import traceback
        traceback.print_exc()
        return False

    print("\n" + "=" * 70)
    print("[完成] 採購預測計算完成")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = main()
    if not success:
        sys.exit(1)
