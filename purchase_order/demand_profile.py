"""
依過去 N 週出庫與日曆旺季錨點，計算品號分群指標並給出建議模式（A–D）。

日曆旺季（與業務約定一致）：
    區間 [中秋節當日 - days_before_mid_autumn 天, 春節正月初一]（預設 90 天；含端點對應之曆日；
    週粒度以與該區間有交集之週標記為旺季週）。天數由 config [SEASON_ANCHORS] 調整。

建議模式（可經 config [DEMAND_PROFILE] 調門檻）：
    A 穩定全年、B 日曆旺季主導、C 資料與日曆不一致、D 間歇／低頻
"""
from __future__ import annotations

import configparser
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .erp_db_helper import ERPDBHelper
from .safety_stock import _four_week_compare_avgs

logger = logging.getLogger(__name__)

EPS = 1e-9


@dataclass(frozen=True)
class PeakInterval:
    """單一業務旺季（曆日，含端點）"""

    start: dt.date
    end: dt.date


def _parse_date(s: str) -> dt.date:
    t = s.strip()
    if not t:
        raise ValueError("empty date")
    return dt.date.fromisoformat(t[:10])


def load_peak_intervals(config: configparser.ConfigParser) -> List[PeakInterval]:
    """
    從 [SEASON_ANCHORS] 讀取中秋／春節配對，組成旺季區間列表。

    peak_cycle_pairs 格式：多組以分號分隔；組內「中秋|春節」以 | 分隔（春節＝正月初一＝旺季最後一日）。
    例：2025-10-06|2026-02-17
    """
    if not config.has_section("SEASON_ANCHORS"):
        logger.warning("config.ini 無 [SEASON_ANCHORS]，將無日曆旺季標記")
        return []

    raw = config.get("SEASON_ANCHORS", "peak_cycle_pairs", fallback="").strip()
    if not raw:
        return []

    days_before_mid_autumn = config.getint("SEASON_ANCHORS", "days_before_mid_autumn", fallback=90)

    intervals: List[PeakInterval] = []
    for group in raw.split(";"):
        group = group.strip()
        if not group:
            continue
        if "|" not in group:
            logger.warning("略過格式錯誤的 peak_cycle_pairs 片段（需 中秋|春節）: %s", group)
            continue
        left, right = [p.strip() for p in group.split("|", 1)]
        if not left or not right:
            logger.warning("略過不完整配對: %s", group)
            continue
        ma = _parse_date(left)
        sf = _parse_date(right)
        peak_start = ma - dt.timedelta(days=days_before_mid_autumn)
        peak_end = sf
        if peak_start > peak_end:
            logger.warning("旺季起日晚於迄日，略過: %s → %s", peak_start, peak_end)
            continue
        intervals.append(PeakInterval(start=peak_start, end=peak_end))
        logger.info("日曆旺季區間: %s ~ %s（中秋 %s, 春節 %s）", peak_start, peak_end, ma, sf)

    return intervals


def load_profile_thresholds(config: configparser.ConfigParser) -> Dict[str, float]:
    """[DEMAND_PROFILE] 門檻，缺鍵時使用預設。"""
    sec = "DEMAND_PROFILE"
    if not config.has_section(sec):
        return {
            "ratio_seasonal_min": 1.5,
            "cv_stable_max": 0.5,
            "zero_week_intermittent_min": 0.35,
            "mean_intermittent_max": 3.0,
        }
    return {
        "ratio_seasonal_min": config.getfloat(sec, "ratio_seasonal_min", fallback=1.5),
        "cv_stable_max": config.getfloat(sec, "cv_stable_max", fallback=0.5),
        "zero_week_intermittent_min": config.getfloat(
            sec, "zero_week_intermittent_min", fallback=0.35
        ),
        "mean_intermittent_max": config.getfloat(sec, "mean_intermittent_max", fallback=3.0),
    }


def week_intersects_interval(week_start: dt.date, interval: PeakInterval) -> bool:
    """週一至週日與 [interval.start, interval.end] 是否有交集。"""
    week_end = week_start + dt.timedelta(days=6)
    return not (week_end < interval.start or week_start > interval.end)


def week_in_any_peak(week_start: dt.date, intervals: Sequence[PeakInterval]) -> bool:
    return any(week_intersects_interval(week_start, iv) for iv in intervals)


