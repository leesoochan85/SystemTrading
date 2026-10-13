"""OpenDART + pykrx collection, separate from the Kiwoom trading process.

Only December-year-end, KRW, ordinary-share-only issuers are supported.
Annual quality ratios preserve the strategy's original annual definitions.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
import hashlib
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

from util.value_quality_data import (
    upsert_fundamental_snapshot, mark_fundamental_invalid, fundamental_is_fresh,
)

REPORTS = {"11013": (3, 31), "11012": (6, 30),
           "11014": (9, 30), "11011": (12, 31)}


class DataUnavailable(ValueError):
    pass


class DartFailure(RuntimeError):
    """Safe error code only: request exceptions can contain the API key URL."""
    pass


def amount(value):
    text = str(value or "").strip().replace(",", "")
    if text in ("", "-", "—"):
        raise DataUnavailable("MISSING_AMOUNT")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise DataUnavailable("INVALID_AMOUNT") from None
    if not result.is_finite():
        raise DataUnavailable("NONFINITE_AMOUNT")
    return result


def normalize_name(value):
    return re.sub(r"[\s()（）]", "", str(value))


ACCOUNTS = {
    "revenue": (("ifrs-full_Revenue", "dart_Revenue", "dart_OperatingRevenue"),
                ("매출액", "수익(매출액)", "매출")),
    "gross": (("ifrs-full_GrossProfit",), ("매출총이익", "매출총이익(손실)")),
    "cost": (("ifrs-full_CostOfSales",), ("매출원가",)),
    "assets": (("ifrs-full_Assets",), ("자산총계",)),
    "profit": (("ifrs-full_ProfitLoss",), ("당기순이익", "당기순이익(손실)")),
    "owner_profit": (("ifrs-full_ProfitLossAttributableToOwnersOfParent",),
        ("지배기업의 소유주에게 귀속되는 당기순이익(손실)",
         "지배기업 소유주지분", "지배기업 소유주에게 귀속되는 당기순이익(손실)")),
    "equity": (("ifrs-full_Equity",), ("자본총계",)),
    "owner_equity": (("ifrs-full_EquityAttributableToOwnersOfParent",),
        ("지배기업의 소유주에게 귀속되는 자본", "지배기업 소유주지분",
         "지배기업의 소유지분")),
}


def account_value(rows, name, field="thstrm_amount"):
    ids, names = ACCOUNTS[name]
    sections = {"BS"} if name in ("assets", "equity", "owner_equity") else {"IS", "CIS"}
    pool = [r for r in rows if r.get("sj_div") in sections]
    matches = [r for r in pool if r.get("account_id") in ids]
    if not matches:
        names = {normalize_name(n) for n in names}
        matches = [r for r in pool if normalize_name(r.get("account_nm")) in names]
    if not matches:
        raise DataUnavailable("MISSING_ACCOUNT:" + name)
    values = set()
    for row in matches:
        if row.get("currency") != "KRW":
            raise DataUnavailable("NON_KRW_OR_UNKNOWN_CURRENCY")
        values.add(amount(row.get(field)))
    if len(values) != 1:
        raise DataUnavailable("AMBIGUOUS_ACCOUNT:" + name)
    return values.pop()


def receipt_numbers(rows, quote_date):
    numbers = sorted({str(r.get("rcept_no", "")) for r in rows})
    if len(numbers) != 1 or not re.fullmatch(r"\d{14}", numbers[0]):
        raise DataUnavailable("INVALID_OR_MIXED_RECEIPT")
    if numbers[0][:8] > quote_date.strftime("%Y%m%d"):
        raise DataUnavailable("REPORT_AFTER_QUOTE_DATE")
    return numbers


def check_ordinary_only(rows, period_end, quote_date):
    receipt_numbers(rows, quote_date)
    common, total = None, None
    for row in rows:
        if row.get("stlm_dt", "").replace("-", "") != period_end.strftime("%Y%m%d"):
            raise DataUnavailable("SHARE_PERIOD_MISMATCH")
        kind = normalize_name(row.get("se"))
        # '-' for an absent share class means no outstanding shares, not
        # missing financial profit. The aggregate must still equal common.
        raw = str(row.get("istc_totqy", "")).strip()
        count = Decimal(0) if raw in ("", "-") else amount(raw)
        if count < 0:
            raise DataUnavailable("INVALID_SHARE_COUNT")
        if kind in ("보통주", "보통주식"):
            if common is not None:
                raise DataUnavailable("AMBIGUOUS_SHARE_CLASS")
            common = count
        elif kind in ("합계", "계"):
            total = count
        elif kind not in ("비고",) and count > 0:
            raise DataUnavailable("MULTIPLE_SHARE_CLASSES")
    if common is None or total is None or common <= 0 or common != total:
        raise DataUnavailable("UNVERIFIED_ORDINARY_SHARES")
    return common


def calculate_snapshot(annual, current, previous, *, year, report_code,
                       fs_div, market_cap, quote_date):
    """All amounts in KRW; all three reports use the same CFS/OFS basis."""
    annual_receipts = receipt_numbers(annual, quote_date)
    current_receipts = receipt_numbers(current, quote_date)
    annual_year = year if report_code == "11011" else year - 1
    annual_end = date(annual_year, 12, 31)
    period_end = date(year, *REPORTS[report_code])
    if not 0 <= (quote_date - annual_end).days <= 550:
        raise DataUnavailable("STALE_ANNUAL_REPORT")
    if not 0 <= (quote_date - period_end).days <= 240:
        raise DataUnavailable("STALE_FINANCIAL_REPORT")
    # Validate report year/code, independently of caller selection.
    for rows, y, code in ((annual, annual_year, "11011"), (current, year, report_code)):
        if any(str(r.get("bsns_year")) != str(y) or r.get("reprt_code") != code for r in rows):
            raise DataUnavailable("REPORT_PERIOD_MISMATCH")
    profit_name = "owner_profit" if fs_div == "CFS" else "profit"
    annual_profit = account_value(annual, profit_name)
    profit = annual_profit
    receipts = annual_receipts + current_receipts
    if report_code != "11011":
        if not previous or any(str(r.get("bsns_year")) != str(year - 1)
                               or r.get("reprt_code") != report_code for r in previous):
            raise DataUnavailable("MISSING_COMPARABLE_PERIOD")
        receipts += receipt_numbers(previous, quote_date)
        # Half-year/Q3 thstrm_amount is just three months. Use YTD amounts.
        def ytd(rows):
            try:
                return account_value(rows, profit_name, "thstrm_add_amount")
            except DataUnavailable as exc:
                if report_code != "11013" or str(exc) != "MISSING_AMOUNT":
                    raise
                # Q1 three-month and YTD periods are identical.
                return account_value(rows, profit_name)
        profit += ytd(current)
        profit -= ytd(previous)
    equity = account_value(current, "owner_equity" if fs_div == "CFS" else "equity")
    revenue = account_value(annual, "revenue")
    try:
        gross = account_value(annual, "gross")
    except DataUnavailable as exc:
        if str(exc) != "MISSING_ACCOUNT:gross":
            raise
        gross = revenue - account_value(annual, "cost")
    assets = account_value(annual, "assets")
    prior_assets = account_value(annual, "assets", "frmtrm_amount")
    cap = amount(market_cap)
    if min(cap, profit, equity, revenue, assets, prior_assets) <= 0:
        raise DataUnavailable("NONPOSITIVE_DENOMINATOR_OR_TTM_PROFIT")
    values = dict(per_ttm=float(cap / profit), pbr=float(cap / equity),
                  gross_margin_pct=float(gross / revenue * 100),
                  asset_turnover=float(revenue / ((assets + prior_assets) / 2)))
    if not all(math.isfinite(v) for v in values.values()):
        raise DataUnavailable("NONFINITE_RATIO")
    return dict(values, as_of_date=quote_date.strftime("%Y%m%d"),
                latest_annual_year=str(annual_year), validated=True,
                financial_period_end=period_end.strftime("%Y%m%d"),
                annual_period_end=annual_end.strftime("%Y%m%d"),
                source_note=json.dumps({"source": "OpenDART+pykrx", "fs_div": fs_div,
                    "report_code": report_code, "receipts": sorted(set(receipts)),
                    "per_basis": "ordinary_market_cap/TTM_owner_profit" if fs_div == "CFS"
                        else "ordinary_market_cap/TTM_profit",
                    "quality_basis": "annual; revenue/mean(beginning,end assets)"}, ensure_ascii=False),
                validation_note="OK")


class DartClient:
    def __init__(self, key, cache_dir, *, sleep=0.3, session=None):
        if not re.fullmatch(r"[A-Za-z0-9]{40}", key or ""):
            raise DartFailure("DART_API_KEY_REQUIRED_40_CHARACTERS")
        self.key = key
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.sleep = sleep
        self.session = session or requests.Session()

    def _request(self, endpoint, params):
        time.sleep(self.sleep)
        try:
            response = self.session.get("https://opendart.fss.or.kr/api/" + endpoint,
                params=dict(params, crtfc_key=self.key), timeout=(5, 30))
        except requests.RequestException as exc:
            raise DartFailure("DART_NETWORK:" + type(exc).__name__) from None
        if response.status_code != 200:
            raise DartFailure("DART_HTTP:" + str(response.status_code))
        return response

    def get(self, endpoint, **params):
        identity = json.dumps([endpoint, params], sort_keys=True)
        path = self.cache / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        if path.exists() and time.time() - path.stat().st_mtime < 86400:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        try:
            payload = self._request(endpoint, params).json()
        except ValueError:
            raise DartFailure("DART_INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise DartFailure("DART_INVALID_PAYLOAD")
        status = str(payload.get("status", ""))
        if status == "013":
            return None
        if status != "000":
            raise DartFailure("DART_STATUS:" + status)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        return payload

    def companies(self):
        path = self.cache / "corp_codes.json"
        if path.exists() and time.time() - path.stat().st_mtime < 86400:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        response = self._request("corpCode.xml", {})
        try:
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                xml = archive.read("CORPCODE.xml")
            root = ET.fromstring(xml)
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            # Error XML/JSON can contain unsafe messages: do not print it.
            raise DartFailure("DART_CORP_DOWNLOAD_FAILED_CHECK_KEY_OR_QUOTA") from None
        result = {}
        for row in root.findall("list"):
            code = (row.findtext("stock_code") or "").strip()
            if re.fullmatch(r"\d{6}", code):
                result[code] = {"corp_code": row.findtext("corp_code"),
                                "code_name": row.findtext("corp_name")}
        if not result:
            raise DartFailure("DART_EMPTY_CORP_CODES")
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    def statements(self, corp, year, report, basis):
        result = self.get("fnlttSinglAcntAll.json", corp_code=corp,
            bsns_year=str(year), reprt_code=report, fs_div=basis)
        return None if result is None else result.get("list")


def collect_company(client, company, quote_date, cap, listed_shares):
    corp = company["corp_code"]
    overview = client.get("company.json", corp_code=corp)
    if not overview or str(overview.get("acc_mt")) != "12":
        raise DataUnavailable("NON_DECEMBER_YEAR_END")
    annual = None
    for year in (quote_date.year - 1, quote_date.year - 2):
        annual = client.statements(corp, year, "11011", "CFS")
        basis = "CFS"
        if not annual:
            annual = client.statements(corp, year, "11011", "OFS")
            basis = "OFS"
        if annual:
            annual_year = year
            break
    if not annual:
        raise DataUnavailable("NO_ANNUAL_REPORT")
    current, previous, y, report = annual, None, annual_year, "11011"
    next_year = annual_year + 1
    for candidate in ("11014", "11012", "11013"):
        if date(next_year, *REPORTS[candidate]) >= quote_date:
            continue
        rows = client.statements(corp, next_year, candidate, basis)
        if rows:
            current, y, report = rows, next_year, candidate
            previous = client.statements(corp, annual_year, candidate, basis)
            break
    # Validate issuer share classes for every profit period used. Ordinary
    # market cap is not interchangeable with the entire issuer's equity
    # when preferred shares exist; exclude those issuers in this version.
    periods = {(annual_year, "11011"), (y, report)}
    if previous is not None:
        periods.add((y - 1, report))
    latest_share_count = None
    for share_year, share_report in sorted(periods):
        data = client.get("stockTotqySttus.json", corp_code=corp,
            bsns_year=str(share_year), reprt_code=share_report)
        if not data or not data.get("list"):
            raise DataUnavailable("NO_SHARE_CLASS_DATA")
        count = check_ordinary_only(data["list"], date(share_year, *REPORTS[share_report]), quote_date)
        if (share_year, share_report) == (y, report):
            latest_share_count = count
    if latest_share_count != amount(listed_shares):
        raise DataUnavailable("SHARE_COUNT_CHANGED_SINCE_REPORT")
    return calculate_snapshot(annual, current, previous, year=y, report_code=report,
        fs_div=basis, market_cap=cap, quote_date=quote_date)


def market_caps(requested_date):
    from importlib.metadata import version, PackageNotFoundError
    try:
        installed = version("pykrx")
    except PackageNotFoundError:
        raise DartFailure("PYKRX_NOT_INSTALLED") from None
    release = tuple(int(v) for v in re.match(r"(\d+)\.(\d+)\.(\d+)", installed).groups())
    if release < (1, 2, 9):
        raise DartFailure("PYKRX_UPGRADE_REQUIRED:" + installed + "; use Python>=3.10 64bit data environment")
    if not os.getenv("KRX_ID") or not os.getenv("KRX_PW"):
        raise DartFailure("KRX_CREDENTIALS_REQUIRED: set KRX_ID and KRX_PW in .env")
    # Import after .env loading: some pykrx auth defaults are bound on import.
    from pykrx import stock
    for shift in range(10):
        day = requested_date - timedelta(days=shift)
        if day.weekday() >= 5:
            continue
        try:
            frame = stock.get_market_cap_by_ticker(day.strftime("%Y%m%d"), market="ALL")
        except KeyError:
            # Old pykrx and unauthenticated/changed responses can lose columns.
            # Do not label malformed responses as holidays or valid empty data.
            raise DartFailure("KRX_RESPONSE_COLUMNS_MISSING: check KRX login/access and pykrx version") from None
        except Exception as exc:
            raise DartFailure("KRX_MARKET_CAP_REQUEST_FAILED:" + type(exc).__name__) from None
        if frame is not None and not frame.empty:
            if not {"시가총액", "상장주식수"}.issubset(frame.columns):
                raise DartFailure("KRX_MARKET_CAP_COLUMNS_CHANGED")
            frame = frame.copy()
            frame.index = frame.index.astype(str).str.zfill(6)
            if frame.index.has_duplicates:
                raise DartFailure("KRX_DUPLICATE_STOCK_CODES")
            # Current DART mapping is numeric-only; do not fail the entire
            # market when KRX contains newly introduced alphanumeric codes.
            frame = frame.loc[[bool(re.fullmatch(r"\d{6}", code)) for code in frame.index]]
            import pandas as pd
            for column in ("시가총액", "상장주식수"):
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            valid = frame["시가총액"].gt(0) & frame["상장주식수"].gt(0)
            valid &= frame["시가총액"].map(lambda v: math.isfinite(float(v)))
            valid &= frame["상장주식수"].map(lambda v: math.isfinite(float(v)) and float(v).is_integer())
            frame = frame.loc[valid]
            if frame.empty:
                continue
            return day, frame
    raise DartFailure("KRX_NO_RECENT_MARKET_CAP")


def run_collection(client, db_path, quote_date, caps, companies, *, codes=None, limit=20):
    selected = list(codes) if codes else sorted(set(companies) & set(caps.index))
    if limit:
        selected = selected[:limit]
    if not selected:
        raise DataUnavailable("NO_COLLECTION_TARGETS")
    summary = {"attempted": 0, "saved": 0, "skipped": 0, "errors": {},
               "quote_date": quote_date.strftime("%Y%m%d")}
    for code in selected:
        summary["attempted"] += 1
        try:
            if code not in companies or code not in caps.index:
                raise DataUnavailable("NO_CORP_OR_MARKET_CAP")
            row = caps.loc[code]
            snapshot = collect_company(client, companies[code], quote_date,
                row["시가총액"], row["상장주식수"])
            if not fundamental_is_fresh(snapshot):
                raise DataUnavailable("STALE_QUOTE_OR_INVALID_METADATA")
            upsert_fundamental_snapshot(code=code, code_name=companies[code]["code_name"],
                db_path=db_path, **snapshot)
            summary["saved"] += 1
            print(f"[ValueCollect] {code}: 재무 지표 저장")
        except (DataUnavailable, DartFailure) as exc:
            reason = str(exc)
            mark_fundamental_invalid(code, reason, db_path=db_path)
            summary["skipped"] += 1
            summary["errors"][code] = reason
            print(f"[ValueCollect] {code}: 제외 / {reason}")
            if isinstance(exc, DartFailure):
                # Stop on auth/quota/network issues instead of hammering API.
                summary["stopped"] = reason
                break
    return summary
