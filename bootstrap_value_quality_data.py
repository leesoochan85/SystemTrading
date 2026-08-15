"""
ValueQualityStrategy용 재무 필터 데이터 입력 도구.

컨센서스 데이터는 더 이상 매도조건에 사용하지 않는다.
이 스크립트는 fundamental snapshot만 value_quality.db에 저장한다.

CSV 컬럼:
code,code_name,as_of_date,per_ttm,pbr,gross_margin_pct,
asset_turnover,latest_annual_year,source_note

사용:
python bootstrap_value_quality_data.py data/value_fundamentals.csv
"""

import csv
import sys
from pathlib import Path

from util.value_quality_data import (
    init_value_quality_tables,
    upsert_fundamental_snapshot,
)


def _optional_float(value):
    text = str(value or "").strip()
    if not text:
        return None
    return float(text.replace(",", ""))


def main():
    init_value_quality_tables()

    if len(sys.argv) != 2:
        print(
            "사용법: python bootstrap_value_quality_data.py "
            "data/value_fundamentals.csv"
        )
        raise SystemExit(2)

    csv_path = Path(sys.argv[1]).resolve()

    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    count = 0

    with open(
        csv_path,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        for row in reader:
            upsert_fundamental_snapshot(
                code=row["code"],
                code_name=(
                    row.get("code_name")
                    or row["code"]
                ),
                as_of_date=row["as_of_date"],
                per_ttm=_optional_float(
                    row.get("per_ttm")
                ),
                pbr=_optional_float(
                    row.get("pbr")
                ),
                gross_margin_pct=_optional_float(
                    row.get("gross_margin_pct")
                ),
                asset_turnover=_optional_float(
                    row.get("asset_turnover")
                ),
                latest_annual_year=(
                    row.get("latest_annual_year")
                    or None
                ),
                source_note=(
                    row.get("source_note")
                    or None
                ),
            )
            count += 1

    print(
        f"[ValueQuality] fundamental "
        f"{count}건 저장 완료"
    )


if __name__ == "__main__":
    main()
