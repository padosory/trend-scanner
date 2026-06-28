"""OpenDART 공시 + 재무지표 수집.

DART_API_KEY 환경변수가 없으면 fetch()가 None을 반환하고 조용히 넘어간다.
법인코드 매핑은 backtest/cache/dart_corp_codes.json에 7일간 캐싱된다.
"""

import io
import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DART_BASE = "https://opendart.fss.or.kr/api"
_CACHE_DIR = Path(__file__).parent.parent / "backtest" / "cache"
_CORP_CODE_CACHE = _CACHE_DIR / "dart_corp_codes.json"
_CORP_CODE_MAX_AGE_DAYS = 7


@dataclass
class DartDisclosure:
    date: str   # YYYY-MM-DD
    title: str
    url: str


@dataclass
class DartData:
    disclosures: list[DartDisclosure] = field(default_factory=list)
    # 재무지표 (최근 연간 기준)
    period_label: str = ""
    revenue: float | None = None          # 매출액 (당기, 억원)
    revenue_growth: float | None = None   # YoY %
    op_income: float | None = None        # 영업이익 (당기, 억원)
    op_income_growth: float | None = None # YoY %
    op_margin: float | None = None        # 영업이익률 %
    debt_ratio: float | None = None       # 부채비율 %
    roe: float | None = None              # ROE %


# ── 법인코드 매핑 ─────────────────────────────────────────────────────────────

