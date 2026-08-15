from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from typing import Iterable, Optional

import requests


REPORT_VIEW_BASE = "https://markets.hankyung.com/consensus/view/"
DEFAULT_REPORT_ID_SEED = 651715

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.5",
    "Referer": "https://markets.hankyung.com/consensus",
    "Connection": "keep-alive",
}


class _PageParser(HTMLParser):
    """
    한 번의 HTML 파싱으로 다음을 수집한다.

    - visible text
    - meta name/property
    - title
    - application/ld+json
    - __NEXT_DATA__
    """
    def __init__(self):
        super().__init__()
        self.visible_parts = []
        self.meta = {}
        self.title_parts = []

        self._skip_depth = 0
        self._in_title = False
        self._script_type = ""
        self._script_id = ""
        self._script_parts = []

        self.json_scripts = []
        self.next_data_scripts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {
            str(k).lower(): (v or "")
            for k, v in attrs
        }

        if tag == "meta":
            key = (
                attr.get("property")
                or attr.get("name")
                or ""
            ).strip().lower()
            value = attr.get("content", "").strip()

            if key and value:
                self.meta[key] = value
            return

        if tag == "title":
            self._in_title = True
            return

        if tag == "script":
            self._skip_depth += 1
            self._script_type = (
                attr.get("type", "")
                .strip()
                .lower()
            )
            self._script_id = (
                attr.get("id", "")
                .strip()
            )
            self._script_parts = []
            return

        if tag in ("style", "noscript"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "title":
            self._in_title = False
            return

        if tag == "script":
            raw = "".join(
                self._script_parts
            ).strip()

            if raw:
                if (
                    self._script_type
                    == "application/ld+json"
                ):
                    self.json_scripts.append(raw)

                if self._script_id == "__NEXT_DATA__":
                    self.next_data_scripts.append(raw)

            self._script_type = ""
            self._script_id = ""
            self._script_parts = []

            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if (
            tag in ("style", "noscript")
            and self._skip_depth > 0
        ):
            self._skip_depth -= 1

    def handle_data(self, data):
        text = str(data or "")

        if self._in_title:
            if text.strip():
                self.title_parts.append(
                    text.strip()
                )
            return

        if self._script_type or self._script_id:
            self._script_parts.append(text)
            return

        if self._skip_depth:
            return

        text = text.strip()
        if text:
            self.visible_parts.append(text)

    @property
    def visible_text(self):
        return "\n".join(
            self.visible_parts
        )

    @property
    def page_title(self):
        return " ".join(
            self.title_parts
        ).strip()


def _clean(value):
    if value is None:
        return ""

    text = unescape(str(value))
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def _normalize_date(value):
    text = _clean(value)

    match = re.search(
        r"(20\d{2})[-./년\s]+"
        r"(\d{1,2})[-./월\s]+"
        r"(\d{1,2})",
        text,
    )

    if not match:
        match = re.search(
            r"(?<!\d)"
            r"(20\d{2})(\d{2})(\d{2})"
            r"(?!\d)",
            text,
        )

    if not match:
        return ""

    year, month, day = match.groups()

    return (
        f"{int(year):04d}"
        f"{int(month):02d}"
        f"{int(day):02d}"
    )


def _search_label(text, labels):
    """
    반드시 '라벨: 값' 형태만 허용한다.

    meta description은 한 줄 안에서:
      작성자: 전배승. 발행기관: LS증권. 목표주가: ...
    형태가 될 수 있으므로 '.', '·', 줄바꿈, 다음 '라벨:' 직전에서 값을 끊는다.

    일반 날짜 fallback은 절대 하지 않는다.
    """
    for label in labels:
        escaped = re.escape(label)

        match = re.search(
            rf"(?:^|[\n.·])\s*{escaped}\s*"
            rf"[:：]\s*"
            rf"(.+?)"
            rf"(?="
            rf"\s*(?:[.·]\s*[\w가-힣 ()/+-]+\s*[:：]|"
            rf"\n|$)"
            rf")",
            text,
            re.I,
        )

        if match:
            return _clean(
                match.group(1)
            ).rstrip(" .·")

    return ""


def _walk_json(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from _walk_json(child)

    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _first_json_value(
    roots,
    candidate_keys,
):
    candidate_keys = {
        key.lower()
        for key in candidate_keys
    }

    for root in roots:
        for obj in _walk_json(root):
            lower_map = {
                str(key).lower(): value
                for key, value in obj.items()
            }

            for key in candidate_keys:
                value = lower_map.get(key)

                if value not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    return _clean(value)

    return ""


def _load_json_scripts(parser):
    roots = []

    for raw in (
        parser.json_scripts
        + parser.next_data_scripts
    ):
        try:
            roots.append(
                json.loads(raw)
            )
        except Exception:
            continue

    return roots


def _combined_metadata_text(parser):
    """
    검색엔진이 사용하는 description 계열을 우선 사용한다.

    실제 651534 검색 결과에서도:
    한국금융지주(071050) ...
    작성자: 전배승.
    발행기관: LS증권.
    목표주가: 300,000원 ...
    형태가 노출된다.
    """
    values = []

    for key in (
        "description",
        "og:description",
        "twitter:description",
    ):
        value = parser.meta.get(key)

        if value:
            values.append(
                _clean(value)
            )

    return "\n".join(values)


def _extract_code(
    parser,
    json_roots,
):
    # 구조화 데이터 우선
    value = _first_json_value(
        json_roots,
        [
            "stockCode",
            "stock_code",
            "itemCode",
            "item_code",
            "code",
            "symbol",
        ],
    )

    match = re.search(
        r"(?<!\d)(\d{6})(?!\d)",
        value,
    )

    if match:
        return match.group(1)

    sources = [
        _combined_metadata_text(parser),
        parser.page_title,
        parser.visible_text,
    ]

    for source in sources:
        match = re.search(
            r"\((\d{6})\)",
            source,
        )

        if match:
            return match.group(1)

    # 마지막 수단도 6자리 숫자를 아무 데서나 잡지 않고
    # 제목/description에서만 허용한다.
    for source in (
        _combined_metadata_text(parser),
        parser.page_title,
    ):
        match = re.search(
            r"(?<!\d)(\d{6})(?!\d)",
            source,
        )

        if match:
            return match.group(1)

    return ""


def _extract_title(
    parser,
    json_roots,
    code,
):
    title = _first_json_value(
        json_roots,
        [
            "reportTitle",
            "report_title",
            "title",
            "subject",
        ],
    )

    if title:
        return _clean_report_title(
            title,
            code,
        )

    # og:title이 일반 title보다 우선
    title = (
        parser.meta.get("og:title")
        or parser.meta.get("twitter:title")
        or parser.page_title
    )

    return _clean_report_title(
        title,
        code,
    ) or "기업 리포트"


def _clean_report_title(
    title,
    code,
):
    title = _clean(title)

    # '한국금융지주071050 상반기...' 또는
    # '한국금융지주(071050) 상반기...' 앞부분 제거
    if code:
        title = re.sub(
            rf"^.*?(?:\({re.escape(code)}\)|"
            rf"{re.escape(code)})\s*",
            "",
            title,
            count=1,
        ).strip()

    # 사이트 suffix 제거
    title = re.sub(
        r"\s*[|\-]\s*(?:한국경제|"
        r"시장종합(?:\s*-\s*한국경제)?)\s*$",
        "",
        title,
        flags=re.I,
    ).strip()

    return title


def _extract_report_date(
    parser,
    json_roots,
    metadata_text,
):
    # 1. 구조화/Next 데이터
    raw = _first_json_value(
        json_roots,
        [
            "reportDate",
            "report_date",
            "publishDate",
            "publish_date",
            "publishedAt",
            "published_at",
            "writeDate",
            "write_date",
            "regDate",
            "reg_date",
        ],
    )

    date = _normalize_date(raw)

    if date:
        return date

    # 2. 메타 description 안에서 '발표일:'이 명시된 경우만
    raw = _search_label(
        metadata_text,
        [
            "발표일",
            "리포트일",
            "작성일",
        ],
    )

    date = _normalize_date(raw)

    if date:
        return date

    # 3. 본문에서도 반드시 라벨이 있을 때만
    raw = _search_label(
        parser.visible_text,
        [
            "발표일",
            "리포트일",
            "작성일",
        ],
    )

    return _normalize_date(raw)


def _extract_field(
    parser,
    json_roots,
    metadata_text,
    json_keys,
    labels,
):
    value = _first_json_value(
        json_roots,
        json_keys,
    )

    if value:
        return value

    value = _search_label(
        metadata_text,
        labels,
    )

    if value:
        return value

    return _search_label(
        parser.visible_text,
        labels,
    )


def parse_hankyung_report_detail_html(
    html: str,
    report_id: int | str,
) -> Optional[dict]:
    parser = _PageParser()
    parser.feed(html or "")
    parser.close()

    if (
        not parser.visible_text
        and not parser.page_title
        and not parser.meta
    ):
        return None

    error_source = (
        parser.visible_text
        + "\n"
        + parser.page_title
    ).lower()

    if (
        "페이지를 찾을 수 없습니다"
        in error_source
        or "존재하지 않는 페이지"
        in error_source
        or "not found" in error_source
    ):
        return None

    json_roots = _load_json_scripts(
        parser
    )
    metadata_text = (
        _combined_metadata_text(parser)
    )

    code = _extract_code(
        parser,
        json_roots,
    )

    # 기업 리포트가 아니면 스킵
    if not code:
        return None

    title = _extract_title(
        parser,
        json_roots,
        code,
    )

    broker = _extract_field(
        parser,
        json_roots,
        metadata_text,
        [
            "brokerName",
            "broker_name",
            "securitiesName",
            "securities_name",
            "companyName",
            "company_name",
            "publisher",
        ],
        [
            "발행기관",
            "증권사",
        ],
    )

    analyst = _extract_field(
        parser,
        json_roots,
        metadata_text,
        [
            "analystName",
            "analyst_name",
            "writerName",
            "writer_name",
            "author",
            "writer",
        ],
        [
            "작성자",
            "애널리스트",
        ],
    )

    opinion = _extract_field(
        parser,
        json_roots,
        metadata_text,
        [
            "gradeName",
            "grade_name",
            "investmentOpinion",
            "investment_opinion",
            "opinion",
        ],
        [
            "투자의견",
            "의견",
        ],
    )

    target_price = _extract_field(
        parser,
        json_roots,
        metadata_text,
        [
            "targetPrice",
            "target_price",
            "goalPrice",
            "goal_price",
        ],
        [
            "목표주가",
            "목표가",
        ],
    )

    report_date = _extract_report_date(
        parser,
        json_roots,
        metadata_text,
    )

    # 중요:
    # report_date가 비어 있어도 정상 기업 리포트로는 인정한다.
    # 잘못된 푸터 날짜를 넣는 것보다 빈 값이 안전하다.
    report_id_text = str(
        report_id
    ).strip()

    return {
        "report_key": (
            f"hankyung:{report_id_text}"
        ),
        "report_id": report_id_text,
        "code": code,
        "title": title,
        "broker": broker,
        "analyst": analyst,
        "report_date": report_date,
        "opinion": opinion,
        "target_price": target_price,
        "url": (
            REPORT_VIEW_BASE
            + report_id_text
        ),
    }


def fetch_hankyung_report_detail(
    report_id: int | str,
    timeout: int = 10,
    session: Optional[
        requests.Session
    ] = None,
) -> Optional[dict]:
    report_id = int(report_id)

    own_session = session is None
    session = (
        session
        or requests.Session()
    )

    try:
        response = session.get(
            REPORT_VIEW_BASE
            + str(report_id),
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )

        if response.status_code in (
            404,
            410,
        ):
            return None

        response.raise_for_status()

        return (
            parse_hankyung_report_detail_html(
                response.text,
                report_id,
            )
        )

    finally:
        if own_session:
            session.close()


def scan_new_hankyung_reports(
    target_codes: Iterable[str],
    last_scanned_report_id: int | None = None,
    max_scan_count: int = 120,
    consecutive_miss_limit: int = 15,
    request_interval_seconds: float = 0.05,
    timeout: int = 10,
):
    target_codes = {
        str(code).strip().zfill(6)
        for code in target_codes
    }

    if not target_codes:
        return {
            "reports": [],
            "last_scanned_report_id": int(
                last_scanned_report_id
                or DEFAULT_REPORT_ID_SEED
            ),
            "request_count": 0,
        }

    current_id = int(
        last_scanned_report_id
        if last_scanned_report_id
        is not None
        else DEFAULT_REPORT_ID_SEED
    )

    highest_valid_id = current_id
    reports = []
    request_count = 0
    consecutive_misses = 0

    session = requests.Session()

    try:
        for _ in range(
            max(1, int(max_scan_count))
        ):
            report_id = current_id + 1

            try:
                report = (
                    fetch_hankyung_report_detail(
                        report_id,
                        timeout=timeout,
                        session=session,
                    )
                )

            except requests.HTTPError as exc:
                status = (
                    exc.response.status_code
                    if exc.response is not None
                    else None
                )

                if status == 403:
                    raise RuntimeError(
                        "한경 공개 리포트 상세 페이지에서 "
                        "403이 발생했습니다."
                    ) from exc

                if (
                    status
                    and status >= 500
                ):
                    break

                report = None

            request_count += 1

            if report is None:
                consecutive_misses += 1

                if (
                    consecutive_misses
                    >= int(
                        consecutive_miss_limit
                    )
                ):
                    break

                current_id = report_id

                if (
                    request_interval_seconds
                    > 0
                ):
                    time.sleep(
                        request_interval_seconds
                    )

                continue

            highest_valid_id = report_id
            current_id = report_id
            consecutive_misses = 0

            if (
                report["code"]
                in target_codes
            ):
                reports.append(report)

            if (
                request_interval_seconds
                > 0
            ):
                time.sleep(
                    request_interval_seconds
                )

    finally:
        session.close()

    reports.sort(
        key=lambda item: (
            item.get(
                "report_date",
                "",
            ),
            int(
                item.get(
                    "report_id",
                    0,
                )
                or 0
            ),
        )
    )

    return {
        "reports": reports,
        "last_scanned_report_id": (
            highest_valid_id
        ),
        "request_count": request_count,
    }
