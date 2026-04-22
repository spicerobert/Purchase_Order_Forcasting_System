"""
安全庫存計算模組

公式（情境三：需求與提前期均為隨機變化）：
    SS  = Z × √( STD² × L + STD2² × D² )
    ROP = D × L + SS
    建議採購量 = max(0, (L + T) × D + SS - total_qty)

時間單位統一為「週」：
    D    = 分群週需求 × 趨勢係數_adj（近4／前4週；見「採用週需求_D」）
          A/C 基底年週均、B 當前淡／旺週均、D 間歇仍用年週均×adj 計 SS／ROP／建議量
    STD  = 標準差(年)（從 INVLA 出庫歷史自動計算，使用週資料計算）
    L    = 平均採購提前期（週，由 config.ini 或 Google Sheets 品號參數提供）
    STD2 = 提前期標準差（週，同上；填 0 退化為情境一）
    T    = 訂貨周期（週，同上）
    Z    = 安全係數（由服務水平查表決定）

參考：
    https://read01.com/zh-tw/Q3dN04D.html
    https://www.jendow.com.tw/wiki/安全庫存
"""
import configparser
import datetime
import logging
import math
from typing import Any, Dict, List, Optional

from .erp_db_helper import ERPDBHelper
from .inventory_query import InventoryQuery

logger = logging.getLogger(__name__)

# Z 係數對照表（服務水平 → 標準常態分布 Z 值）
_Z_TABLE: Dict[int, float] = {
    100: 3.09,
    99:  2.33,
    98:  2.05,
    97:  1.88,
    96:  1.75,
    95:  1.65,
    90:  1.28,
    85:  1.04,
    84:  1.00,
    80:  0.84,
    75:  0.68,
}