def _trailing_ma4(qtys: List[float]) -> List[Optional[float]]:
    """與週序列對齊的 trailing 4 週移動平均；前 3 週為 None。"""
    n = len(qtys)
    out: List[Optional[float]] = [None] * n
    for i in range(3, n):
        out[i] = sum(qtys[i - 3 : i + 1]) / 4.0
    return out


def ma_peak_week_in_calendar(
    week_starts: List[dt.date],
    qtys: List[float],
    intervals: Sequence[PeakInterval],
) -> Tuple[bool, str]:
    """
    以 4 週移動平均最大值所在週，檢查是否落在任一日曆旺季內。
    若無法計算（週數不足），回傳 (False, 原因說明)。
    """
    if len(qtys) < 4:
        return False, "週數不足4無法算移動平均"
    mas = _trailing_ma4(qtys)
    best_i = -1
    best_v = -1.0
    for i, m in enumerate(mas):
        if m is None:
            continue
        if m > best_v + EPS or (abs(m - best_v) <= EPS and i > best_i):
            best_v = m
            best_i = i
    if best_i < 0:
        return False, "無有效移動平均"
    if not intervals:
        return False, "未定義日曆旺季"
    ok = week_in_any_peak(week_starts[best_i], intervals)
    return ok, f"MA高峰週 {week_starts[best_i]} MA4={best_v:.2f}"


def suggest_mode(
    *,
    ratio_peak_to_off: Optional[float],
    cv: Optional[float],
    zero_week_ratio: float,
    mean_all: float,
    ma_peak_in_calendar: bool,
    thresholds: Dict[str, float],
    has_peak_weeks: bool,
    has_off_weeks: bool,
) -> Tuple[str, str]:
    """
    回傳 (建議模式代碼+簡稱, 簡短說明)。
    """
    rmin = thresholds["ratio_seasonal_min"]
    cv_max = thresholds["cv_stable_max"]
    zmin = thresholds["zero_week_intermittent_min"]
    mean_low = thresholds["mean_intermittent_max"]

    if zero_week_ratio >= zmin or (mean_all <= mean_low and zero_week_ratio >= 0.2):
        return (
            "D 間歇／低頻",
            f"零出庫週占比={zero_week_ratio:.0%} 或 週均極低；不建議單純用全年常態 SS。",
        )

    if not has_peak_weeks or not has_off_weeks:
        return (
            "C 資料與日曆不一致",
            "52 週內未同時涵蓋日曆旺季週與淡季週，無法計算可靠淡旺比；請檢查錨點或拉長回溯。",
        )

    if ratio_peak_to_off is None:
        return "C 資料與日曆不一致", "淡旺季比值無法計算。"

    r = ratio_peak_to_off
    cv_val = cv if cv is not None else 999.0

    if r >= rmin:
        if ma_peak_in_calendar:
            return (
                "B 日曆旺季主導",
                f"淡旺季比≈{r:.2f}（≥{rmin}），且移動平均高峰落在日曆旺季內；適合分淡／旺估 D 或分季參數。",
            )
        return (
            "C 資料與日曆不一致",
            f"淡旺季比高（{r:.2f}）但移動平均高峰與日曆旺季不一致；建議核對出庫別、調撥或錨點年度。",
        )

    inv_r = 1.0 / r if r > EPS else 999.0
    if inv_r >= rmin:
        return (
            "C 資料與日曆不一致",
            f"淡季週均高於旺季（比≈{r:.2f}），與日曆假設相反；請核對資料或品項生命週期。",
        )

    if cv_val <= cv_max:
        return (
            "A 穩定全年",
            f"淡旺比接近 1（{r:.2f}）且 CV≤{cv_max}；可沿用單一週均 D + 現行 SS。",
        )

    return (
        "C 資料與日曆不一致",
        f"淡旺比未達季節性門檻但 CV 偏高（{cv_val:.2f}）；需求波動大，宜檢視異常週或改採保守參數。",
    )


@dataclass(frozen=True)
class SeasonProfileMetrics:
    """與 build_profile_row 相同的分群統計，供進貨建議選用週需求 D。"""

    mode: str
    mode_note: str
    mean_all: float
    peak_avg: float
    off_avg: float
    has_calendar: bool
    ma_peak_in_calendar: bool
    ma_note: str


