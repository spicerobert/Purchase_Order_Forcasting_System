"""
Google Sheets 操作工具模組
用於讀取品號採購參數及寫入進貨建議工作表
"""
import configparser
import json
import logging
import gspread
from google.oauth2.service_account import Credentials
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 輸出工作表預設名稱（舊版 config 曾使用 LEGACY_OUTPUT_SHEET）
DEFAULT_OUTPUT_SHEET = "進貨建議"
LEGACY_OUTPUT_SHEET = "採購建議結果"


def resolve_output_sheet_name(gs_section) -> str:
    """
    從 [GOOGLE_SHEETS] 解析輸出工作表名稱。

    若仍設定舊名稱「採購建議結果」，一律改為「進貨建議」，避免新增分頁時沿用舊標題。
    """
    raw = (gs_section.get("output_sheet") or DEFAULT_OUTPUT_SHEET).strip()
    if not raw:
        return DEFAULT_OUTPUT_SHEET
    if raw == LEGACY_OUTPUT_SHEET:
        logger.info(
            "config.ini 的 output_sheet 為舊名稱「%s」，已自動改用「%s」",
            LEGACY_OUTPUT_SHEET,
            DEFAULT_OUTPUT_SHEET,
        )
        return DEFAULT_OUTPUT_SHEET
    return raw


class GoogleSheetsHelper:
    """Google Sheets 操作輔助類別"""

    def __init__(self, config_file: str = "config.ini", credentials_file: str = "service_account.json"):
        """
        初始化 Google Sheets 連接

        Args:
            config_file:       設定檔路徑
            credentials_file:  服務帳戶憑證 JSON 檔案路徑
        """
        self.config_file = config_file
        self.credentials_file = credentials_file
        self.client = None
        self.spreadsheet = None
        self._connect()

    # -------------------------------------------------------------------------
    # 內部工具方法
    # -------------------------------------------------------------------------

    def _connect(self):
        """建立 Google Sheets 連接"""
        config = configparser.ConfigParser()
        config.read(self.config_file, encoding="utf-8")

        if "GOOGLE_SHEETS" not in config:
            raise ValueError("config.ini 中缺少 [GOOGLE_SHEETS] 區段")

        sheet_url = config["GOOGLE_SHEETS"].get("spreadsheet_url", "")
        if not sheet_url:
            raise ValueError("config.ini [GOOGLE_SHEETS] 缺少 spreadsheet_url")

        with open(self.credentials_file, "r", encoding="utf-8") as f:
            creds_info = json.load(f)

        credentials = Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

        self.client = gspread.authorize(credentials)
        sheet_id = self._extract_sheet_id(sheet_url)
        self.spreadsheet = self.client.open_by_key(sheet_id)
        logger.info(f"已連接到 Google Sheets: {self.spreadsheet.title}")

    @staticmethod
    def _extract_sheet_id(url: str) -> str:
        """從 Google Sheets URL 提取 Spreadsheet ID"""
        parts = url.split("/")
        try:
            idx = parts.index("d") + 1
            return parts[idx].split("?")[0]
        except (ValueError, IndexError):
            raise ValueError(f"無法從 URL 提取 Sheet ID: {url}")

    # -------------------------------------------------------------------------
    # 工作表基礎操作
    # -------------------------------------------------------------------------

    def get_worksheet(
        self,
        worksheet_name: str,
        create_if_not_exists: bool = False,
        rows: int = 1000,
        cols: int = 26,
    ) -> Optional[gspread.Worksheet]:
        """取得工作表，若不存在可選擇是否建立"""
        if self.spreadsheet is None:
            return None
        try:
            return self.spreadsheet.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            if create_if_not_exists:
                return self.spreadsheet.add_worksheet(title=worksheet_name, rows=rows, cols=cols)
            return None

    def create_worksheet(self, worksheet_name: str, rows: int = 1000, cols: int = 26) -> gspread.Worksheet:
        """建立工作表（若已存在則先刪除再建立）"""
        if self.spreadsheet is None:
            raise ValueError("尚未連接到 Google Sheets")
        try:
            existing = self.spreadsheet.worksheet(worksheet_name)
            self.spreadsheet.del_worksheet(existing)
        except gspread.exceptions.WorksheetNotFound:
            pass
        return self.spreadsheet.add_worksheet(title=worksheet_name, rows=rows, cols=cols)

    def read_worksheet(self, worksheet_name: str) -> List[List[Any]]:
        """讀取整個工作表的所有值（二維列表）"""
        worksheet = self.get_worksheet(worksheet_name)
        if worksheet is None:
            return []
        return worksheet.get_all_values()

    def write_worksheet(self, worksheet_name: str, data: List[List[Any]], start_cell: str = "A1"):
        """將二維列表資料寫入工作表"""
        worksheet = self.get_worksheet(worksheet_name, create_if_not_exists=True)
        if worksheet is None:
            raise ValueError(f"無法取得或建立工作表: {worksheet_name}")
        worksheet.update(range_name=start_cell, values=data)

    def clear_worksheet(self, worksheet_name: str):
        """清空工作表內容"""
        worksheet = self.get_worksheet(worksheet_name)
        if worksheet:
            worksheet.clear()

    def list_worksheets(self) -> List[str]:
        """列出所有工作表名稱"""
        if self.spreadsheet is None:
            return []
        return [ws.title for ws in self.spreadsheet.worksheets()]

    # -------------------------------------------------------------------------
    # 採購預測系統專用：讀取參數 / 寫入結果
    # -------------------------------------------------------------------------

    def read_item_params(self) -> List[Dict[str, str]]:
        """
        讀取「品號參數」工作表

        Returns:
            每一列轉為字典，key 為標題行欄位名稱。
            空白列（品號為空）自動略過。

        期望欄位：
            品號 / 品名 / 服務水準 / 前置天數(週) /
            提前期標準差_週 / 到貨週期(週) / 最小採購量 /
            備註 / 進貨模式（常備｜有單才進；有單才進不計 SS／ROP／建議量）
        """
        config = configparser.ConfigParser()
        config.read(self.config_file, encoding="utf-8")
        sheet_name = config["GOOGLE_SHEETS"].get("input_sheet", "品號參數")

        data = self.read_worksheet(sheet_name)
        if not data or len(data) < 2:
            logger.warning(f"工作表「{sheet_name}」無資料或僅有標題行")
            return []

        headers = data[0]
        results = []
        for row in data[1:]:
            if not row or not row[0].strip():
                continue
            item: Dict[str, str] = {}
            for i, header in enumerate(headers):
                item[header] = row[i].strip() if i < len(row) else ""
            results.append(item)

        logger.info(f"從「{sheet_name}」讀取 {len(results)} 筆品號參數")
        return results

    def write_results(self, results: List[Dict]) -> None:
        """
        清空並寫入進貨建議到輸出工作表

        Args:
            results: calculate() 回傳的結果列表（每筆為 dict）
        """
        config = configparser.ConfigParser()
        config.read(self.config_file, encoding="utf-8")
        sheet_name = resolve_output_sheet_name(config["GOOGLE_SHEETS"])

        if not results:
            logger.warning("寫入結果為空，略過")
            return

        headers = list(results[0].keys())
        rows: List[List[Any]] = [headers]
        for r in results:
            rows.append([r.get(h, "") for h in headers])

        self.clear_worksheet(sheet_name)
        self.write_worksheet(sheet_name, rows)
        logger.info(f"已將 {len(results)} 筆結果寫入「{sheet_name}」")