def _lookup_z(service_level: float) -> float:
    """
    依服務水平查詢 Z 係數。
    自動相容兩種輸入格式：0.95（小數）或 95（整數百分比）
    若不在對照表中，使用最接近的值。
    """
    # 自動轉換：0.95 → 95
    if service_level <= 1.0:
        service_level = service_level * 100.0
    level_int = int(round(service_level))
    if level_int in _Z_TABLE:
        return _Z_TABLE[level_int]
    # 找最近的鍵
    closest = min(_Z_TABLE.keys(), key=lambda k: abs(k - level_int))
    logger.warning(f"服務水平 {service_level}% 不在對照表中，使用最近值 {closest}% (Z={_Z_TABLE[closest]})")
    return _Z_TABLE[closest]


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全地將任意值轉換為 float，失敗時回傳 default"""
    try:
        return float(value) if str(value).strip() else default
    except (ValueError, TypeError):
        return default


def _normalize_purchase_mode(raw: Any) -> str:
    """
    試算表「進貨模式」：常備 → 計算 SS／ROP／建議採購量；有單才進 → 不計算（輸出依訂單）。
    空白或無法辨識視為常備。
    """
    s = str(raw or "").strip().replace(" ", "").replace("\u3000", "")
    if not s:
        return "常備"
    if s in ("有單才進", "有单才进", "MTO", "mto"):
        return "有單才進"
    if s in ("常備", "常备"):
        return "常備"
    logger.warning("未知進貨模式 %r，視為常備", raw)
    return "常備"


def _four_week_compare_avgs(week_qtys: List[float]) -> tuple[float, float]:
    """
    以週需求序列（由舊到新，與 get_weekly_demand 的 weeks 順序一致）計算兩組各 4 週平均：

    - 排除最後 1 筆（倒數第 1 週不納入，視為本週／不完整週）
    - 第一組：倒數第 2～5 週，共 4 個資料點 → week_qtys[-5:-1]
    - 第二組：倒數第 6～9 週，共 4 個資料點 → week_qtys[-9:-5]

    須至少 5 週可算第一組；至少 9 週可算第二組，不足則該組為 0。
    """
    if not week_qtys:
        return 0.0, 0.0
    n = len(week_qtys)
    if n < 5:
        return 0.0, 0.0
    avg1 = sum(week_qtys[-5:-1]) / 4.0
    if n < 9:
        return round(avg1, 4), 0.0
    avg2 = sum(week_qtys[-9:-5]) / 4.0
    return round(avg1, 4), round(avg2, 4)


class SafetyStockCalculator:
    """
    安全庫存計算器

    資料流：
        1. 從 Google Sheets「品號參數」讀取品號清單及各項採購參數（含進貨模式：常備／有單才進）
        2. 從 ERP INVLA 查詢過去一年週需求歷史，計算 avg_D / STD
        3. 依 demand_profile 分群決定基底週需求 D，再乘趨勢係數 adj（D 亦為年週均×adj）
        4. 從 ERP 查詢現有庫存 + 在途庫存（total_qty）
        5. 套用公式計算 SS / ROP / 建議採購量
        6. 回傳結果列表，供 GoogleSheetsHelper.write_results() 寫回

    各參數優先順序（高 → 低）：
        品號個別設定（Google Sheets）> config.ini 全域預設

    品名／單位顯示優先順序：
        試算表「品名」> 庫存在途彙總列 > ERP INVMB 主檔（避免「無庫存品號」匯出時品名空白）
    """

    def __init__(self, config_file: str = "config.ini"):
        self._config_file = config_file
        self._config = self._load_config()

    def _load_config(self) -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config.read(self._config_file, encoding="utf-8")
        return config

    def _get_ss_config(self, key: str, fallback: float) -> float:
        """讀取 [SAFETY_STOCK] 設定，失敗時回傳 fallback"""
        try:
            return self._config.getfloat("SAFETY_STOCK", key, fallback=fallback)
        except Exception:
            return fallback

    def _get_ss_config_bool(self, key: str, fallback: bool) -> bool:
        try:
            return self._config.getboolean("SAFETY_STOCK", key, fallback=fallback)
        except Exception:
            return fallback

    @staticmethod
    def _purchase_advice_text(
        stock_qty: float,
        rop: float,
        *,
        d_for_ss: float,
        lead_time: float,
        order_cycle: float,
        safety_stock: float,
        overstock_enabled: bool,
        overstock_extra_weeks: float,
    ) -> str:
        """
        Google Sheets「進貨建議」欄位文字。

        - 與「現有庫存」欄一致：round(現有, 0)==0 → 庫存0（優先）
        - 現有庫存 < 補貨點 ROP → 需要進貨
        - 庫存偏高：門檻 ceiling=(L+T)*D+SS+額外週*D 須 >0，且 現有 >= ceiling
          （避免 ceiling=0 時 0>=0 誤判為庫存偏高）
        - 其餘：空白
        """
        if round(stock_qty, 0) == 0:
            return "庫存0"
        if rop > 0 and stock_qty < rop:
            return "需要進貨"
        if (
            overstock_enabled
            and overstock_extra_weeks > 0
            and d_for_ss > 1e-9
        ):
            ceiling = (
                (lead_time + order_cycle) * d_for_ss
                + safety_stock
                + overstock_extra_weeks * d_for_ss
            )
            if ceiling > 1e-6 and stock_qty >= ceiling:
                return "庫存偏高"
        return ""

    def _get_trend_adj_settings(self) -> tuple[bool, float, float, float]:
        """
        讀取 [DEMAND_PROFILE] 8 週趨勢係數設定。
        Returns:
            (enabled, adj_min, adj_max, prev4_floor)
        """
        if not self._config.has_section("DEMAND_PROFILE"):
            return True, 0.5, 1.5, 0.01
        sec = self._config["DEMAND_PROFILE"]
        try:
            enabled = sec.getboolean("trend_adj_enabled", fallback=True)
        except Exception:
            enabled = True
        lo = sec.getfloat("trend_adj_min", fallback=0.5)
        hi = sec.getfloat("trend_adj_max", fallback=1.5)
        floor = sec.getfloat("trend_adj_prev4_floor", fallback=0.01)
        return enabled, lo, hi, floor

    @staticmethod
    def _compute_trend_adj(
        near4: float,
        prev4: float,
        enabled: bool,
        adj_min: float,
        adj_max: float,
        prev4_floor: float,
    ) -> tuple[float, str]:
        """
        adj = 近4週均 / 前4週均，並限制在 [adj_min, adj_max]。
        回傳 (用於乘上分群 D 的係數, 進貨建議欄位「趨勢係數_adj」顯示字串)。
        """
        if not enabled:
            return 1.0, ""
        if prev4 <= prev4_floor:
            return 1.0, ""
        raw = near4 / prev4
        adj = max(adj_min, min(adj_max, raw))
        return adj, round(adj, 4)

    # -------------------------------------------------------------------------
    # 核心計算
    # -------------------------------------------------------------------------

    @staticmethod
    def _calc_safety_stock(
        z: float,
        std_demand: float,
        avg_lead_time: float,
        std_lead_time: float,
        avg_demand: float,
    ) -> float:
        """
        SS = Z × √( STD² × L + STD2² × D² )

        Args:
            z:              安全係數
            std_demand:     標準差(年) STD（由週資料統計）
            avg_lead_time:  平均提前期 L（週）
            std_lead_time:  提前期標準差 STD2（週）
            avg_demand:     週平均(年)需求量 D

        Returns:
            安全庫存 SS（與需求同單位）
        """
        variance = (std_demand ** 2) * avg_lead_time + (std_lead_time ** 2) * (avg_demand ** 2)
        return z * math.sqrt(variance) if variance > 0 else 0.0

    @staticmethod
    def _calc_rop(avg_demand: float, avg_lead_time: float, safety_stock: float) -> float:
        """ROP = D × L + SS"""
        return avg_demand * avg_lead_time + safety_stock

    @staticmethod
    def _calc_suggested_qty(
        avg_demand: float,
        avg_lead_time: float,
        order_cycle: float,
        safety_stock: float,
        existing_qty: float,
        moq: float = 1.0,
    ) -> float:
        """
        建議採購量 = max(0, (L + T) × D + SS - existing_qty)
        結果進位至 moq 的整數倍
        """
        raw = (avg_lead_time + order_cycle) * avg_demand + safety_stock - existing_qty
        if raw <= 0:
            return 0.0
        if moq > 1:
            return math.ceil(raw / moq) * moq
        return math.ceil(raw)

    # -------------------------------------------------------------------------
    # 主要公開方法
    # -------------------------------------------------------------------------

    def calculate(self, item_params: List[Dict[str, str]]) -> List[Dict]:
        """
        批次計算所有品號的安全庫存、補貨點與建議採購量。

        Args:
            item_params: Google Sheets「品號參數」工作表的列表
                         每筆至少含 {'品號': ..., '服務水準': ..., ...}

        Returns:
            結果列表，每筆含完整欄位，可直接傳給 GoogleSheetsHelper.write_results()
        """
        if not item_params:
            logger.warning("item_params 為空，無法計算")
            return []

        item_codes = [p["品號"] for p in item_params if p.get("品號")]
        if not item_codes:
            logger.warning("item_params 中無有效品號")
            return []

        # --- 全域預設值（config.ini [SAFETY_STOCK]）---
        default_service_level = self._get_ss_config("service_level", 95.0)
        default_lead_time     = self._get_ss_config("avg_lead_time_weeks", 2.0)
        default_std_lead_time = self._get_ss_config("std_lead_time_weeks", 0.0)
        default_order_cycle   = self._get_ss_config("order_cycle_weeks", 2.0)
        history_weeks         = int(self._get_ss_config("history_weeks", 52.0))

        # --- 查詢 ERP：週需求歷史 + 品號主檔（INVMB 品名／單位；供無庫存列之品號補齊顯示）---
        logger.info(f"查詢週需求歷史與品號主檔（{len(item_codes)} 個品號）...")
        with ERPDBHelper(self._config_file) as db:
            weekly_demand = db.get_weekly_demand(item_codes, history_weeks)
            inv_mb = db.get_item_info(item_codes)

        # --- 查詢 ERP：現有 + 在途庫存 ---
        logger.info("查詢現有庫存與在途庫存...")
        iq = InventoryQuery(self._config_file)
        inventory_list = iq.get_inventory()
        inventory_map: Dict[str, Dict] = {r["item_code"]: r for r in inventory_list}

        # --- 需求分群（與「需求分群」工作表相同邏輯）：決定採購用之週需求 D ---
        from .demand_profile import (
            compute_season_profile_metrics,
            load_peak_intervals,
            load_profile_thresholds,
            resolve_demand_for_purchase,
        )

        peak_intervals = load_peak_intervals(self._config)
        profile_thresholds = load_profile_thresholds(self._config)
        trend_en, adj_lo, adj_hi, prev4_floor = self._get_trend_adj_settings()
        overstock_on = self._get_ss_config_bool("overstock_suggestion_enabled", True)
        overstock_xw = self._get_ss_config("overstock_extra_demand_weeks", 6.0)

        # --- 逐品號計算 ---
        now_str = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")
        results: List[Dict] = []

        for params in item_params:
            code = params.get("品號", "").strip()
            if not code:
                continue

            # 個別參數（優先），否則使用全域預設
            service_level = _safe_float(params.get("服務水準"),      default_service_level)
            lead_time     = _safe_float(params.get("前置天數(週)"),  default_lead_time)
            std_lead_time = _safe_float(params.get("提前期標準差_週"), default_std_lead_time)
            order_cycle   = _safe_float(params.get("到貨週期(週)"),  default_order_cycle)
            moq           = _safe_float(params.get("最小採購量"),     1.0)
            if moq < 1:
                moq = 1.0

            # 需求統計
            demand_stats  = weekly_demand.get(code, {})
            avg_demand    = demand_stats.get("avg_weekly_demand", 0.0)
            std_demand    = demand_stats.get("std_weekly_demand", 0.0)  # 標準差(年)欄位顯示（實際由週資料統計）
            data_weeks    = demand_stats.get("data_weeks", 0)

            # 近況比較：各 4 完整週平均（不含序列最後 1 週；見 _four_week_compare_avgs）
            qty_series = [
                float(w.get("qty", 0) or 0) for w in demand_stats.get("weeks", [])
            ]
            last_month_weekly_avg, month_before_last_weekly_avg = (
                _four_week_compare_avgs(qty_series)
            )

            prof = compute_season_profile_metrics(demand_stats, peak_intervals, profile_thresholds)
            seg = resolve_demand_for_purchase(prof, peak_intervals)
            d_segment = seg.d_effective
            adj, adj_display = self._compute_trend_adj(
                last_month_weekly_avg,
                month_before_last_weekly_avg,
                trend_en,
                adj_lo,
                adj_hi,
                prev4_floor,
            )
            # 採用週需求_D = 分群基底 D × 趨勢係數（D 間歇基底為年週均，與 A/C 一致乘 adj）
            d_for_ss = d_segment * adj

            # 庫存數量；品名／單位：試算表 > 庫存彙總 > INVMB 主檔（無庫存在途時仍可有品名）
            inv = inventory_map.get(code, {})
            mb = inv_mb.get(code, {})
            sheet_nm = (params.get("品名") or "").strip()
            item_name = (
                sheet_nm
                or str(inv.get("item_name", "") or "").strip()
                or str(mb.get("item_name", "") or "").strip()
            )
            unit = str(inv.get("unit", "") or "").strip() or str(mb.get("unit", "") or "").strip()
            stock_qty   = inv.get("stock_qty", 0.0)
            transit_qty = inv.get("transit_qty", 0.0)
            total_qty   = inv.get("total_qty", 0.0)

            purchase_mode = _normalize_purchase_mode(params.get("進貨模式"))
            if purchase_mode == "有單才進":
                ss = 0.0
                rop = 0.0
                suggested = 0.0
                purchase_advice = "依訂單"
            else:
                z = _lookup_z(service_level)
                ss = self._calc_safety_stock(z, std_demand, lead_time, std_lead_time, d_for_ss)
                rop = self._calc_rop(d_for_ss, lead_time, ss)
                suggested = self._calc_suggested_qty(
                    d_for_ss, lead_time, order_cycle, ss, stock_qty, moq
                )
                purchase_advice = self._purchase_advice_text(
                    stock_qty,
                    rop,
                    d_for_ss=d_for_ss,
                    lead_time=lead_time,
                    order_cycle=order_cycle,
                    safety_stock=ss,
                    overstock_enabled=overstock_on,
                    overstock_extra_weeks=overstock_xw,
                )

            results.append({
                "更新時間":      now_str,
                "品號":          code,
                "品名":          item_name,
                "進貨模式":      purchase_mode,
                "單位":          unit,
                "現有庫存":      round(stock_qty, 0),
                "在途庫存":      round(transit_qty, 0),
                "合計庫存":      round(total_qty, 0),
                "統計週數":      data_weeks,
                "前4週均(再往前4週)":       round(month_before_last_weekly_avg, 2),
                "近4週均(不含最新週)":       round(last_month_weekly_avg, 2),
                "週平均(年)":    round(avg_demand, 2),
                # 以下兩欄必須同一次 resolve_demand_for_purchase(seg) 產出，邏輯對照見 demand_profile 該函式 docstring
                "需求分群":      seg.mode_label,
                "趨勢係數_adj":  adj_display,
                "採用週需求_D":  round(d_for_ss, 2),
                "需求D來源":     seg.d_source,
                "標準差(年)":     round(std_demand, 2),
                "安全庫存_SS":   round(ss, 2),
                "補貨點_ROP":    round(rop, 2),
                "建議採購量":    round(suggested, 0),
                "進貨建議":      purchase_advice,
            })

            logger.info(
                f"[{code}] 進貨模式={purchase_mode} 分群={seg.mode_label} D基底={d_segment:.1f} "
                f"adj={adj:.3f} D採用={d_for_ss:.1f} STD={std_demand:.1f} SS={ss:.1f} ROP={rop:.1f} "
                f"建議={suggested:.0f} 現有={stock_qty:.0f}"
            )

        logger.info(f"安全庫存計算完成，共 {len(results)} 個品號")
        return results