def compute_season_profile_metrics(
    demand_stats: Dict[str, Any],
    intervals: Sequence[PeakInterval],
    thresholds: Dict[str, float],
) -> SeasonProfileMetrics:
    """
    依週需求序列與日曆錨點計算淡旺週均與建議模式（A/B/C/D）。
    無 [SEASON_ANCHORS] 時 has_calendar=False，模式固定為 C（無日曆分群）。
    """
    weeks = demand_stats.get("weeks") or []
    qtys = [float(w.get("qty", 0) or 0) for w in weeks]
    mean_all = float(demand_stats.get("avg_weekly_demand", 0.0) or 0.0)
    std_all = float(demand_stats.get("std_weekly_demand", 0.0) or 0.0)
    n = len(qtys)
    zero_weeks = sum(1 for q in qtys if q <= EPS)
    zratio = (zero_weeks / n) if n else 0.0

    if not intervals:
        return SeasonProfileMetrics(
            mode="C 資料與日曆不一致",
            mode_note="config.ini 未設定 [SEASON_ANCHORS] peak_cycle_pairs，無法以日曆區分淡旺季。",
            mean_all=mean_all,
            peak_avg=0.0,
            off_avg=mean_all if n else 0.0,
            has_calendar=False,
            ma_peak_in_calendar=False,
            ma_note="未設定日曆旺季",
        )

    week_starts: List[dt.date] = []
    for w in weeks:
        ws = w.get("week_start", "")
        if isinstance(ws, str) and ws.strip():
            week_starts.append(_parse_date(ws))
        else:
            week_starts.append(dt.date.today())

    peak_qty: List[float] = []
    off_qty: List[float] = []
    for i, q in enumerate(qtys):
        if week_in_any_peak(week_starts[i], intervals):
            peak_qty.append(q)
        else:
            off_qty.append(q)

    peak_avg = sum(peak_qty) / len(peak_qty) if peak_qty else 0.0
    off_avg = sum(off_qty) / len(off_qty) if off_qty else 0.0
    if off_avg > EPS:
        ratio_po = peak_avg / off_avg
    elif peak_avg > EPS:
        ratio_po = None
    else:
        ratio_po = 1.0

    cv = (std_all / mean_all) if mean_all > EPS else None
    ma_in_cal, ma_note_str = ma_peak_week_in_calendar(week_starts, qtys, intervals)

    mode, note = suggest_mode(
        ratio_peak_to_off=ratio_po,
        cv=cv,
        zero_week_ratio=zratio,
        mean_all=mean_all,
        ma_peak_in_calendar=ma_in_cal,
        thresholds=thresholds,
        has_peak_weeks=len(peak_qty) > 0,
        has_off_weeks=len(off_qty) > 0,
    )
    return SeasonProfileMetrics(
        mode=mode,
        mode_note=note,
        mean_all=mean_all,
        peak_avg=peak_avg,
        off_avg=off_avg,
        has_calendar=True,
        ma_peak_in_calendar=ma_in_cal,
        ma_note=ma_note_str,
    )


@dataclass(frozen=True)
class SegmentedDemandForPurchase:
    """
    進貨建議採用之週需求 D 與是否計算安全庫存。

    Google Sheets「進貨建議」其中兩欄必須同源、一次產出（勿分開重算）：
        - mode_label → 欄「需求分群」
        - d_source   → 欄「需求D來源」
    兩者對照由 resolve_demand_for_purchase 保證一致；日後若改分群規則請只改該函數（或
    compute_season_profile_metrics / suggest_mode），不要另寫欄位公式以免漂移。
    """

    d_effective: float
    mode_label: str
    d_source: str


