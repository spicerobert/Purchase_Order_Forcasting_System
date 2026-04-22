"""
ERP 資料庫連接工具模組
專用於 MS-SQL Server 資料庫連接，用於採購預測系統的庫存查詢
"""
import configparser
import logging
from typing import Any, Dict, List, Optional, Tuple

try:
    import pyodbc
except ImportError:
    raise ImportError("請安裝 pyodbc: uv add pyodbc")

logger = logging.getLogger(__name__)

DEFAULT_WAREHOUSE_CODES = [
    "SB11", "SB12", "SB13", "SB14",
    "SM10", "SM20", "SM30",
    "SP10", "SP11", "SP20", "SP30", "SP40", "SP50", "SP60", "SP61", "SP70", "SP80", "SP81",
    "SS10", "SS11", "SS12", "SS13", "SS14", "SS20", "SS30", "SS40", "SS41", "SS42",
]


class ERPDBHelper:
    """ERP 資料庫連接輔助類別（MS-SQL Server）"""

    def __init__(self, config_file: str = "config.ini"):
        """
        初始化 ERP 資料庫連接

        Args:
            config_file: 設定檔路徑
        """
        self.config_file = config_file
        self._config = self._load_config()
        self.connection = None
        self._connect()

    # -------------------------------------------------------------------------
    # 內部工具方法
    # -------------------------------------------------------------------------

    def _load_config(self) -> configparser.ConfigParser:
        """讀取並回傳 config.ini（初始化時呼叫一次，結果快取於 self._config）"""
        config = configparser.ConfigParser()
        config.read(self.config_file, encoding="utf-8")
        return config

    def _get_warehouse_codes(self) -> List[str]:
        """從 config.ini 讀取倉庫代碼清單，若未設定則使用預設值"""
        if "INVENTORY" in self._config:
            raw = self._config["INVENTORY"].get("warehouse_codes", "")
            codes = [c.strip() for c in raw.split(",") if c.strip()]
            if codes:
                return codes
        return DEFAULT_WAREHOUSE_CODES

    def _build_codes_str(self) -> Tuple[List[str], str]:
        """
        組合倉庫代碼清單與 SQL IN 子句字串

        Returns:
            (倉庫代碼清單, SQL IN 子句內容)
        """
        codes = self._get_warehouse_codes()
        safe = [c.replace("'", "''") for c in codes]
        return codes, "','".join(safe)

    def _get_custom_sql(self, key: str) -> str:
        """
        從 config.ini [ERP_QUERIES] 取得自訂 SQL

        Args:
            key: 查詢鍵名（如 'inventory_query'）

        Returns:
            自訂 SQL 字串，若未設定則回傳空字串
        """
        if "ERP_QUERIES" in self._config:
            return self._config["ERP_QUERIES"].get(key, "").strip()
        return ""

    def _connect(self):
        """建立 MS-SQL Server 資料庫連接"""
        if "ERP_DATABASE" not in self._config:
            raise ValueError("config.ini 中缺少 [ERP_DATABASE] 區段")

        db_config = self._config["ERP_DATABASE"]
        server = db_config.get("server")
        database = db_config.get("database")
        username = db_config.get("username", "")
        password = db_config.get("password", "")

        if not server or not database:
            raise ValueError("config.ini 中缺少 server 或 database 設定")

        driver = self._find_available_driver()

        if username and password:
            conn_str = (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={database};"
                f"UID={username};"
                f"PWD={password}"
            )
        else:
            conn_str = (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={database};"
                f"Trusted_Connection=yes;"
            )

        try:
            self.connection = pyodbc.connect(conn_str)
            logger.info(f"已連接到 SQL Server: {server}/{database}")
        except Exception as e:
            logger.error(f"連接 SQL Server 失敗: {str(e)}")
            raise

    def _find_available_driver(self) -> str:
        """尋找可用的 ODBC 驅動程式"""
        available_drivers = pyodbc.drivers()
        preferred_drivers = [
            "ODBC Driver 17 for SQL Server",
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 13 for SQL Server",
            "SQL Server Native Client 11.0",
            "SQL Server",
        ]

        for preferred in preferred_drivers:
            if preferred in available_drivers:
                logger.info(f"使用 ODBC 驅動程式: {preferred}")
                return preferred

        raise ValueError(
            f"找不到可用的 SQL Server ODBC 驅動程式。"
            f"可用的驅動程式: {', '.join(available_drivers)}"
        )

    # -------------------------------------------------------------------------
    # 公開查詢方法
    # -------------------------------------------------------------------------

    def execute_query(
        self, sql: str, params: Optional[tuple] = None
    ) -> List[Dict[str, Any]]:
        """
        執行 SQL 查詢

        Args:
            sql: SQL 查詢語句
            params: 查詢參數（用於防止 SQL 注入）

        Returns:
            查詢結果列表，每個元素是一個字典（欄位名: 值）
        """
        if self.connection is None:
            raise ConnectionError("資料庫連接未建立")

        cursor = self.connection.cursor()
        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)

            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [
                {col: row[i] for i, col in enumerate(columns)}
                for row in cursor.fetchall()
            ]
        finally:
            cursor.close()

    def get_item_info(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        查詢品號主檔（INVMB），取得品名、庫存單位、包裝單位與外包裝含商品數

        Args:
            codes: 品號清單

        Returns:
            {
                item_code: {
                    'item_name': str,   # MB002 品名
                    'unit':      str,   # MB004 庫存單位
                    'pack_unit': str,   # MB016 包裝單位
                    'pack_qty':  float, # MB073 外包裝含商品數（1包裝單位 = N 庫存單位）
                }
            }
        """
        if not codes:
            return {}

        safe_codes = [c.replace("'", "''") for c in codes]
        codes_str = "','".join(safe_codes)

        sql = f"""
            SELECT
                MB001                       as item_code,
                COALESCE(MB002, '')         as item_name,
                COALESCE(MB004, '')         as unit,
                COALESCE(MB016, '')         as pack_unit,
                COALESCE(MB073, 1)          as pack_qty
            FROM [AS_online].[dbo].[INVMB]
            WHERE MB001 IN ('{codes_str}')
        """

        logger.info(f"查詢品號主檔（共 {len(codes)} 個品號）...")
        results = self.execute_query(sql)
        return {
            str(row["item_code"]).strip(): {
                "item_name": str(row["item_name"]).strip(),
                "unit":      str(row["unit"]).strip(),
                "pack_unit": str(row["pack_unit"]).strip(),
                "pack_qty":  float(row["pack_qty"]) if row.get("pack_qty") else 1.0,
            }
            for row in results
        }

    def get_current_period(self) -> str:
        """
        查詢 ERP 目前實際有效期間（兩步驟查詢的第一步）

        透過查詢 INVLC 表中指定倉庫的 MAX(LC002)，取得 ERP 目前正在運作的
        期間字串（格式：YYYYMM）。此方式可正確反映月結狀態：
        - 若上月尚未月結關帳，回傳上月期間（如 '202602'）
        - 若上月已完成月結，INVLC 已結轉，則回傳當月期間（如 '202603'）

        Returns:
            期間字串，格式 YYYYMM（例：'202603'）

        Raises:
            ValueError: 若指定倉庫在 INVLC 無任何資料
        """
        codes, codes_str = self._build_codes_str()

        sql = f"""
            SELECT MAX(LC002) as current_period
            FROM [AS_online].[dbo].[INVLC]
            WHERE LC003 IN ('{codes_str}')
        """

        logger.info(f"查詢 ERP 目前有效期間（倉庫：{', '.join(codes)}）...")
        results = self.execute_query(sql)

        if not results or results[0].get("current_period") is None:
            raise ValueError(
                f"無法從 INVLC 取得有效期間，"
                f"請確認倉庫代碼是否正確：{', '.join(codes)}"
            )

        current_period = str(results[0]["current_period"]).strip()
        logger.info(f"目前 ERP 有效期間: {current_period}")
        return current_period

    def get_transit_inventory(self) -> List[Dict[str, Any]]:
        """
        查詢在途庫存（已確認採購單但尚未到貨的數量）

        查詢條件：
        - TD018 = 'Y'（採購單已確認）
        - TD016 = 'N'（尚未結案）
        在途數量 = SUM(TD008 - TD015)（採購數量 - 已交數量）

        依 TD004（品號）、TD007（倉庫）、TD009（訂貨單位）分組，
        保留訂貨單位供上層進行庫存單位轉換判斷。

        僅回傳數量資料，不含品名與庫存單位。
        品名與單位資訊請透過 get_item_info() 統一查詢。

        Returns:
            在途庫存資料列表，格式:
            [{'item_code', 'warehouse_code', 'order_unit', 'transit_qty'}, ...]
        """
        sql = """
            SELECT
                TD004                       as item_code,
                TD007                       as warehouse_code,
                TD009                       as order_unit,
                SUM(TD008 - TD015)          as transit_qty
            FROM [AS_online].[dbo].[PURTD]
            WHERE TD018 = 'Y'
                AND TD016 = 'N'
                AND TD004 IS NOT NULL
            GROUP BY TD004, TD007, TD009
            HAVING SUM(TD008 - TD015) > 0
        """

        logger.info("執行在途庫存查詢...")
        results = self.execute_query(sql)

        standardized = [
            {
                "item_code":      str(row.get("item_code", "")).strip(),
                "warehouse_code": str(row.get("warehouse_code", "")).strip(),
                "order_unit":     str(row.get("order_unit", "")).strip(),
                "transit_qty":    float(row["transit_qty"]) if row.get("transit_qty") is not None else 0.0,
            }
            for row in results
        ]

        logger.info(f"在途庫存查詢完成，共 {len(standardized)} 筆")
        return standardized

    def get_inventory(self) -> List[Dict[str, Any]]:
        """
        查詢即時庫存（兩步驟：先取有效期間，再查庫存明細）

        僅回傳庫存數量資料，不含品名與單位。
        品名與單位請透過 get_item_info() 統一查詢。

        Returns:
            庫存資料列表，格式:
            [{'item_code', 'warehouse_code', 'qty'}, ...]
        """
        custom_sql = self._get_custom_sql("inventory_query")
        if custom_sql:
            logger.info("執行庫存查詢（使用 config.ini 自訂 SQL）")
            results = self.execute_query(custom_sql)
        else:
            current_period = self.get_current_period()
            codes, codes_str = self._build_codes_str()
            period_start = f"{current_period}01"

            sql = f"""
                SELECT
                    LC.LC001    as item_code,
                    LC.LC003    as warehouse_code,
                    LC.LC004 + COALESCE(SUM(LA.LA011 * LA.LA005), 0) as qty
                FROM [AS_online].[dbo].[INVLC] LC
                LEFT JOIN [AS_online].[dbo].[INVLA] LA
                    ON LA.LA001 = LC.LC001
                    AND LA.LA009 = LC.LC003
                    AND LA.LA004 >= '{period_start}'
                WHERE LC.LC001 IS NOT NULL
                    AND LC.LC002 = '{current_period}'
                    AND LC.LC003 IN ('{codes_str}')
                GROUP BY LC.LC001, LC.LC003, LC.LC004
            """

            logger.info(f"執行庫存查詢（期間: {current_period}，倉庫: {', '.join(codes)}）")
            results = self.execute_query(sql)

        standardized = [
            {
                "item_code":      str(row.get("item_code", "")).strip(),
                "warehouse_code": str(row.get("warehouse_code", "")).strip(),
                "qty":            float(row["qty"]) if row.get("qty") is not None else 0.0,
            }
            for row in results
        ]

        logger.info(f"庫存查詢完成，共 {len(standardized)} 筆")
        return standardized

    def get_weekly_demand(
        self,
        item_codes: List[str],
        history_weeks: int = 52,
    ) -> Dict[str, Any]:
        """
        查詢指定品號過去 N 個「完整曆週」的出庫需求，並依週小計。

        時間範圍（與業務假設一致）：
        - **不含本週尚未結束的區間**（本週視為不完整週，不納入統計）。
        - 以「上一個完整曆週」為最新一週，再往前共 history_weeks 週（預設 52）。

        查詢條件：
        - LA005 = -1（出庫方向）
        - LA004 落在「本週一 00:00 之前」，且自「本週一往前第 N 週的週一」起（與週分組同一套週界）
        - LA009 IN（config.ini 倉庫清單）
        - LA001 IN（item_codes）

        零需求週處理：自動補入數量 0 的週，確保回傳剛好 history_weeks 筆。

        注意：週分組使用 SQL `DATEADD(WEEK, DATEDIFF(WEEK, 0, LA004), 0)`；下方 Python
        以「本週一」建週清單，須與伺服器週界一致（一般與週一分週對齊）。

        Args:
            item_codes:    要查詢的品號清單
            history_weeks: 回溯週數，預設 52（一年）

        Returns:
            {
                "A001": {
                    "weeks": [
                        {"week_start": "2025-03-17", "qty": 120.0},
                        ...
                    ],
                    "avg_weekly_demand": 105.3,
                    "std_weekly_demand": 18.7,
                    "data_weeks": 52,
                }
            }
        """
        if not item_codes:
            return {}

        safe_codes = [c.replace("'", "''") for c in item_codes]
        codes_str = "','".join(safe_codes)
        _, warehouse_codes_str = self._build_codes_str()

        # 週界與 SELECT 中 week_start 一致：本週一 = DATEADD(WEEK, DATEDIFF(WEEK, 0, GETDATE()), 0)
        # 僅統計「上一完整曆週」以前：LA004 < 本週一；下界為本週一往前 history_weeks 週。
        sql = f"""
            SELECT
                LA001                                               AS item_code,
                DATEADD(WEEK, DATEDIFF(WEEK, 0, LA004), 0)         AS week_start,
                SUM(LA011)                                          AS weekly_qty
            FROM [AS_online].[dbo].[INVLA]
            WHERE LA005 = -1
              AND LA004 >= DATEADD(
                  WEEK,
                  -{history_weeks},
                  DATEADD(WEEK, DATEDIFF(WEEK, 0, GETDATE()), 0)
              )
              AND LA004 < DATEADD(WEEK, DATEDIFF(WEEK, 0, GETDATE()), 0)
              AND LA009 IN ('{warehouse_codes_str}')
              AND LA001 IN ('{codes_str}')
            GROUP BY
                LA001,
                DATEADD(WEEK, DATEDIFF(WEEK, 0, LA004), 0)
            ORDER BY LA001, week_start
        """

        logger.info(
            f"查詢週需求歷史（{len(item_codes)} 個品號，"
            f"{history_weeks} 個完整曆週、不含本週）..."
        )
        rows = self.execute_query(sql)

        # --- 整理成 {item_code: {week_start_str: qty}} ---
        from collections import defaultdict
        import datetime
        import math

        raw: Dict[str, Dict[str, float]] = defaultdict(dict)
        for row in rows:
            code  = str(row["item_code"]).strip()
            ws    = row["week_start"]
            qty   = float(row["weekly_qty"]) if row["weekly_qty"] is not None else 0.0
            ws_str = ws.strftime("%Y-%m-%d") if hasattr(ws, "strftime") else str(ws)[:10]
            raw[code][ws_str] = qty

        # --- 建立完整 history_weeks 個週起始日清單（含零需求週）---
        # 本週一（Python：Monday=0）；序列為 this_monday-52w … this_monday-1w，
        # 即「以上一完整曆週為最新一週」往回數 N 週，**不含本週**。
        today       = datetime.date.today()
        this_monday = today - datetime.timedelta(days=today.weekday())
        all_weeks = [
            (this_monday - datetime.timedelta(weeks=i)).strftime("%Y-%m-%d")
            for i in range(history_weeks, 0, -1)
        ]

        result: Dict[str, Any] = {}
        for code in item_codes:
            week_data = [
                {"week_start": w, "qty": raw[code].get(w, 0.0)}
                for w in all_weeks
            ]
            qtys = [d["qty"] for d in week_data]

            n   = len(qtys)
            avg = sum(qtys) / n if n > 0 else 0.0
            variance = sum((q - avg) ** 2 for q in qtys) / (n - 1) if n > 1 else 0.0
            std = math.sqrt(variance)

            result[code] = {
                "weeks":              week_data,
                "avg_weekly_demand":  round(avg, 4),
                "std_weekly_demand":  round(std, 4),
                "data_weeks":         n,
            }
            logger.debug(
                f"品號 {code}：週均需求 {avg:.2f}，標準差 {std:.2f}（{n} 週）"
            )

        logger.info(f"週需求歷史查詢完成，共 {len(result)} 個品號")
        return result

    # -------------------------------------------------------------------------
    # Context manager 支援
    # -------------------------------------------------------------------------

    def close(self):
        """關閉資料庫連接"""
        if self.connection:
            self.connection.close()
            logger.info("已關閉資料庫連接")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