def _load_corp_codes(api_key: str) -> dict[str, str]:
    """stock_code(6자리) → corp_code 매핑. JSON 캐시 7일 유효."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if _CORP_CODE_CACHE.exists():
        age = datetime.now() - datetime.fromtimestamp(_CORP_CODE_CACHE.stat().st_mtime)
        if age.days < _CORP_CODE_MAX_AGE_DAYS:
            try:
                return json.loads(_CORP_CODE_CACHE.read_text(encoding="utf-8"))
            except Exception:
                pass

    try:
        resp = requests.get(
            f"{DART_BASE}/corpCode.xml",
            params={"crtfc_key": api_key},
            timeout=30,
        )
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = next(n for n in zf.namelist() if n.endswith(".xml"))
            xml_content = zf.read(xml_name).decode("utf-8")

        root = ET.fromstring(xml_content)
        mapping = {}
        for item in root.findall(".//list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code  = (item.findtext("corp_code") or "").strip()
            if stock_code and corp_code:
                mapping[stock_code] = corp_code

        _CORP_CODE_CACHE.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        logger.info("DART 법인코드 %d건 캐시 완료", len(mapping))
        return mapping

    except Exception as exc:
        logger.warning("DART 법인코드 다운로드 실패: %s", exc)
        return {}


def _get_corp_code(api_key: str, ticker: str) -> str | None:
    return _load_corp_codes(api_key).get(ticker)


# ── 공시 타임라인 ─────────────────────────────────────────────────────────────

def _fetch_disclosures(api_key: str, corp_code: str, scan_date: str) -> list[DartDisclosure]:
    """scan_date 기준 최근 30일 공시 목록."""
    try:
        end_dt   = datetime.strptime(scan_date, "%Y%m%d")
        start_dt = end_dt - timedelta(days=30)

        resp = requests.get(
            f"{DART_BASE}/list.json",
            params={
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bgn_de":    start_dt.strftime("%Y%m%d"),
                "end_de":    end_dt.strftime("%Y%m%d"),
                "page_count": 20,
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("status") != "000":
            return []

        items = []
        for row in data.get("list", []):
            dt = row.get("rcept_dt", "")
            date_str = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}" if len(dt) == 8 else dt
            rcept_no = row.get("rcept_no", "")
            items.append(DartDisclosure(
                date=date_str,
                title=row.get("report_nm", "").strip(),
                url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
            ))
        return items

    except Exception as exc:
        logger.warning("DART 공시 조회 실패 (corp_code=%s): %s", corp_code, exc)
        return []


# ── 재무지표 ──────────────────────────────────────────────────────────────────

def _parse_amount(s) -> float | None:
    """DART 금액 문자열 → 억원. 실패 시 None."""
    try:
        return float(str(s).replace(",", "").strip()) / 1e8
    except (ValueError, TypeError):
        return None


def _find_row(rows: dict, names: list[str]) -> dict | None:
    for name in names:
        if name in rows:
            return rows[name]
    return None


def _try_fetch_year(api_key: str, corp_code: str, bsns_year: str) -> dict | None:
    """특정 연도 연간 재무제표에서 핵심 지표 추출. 연결 우선, 별도 fallback."""
    for fs_div in ("CFS", "OFS"):
        try:
            resp = requests.get(
                f"{DART_BASE}/fnlttSinglAcntAll.json",
                params={
                    "crtfc_key": api_key,
                    "corp_code": corp_code,
                    "bsns_year": bsns_year,
                    "reprt_code": "11011",   # 연간
                    "fs_div": fs_div,
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("status") != "000":
                continue

            rows_list = data.get("list", [])
            if not rows_list:
                continue

            is_rows = {r["account_nm"]: r for r in rows_list if r.get("sj_div") == "IS"}
            bs_rows = {r["account_nm"]: r for r in rows_list if r.get("sj_div") == "BS"}

            rev_row = _find_row(is_rows, ["매출액", "수익(매출액)", "영업수익"])
            op_row  = _find_row(is_rows, ["영업이익", "영업이익(손실)"])
            net_row = _find_row(is_rows, ["당기순이익", "당기순이익(손실)", "연결당기순이익"])
            dbt_row = _find_row(bs_rows, ["부채총계"])
            eq_row  = _find_row(bs_rows, ["자본총계"])

            revenue      = _parse_amount(rev_row.get("thstrm_amount")) if rev_row else None
            revenue_prev = _parse_amount(rev_row.get("frmtrm_amount")) if rev_row else None
            op_income      = _parse_amount(op_row.get("thstrm_amount")) if op_row else None
            op_income_prev = _parse_amount(op_row.get("frmtrm_amount")) if op_row else None
            net_income   = _parse_amount(net_row.get("thstrm_amount")) if net_row else None
            total_debt   = _parse_amount(dbt_row.get("thstrm_amount")) if dbt_row else None
            total_equity = _parse_amount(eq_row.get("thstrm_amount"))  if eq_row  else None

            return {
                "period_label":      f"{bsns_year}년 연간",
                "revenue":           revenue,
                "revenue_growth":    ((revenue / revenue_prev) - 1) * 100
                                     if revenue and revenue_prev else None,
                "op_income":         op_income,
                "op_income_growth":  ((op_income / op_income_prev) - 1) * 100
                                     if op_income and op_income_prev else None,
                "op_margin":         (op_income / revenue) * 100
                                     if op_income and revenue else None,
                "debt_ratio":        (total_debt / total_equity) * 100
                                     if total_debt and total_equity else None,
                "roe":               (net_income / total_equity) * 100
                                     if net_income and total_equity else None,
            }
        except Exception as exc:
            logger.warning("DART 재무 조회 실패 (corp=%s year=%s fs=%s): %s",
                           corp_code, bsns_year, fs_div, exc)
    return None


def _fetch_financials(api_key: str, corp_code: str) -> dict:
    current_year = datetime.now().year
    for year in (current_year - 1, current_year - 2):
        result = _try_fetch_year(api_key, corp_code, str(year))
        if result:
            return result
    return {}


# ── 공개 API ──────────────────────────────────────────────────────────────────

def fetch(api_key: str, ticker: str, scan_date: str) -> "DartData | None":
    """ticker 종목의 공시 + 재무지표 반환. api_key 없거나 실패 시 None."""
    if not api_key:
        return None

    corp_code = _get_corp_code(api_key, ticker)
    if not corp_code:
        logger.debug("DART 법인코드 없음: %s", ticker)
        return None

    disclosures = _fetch_disclosures(api_key, corp_code, scan_date)
    fin         = _fetch_financials(api_key, corp_code)

    return DartData(disclosures=disclosures, **fin)