# -------------------------------------------------------------------------
# 工作表初始化
# -------------------------------------------------------------------------

def initialize_sheets_structure(helper: GoogleSheetsHelper, config_file: str = "config.ini"):
    """
    初始化採購預測系統所需的 Google Sheets 工作表結構。

    - 工作表不存在 → 建立並寫入標題行
    - 工作表已存在但標題不符 → 僅更新第一列標題
    - 工作表已存在且標題正確 → 跳過

    Args:
        helper:      GoogleSheetsHelper 實例
        config_file: 設定檔路徑（用於讀取工作表名稱）
    """
    config = configparser.ConfigParser()
    config.read(config_file, encoding="utf-8")
    gs_cfg = config["GOOGLE_SHEETS"] if "GOOGLE_SHEETS" in config else {}
    input_sheet   = gs_cfg.get("input_sheet",  "品號參數")
    output_sheet  = resolve_output_sheet_name(gs_cfg)
    profile_sheet = gs_cfg.get("profile_sheet", "需求分群").strip() or "需求分群"

    profile_headers = [
        [
            "品號",
            "品名",
            "統計週數",
            "日曆旺季週數",
            "日曆淡季週數",
            "旺季週均需求",
            "淡季週均需求",
            "淡旺季比值_旺除淡",
            "年週平均",
            "年週標準差",
            "變異係數_CV",
            "零出庫週數",
            "零出庫週占比",
            "近4週均_不含最新週",
            "前4週均_再往前4週",
            "MA4高峰在日曆旺季內",
            "MA4高峰說明",
            "建議模式",
            "分群說明",
        ]
    ]

    sheets_structure = {
        input_sheet: [
            [
                "品號", "品名", "服務水準",
                "前置天數(週)", "提前期標準差_週", "到貨週期(週)",
                "最小採購量", "備註", "進貨模式",
            ]
        ],
        output_sheet: [
            [
                "更新時間", "品號", "品名", "進貨模式", "單位",
                "現有庫存", "在途庫存", "合計庫存", "統計週數",
                "前4週均(再往前4週)", "近4週均(不含最新週)",
                "週平均(年)", "需求分群", "趨勢係數_adj", "採用週需求_D", "需求D來源",
                "標準差(年)",
                "安全庫存_SS", "補貨點_ROP", "建議採購量", "進貨建議",
            ]
        ],
        profile_sheet: profile_headers,
    }

    print("正在建立工作表結構...")
    created, updated = [], []
    existing_sheets = helper.list_worksheets()

    for sheet_name, headers in sheets_structure.items():
        if sheet_name not in existing_sheets:
            helper.create_worksheet(sheet_name, rows=1000, cols=26)
            helper.write_worksheet(sheet_name, headers)
            created.append(sheet_name)
            print(f"  [建立] {sheet_name}")
        else:
            ws = helper.get_worksheet(sheet_name)
            if ws:
                existing_headers = ws.row_values(1)
                if existing_headers != headers[0]:
                    ws.update(range_name="1:1", values=[headers[0]])
                    updated.append(sheet_name)
                    print(f"  [更新標題] {sheet_name}")
                else:
                    print(f"  [已存在] {sheet_name}")

    if created:
        print(f"\n[完成] 已建立 {len(created)} 個新工作表: {', '.join(created)}")
    if updated:
        print(f"[完成] 已更新 {len(updated)} 個工作表標題: {', '.join(updated)}")
    if not created and not updated:
        print("\n[完成] 所有工作表已存在且結構正確，無需更新")
