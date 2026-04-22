"""
設定 Google Sheets 工作表結構
執行此腳本將建立採購預測系統所需的工作表及欄位標題

使用方式：
    uv run setup_sheets.py
"""
from purchase_order.google_sheets_helper import GoogleSheetsHelper, initialize_sheets_structure


def main() -> bool:
    """主程式：建立工作表結構"""
    print("=" * 65)
    print("  採購預測系統 - Google Sheets 工作表初始化")
    print("=" * 65)

    try:
        print("\n[步驟 1/2] 連接 Google Sheets...")
        helper = GoogleSheetsHelper()
        print(f"[成功] 已連接到試算表: {helper.spreadsheet.title}")

        existing_sheets = helper.list_worksheets()
        if existing_sheets:
            print(f"\n現有工作表 ({len(existing_sheets)} 個):")
            for i, name in enumerate(existing_sheets, 1):
                print(f"  {i}. {name}")

        print("\n[步驟 2/2] 建立工作表結構...")
        initialize_sheets_structure(helper)

        print("\n" + "=" * 65)
        print("最終工作表列表:")
        for i, name in enumerate(helper.list_worksheets(), 1):
            print(f"  {i}. {name}")

        print("\n" + "=" * 65)
        print("[完成] 工作表結構設定完成！")
        print("=" * 65)
        print("\n接下來請在「品號參數」工作表中填入各品號的採購參數：")
        print()
        print("  【必填欄位】")
        print("  ├─ 品號           : ERP 品號（例如 A001）")
        print("  ├─ 服務水準       : 目標服務水準（建議 95）")
        print("  ├─ 前置天數(週)   : 平均到貨週數（例如 2）")
        print("  ├─ 提前期標準差_週: 交期波動標準差，固定交期填 0")
        print("  └─ 到貨週期(週)   : 兩次採購間隔週數（例如 2）")
        print()
        print("  【選填欄位】")
        print("  ├─ 品名           : 可留空，系統自動從 ERP 填入")
        print("  ├─ 最小採購量     : MOQ，未填則預設 1")
        print("  └─ 進貨模式       : 常備（預設，計算安全庫存與建議量）或 有單才進（不計算，進貨建議顯示依訂單）")
        print()
        print("填妥後執行 `uv run main.py` 即可產生進貨建議。")
        print("=" * 65)

    except Exception as e:
        print(f"\n[錯誤] 執行失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

    return True


if __name__ == "__main__":
    success = main()
    if not success:
        exit(1)