def resolve_demand_for_purchase(
    metrics: SeasonProfileMetrics,
    intervals: Sequence[PeakInterval],
    *,
    today: Optional[dt.date] = None,
) -> SegmentedDemandForPurchase:
    """
    依需求分群決定採購計算用之週需求 D 與是否計算 SS/ROP/建議量。

    【進貨建議】需求分群（mode_label）與 需求D來源（d_source）對照（同一回傳一併寫入）：

    | 需求分群（建議模式字串開頭） | 需求D來源（d_source）              | 採用之 D（d_effective）   |
    |------------------------------|-------------------------------------|---------------------------|
    | D …                          | 年週平均×趨勢(間歇／低頻)            | 年週平均（採用D=此值×adj）         |
    | B … 且 has_calendar          | 旺季週均(當前日曆旺季) 或 淡季…     | 旺／淡週均（依本週一是否在日曆旺季） |
    | B … 且非 has_calendar        | 年週平均(B-無日曆區間後備)          | 年週平均（防呆，正常不應出現）     |
    | A …                          | 年週平均（穩定全年）                 | 年週平均                   |
    | C …                          | 年週平均（資料與日曆不一致）         | 年週平均                   |
    | 其他                         | 年週平均                             | 年週平均                   |

    - A／C：D = 年週平均（來源欄用字區分）
    - B：見上表；本週一與 [SEASON_ANCHORS] 旺季區間交集判斷「當前」淡／旺
    - D：仍以年週平均為分群基底 D，與其他群相同乘趨勢係數 adj 後計 SS／ROP／建議採購
         （間歇品項統計上未必服從常態假設，公式為權宜參考，可再搭配營運判斷）
    """
    mean_all = metrics.mean_all
    today_d = today or dt.date.today()
    this_monday = today_d - dt.timedelta(days=today_d.weekday())

    if metrics.mode.startswith("D "):
        return SegmentedDemandForPurchase(
            d_effective=mean_all,
            mode_label=metrics.mode,
            d_source="年週平均×趨勢(間歇／低頻)",
        )

    if metrics.mode.startswith("B "):
        if metrics.has_calendar:
            now_peak = week_in_any_peak(this_monday, intervals)
            if now_peak:
                return SegmentedDemandForPurchase(
                    d_effective=metrics.peak_avg,
                    mode_label=metrics.mode,
                    d_source="旺季週均(當前日曆旺季)",
                )
            return SegmentedDemandForPurchase(
                d_effective=metrics.off_avg,
                mode_label=metrics.mode,
                d_source="淡季週均(當前日曆淡季)",
            )
        logger.warning(
            "需求分群為 B 但 has_calendar=False，D 與來源欄退回年週均（請檢查分群邏輯是否改壞）"
        )
        return SegmentedDemandForPurchase(
            d_effective=mean_all,
            mode_label=metrics.mode,
            d_source="年週平均(B-無日曆區間後備)",
        )

    # A 穩定全年、C 資料與日曆不一致、或無日曆時之 C（皆用年週均，來源欄位區分）
    if metrics.mode.startswith("A "):
        d_src = "年週平均（穩定全年）"
    elif metrics.mode.startswith("C "):
        d_src = "年週平均（資料與日曆不一致）"
    else:
        d_src = "年週平均"
    return SegmentedDemandForPurchase(
        d_effective=mean_all,
        mode_label=metrics.mode,
        d_source=d_src,
    )


def build_profile_row(
    item_code: str,
    item_name: str,
    demand_stats: Dict[str, Any],
    intervals: Sequence[PeakInterval],
    thresholds: Dict[str, float],
) -> Dict[str, Any]:
    weeks = demand_stats.get("weeks") or []
    qtys = [float(w.get("qty", 0) or 0) for w in weeks]
    mean_all = float(demand_stats.get("avg_weekly_demand", 0.0) or 0.0)
    std_all = float(demand_stats.get("std_weekly_demand", 0.0) or 0.0)
    n = len(qtys)
    near4, prev4 = _four_week_compare_avgs(qtys)
    zero_weeks = sum(1 for q in qtys if q <= EPS)
    zratio = (zero_weeks / n) if n else 0.0

    m = compute_season_profile_metrics(demand_stats, intervals, thresholds)

    if not m.has_calendar:
        return {
            "品號": item_code,
            "品名": item_name,
            "統計週數": n,
            "日曆旺季週數": 0,
            "日曆淡季週數": n,
            "旺季週均需求": "",
            "淡季週均需求": round(mean_all, 4) if n else "",
            "淡旺季比值_旺除淡": "",
            "年週平均": round(mean_all, 4),
            "年週標準差": round(std_all, 4),
            "變異係數_CV": round(std_all / mean_all, 4) if mean_all > EPS else "",
            "零出庫週數": zero_weeks,
            "零出庫週占比": round(zratio, 4) if n else 0.0,
            "近4週均_不含最新週": near4,
            "前4週均_再往前4週": prev4,
            "MA4高峰在日曆旺季內": "",
            "MA4高峰說明": m.ma_note,
            "建議模式": m.mode,
            "分群說明": m.mode_note,
        }

    week_starts: List[dt.date] = []
    for w in weeks:
        ws = w.get("week_start", "")
        if isinstance(ws, str) and ws.strip():
            week_starts.append(_parse_date(ws))
        else:
            week_starts.append(dt.date.today())
    peak_qty: List[float] = []
    off_qty: List[float] = []
    for i, q in enumerate(qtys):
        if week_in_any_peak(week_starts[i], intervals):
            peak_qty.append(q)
        else:
            off_qty.append(q)

    off_avg = m.off_avg
    peak_avg = m.peak_avg
    if off_avg > EPS:
        ratio_po = peak_avg / off_avg
    elif peak_avg > EPS:
        ratio_po = None
    else:
        ratio_po = 1.0
    cv = (std_all / mean_all) if mean_all > EPS else None
    ratio_display = "" if ratio_po is None else round(ratio_po, 4)
    cv_display = "" if cv is None else round(cv, 4)

    return {
        "品號": item_code,
        "品名": item_name,
        "統計週數": n,
        "日曆旺季週數": len(peak_qty),
        "日曆淡季週數": len(off_qty),
        "旺季週均需求": round(peak_avg, 4),
        "淡季週均需求": round(off_avg, 4),
        "淡旺季比值_旺除淡": ratio_display,
        "年週平均": round(mean_all, 4),
        "年週標準差": round(std_all, 4),
        "變異係數_CV": cv_display,
        "零出庫週數": zero_weeks,
        "零出庫週占比": round(zratio, 4) if n else 0.0,
        "近4週均_不含最新週": near4,
        "前4週均_再往前4週": prev4,
        "MA4高峰在日曆旺季內": "是" if m.ma_peak_in_calendar else "否",
        "MA4高峰說明": m.ma_note,
        "建議模式": m.mode,
        "分群說明": m.mode_note,
    }


def profile_items(
    item_codes: List[str],
    item_names: Optional[Dict[str, str]],
    config_file: str = "config.ini",
) -> Tuple[List[Dict[str, Any]], str]:
    """
    查 ERP 週需求並回傳每品號分群列與「基準說明」文字（供報表列印）。
    品名欄為 INVMB 主檔（查詢品名）；主檔無資料時使用試算表品號參數的品名。
    """
    cfg = configparser.ConfigParser()
    cfg.read(config_file, encoding="utf-8")
    intervals = load_peak_intervals(cfg)
    thresholds = load_profile_thresholds(cfg)

    history_weeks = 52
    if cfg.has_section("SAFETY_STOCK"):
        history_weeks = int(cfg.getfloat("SAFETY_STOCK", "history_weeks", fallback=52.0))

    names = item_names or {}
    rows: List[Dict[str, Any]] = []

    with ERPDBHelper(config_file) as db:
        weekly = db.get_weekly_demand(item_codes, history_weeks=history_weeks)
        item_info = db.get_item_info(item_codes)

    for code in item_codes:
        stats = weekly.get(code, {})
        erp_name = str(item_info.get(code, {}).get("item_name", "") or "").strip()
        sheet_name = names.get(code, "").strip()
        # 品名：以 ERP 主檔為主；主檔無資料時退回試算表品名
        display_name = erp_name or sheet_name
        row = build_profile_row(
            code,
            display_name,
            stats,
            intervals,
            thresholds,
        )
        rows.append(row)

    lines = [
        "【分群基準】",
        "1) 日曆旺季：每一組 peak_cycle_pairs 為「中秋節日期|隔一循環之春節正月初一」。",
        "   旺季區間 = [中秋當日 - days_before_mid_autumn 天, 春節當日]（曆日含端點）。",
        "   若某週（週一至週日）與該區間有交集，該週標記為「日曆旺季週」。",
        "2) 旺季週均／淡季週均：分別為旺季週與淡季週的出庫量算術平均（零仍計入）。",
        "3) 淡旺季比值：旺季週均 ÷ 淡季週均（淡季極低時不強算比值，改標為異常類 C）。",
        "4) 變異係數 CV：年週標準差 ÷ 年週平均（來自 ERP 與 get_weekly_demand 一致）。",
        "5) 近4週／前4週均：與進貨建議相同定義（不含序列最末 1 週）。",
        "6) MA4 高峰：4 週移動平均最大值所落之週，是否落在日曆旺季內（驗證序列與日曆是否一致）。",
        "7) 建議模式門檻：見 config.ini [DEMAND_PROFILE]（ratio_seasonal_min, cv_stable_max 等）。",
        "",
        f"已載入日曆旺季區間數：{len(intervals)}",
    ]
    baseline = "\n".join(lines)
    return rows, baseline


def profile_rows_to_matrix(rows: List[Dict[str, Any]]) -> List[List[Any]]:
    if not rows:
        return []
    headers = list(rows[0].keys())
    return [headers] + [[r.get(h, "") for h in headers] for r in rows]
