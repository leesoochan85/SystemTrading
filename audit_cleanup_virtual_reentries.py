from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path.cwd()
DB = ROOT / "monitoring.db"


def connect():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    return con


def table_exists(con, table_name):
    row = con.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def load_round_trips(con):
    """
    virtual_trade의 여러 SELL leg를
    strategy_name + code + entry_at 단위 round-trip으로 묶는다.

    PullbackTrend의 30% + 70%처럼 부분청산 여러 건은
    하나의 진입 거래로 취급한다.
    """
    rows = con.execute(
        """
        SELECT
            strategy_name,
            code,
            MAX(code_name) AS code_name,
            entry_at,
            MIN(entry_price) AS entry_price,

            MIN(exit_at) AS first_exit_at,
            MAX(exit_at) AS last_exit_at,

            SUM(exit_ratio) AS exited_ratio,
            SUM(weighted_return_pct) AS trade_return_pct,

            COUNT(*) AS sell_leg_count,
            GROUP_CONCAT(trade_id) AS trade_ids,

            MIN(sell_reason_code) AS first_sell_reason_code,
            MAX(sell_reason_code) AS last_sell_reason_code
        FROM virtual_trade
        GROUP BY
            strategy_name,
            code,
            entry_at
        ORDER BY
            strategy_name,
            code,
            entry_at
        """
    ).fetchall()

    result = []

    for row in rows:
        item = dict(row)
        item["entry_date"] = str(
            item.get("entry_at") or ""
        )[:8]
        item["exit_date"] = str(
            item.get("last_exit_at") or ""
        )[:8]
        item["trade_ids_list"] = [
            int(value)
            for value in str(
                item.get("trade_ids") or ""
            ).split(",")
            if str(value).strip()
        ]
        item["is_completed"] = (
            float(item.get("exited_ratio") or 0)
            >= 0.999999
        )
        result.append(item)

    return result


def analyze_reentries(round_trips):
    """
    같은 strategy + code + entry_date에서
    완료 round-trip이 여러 개면 최초 1개만 유지하고
    이후 완료 round-trip을 반복 재진입으로 판정한다.
    """
    grouped = defaultdict(list)

    for item in round_trips:
        if not item["is_completed"]:
            continue
        if not item["entry_date"]:
            continue

        key = (
            item["strategy_name"],
            item["code"],
            item["entry_date"],
        )
        grouped[key].append(item)

    repeated_groups = []
    repeated_round_trips = []
    delete_trade_ids = []

    for key, items in grouped.items():
        items.sort(
            key=lambda x: (
                str(x.get("entry_at") or ""),
                int(
                    x["trade_ids_list"][0]
                    if x["trade_ids_list"]
                    else 0
                ),
            )
        )

        if len(items) <= 1:
            continue

        keep = items[0]
        repeated = items[1:]

        repeated_groups.append({
            "strategy_name": key[0],
            "code": key[1],
            "entry_date": key[2],
            "round_trip_count": len(items),
            "repeat_count": len(repeated),
            "kept_entry_at": keep["entry_at"],
            "code_name": keep.get("code_name") or "",
        })

        for sequence, item in enumerate(
            repeated,
            start=2,
        ):
            row = dict(item)
            row["sequence_in_day"] = sequence
            repeated_round_trips.append(row)
            delete_trade_ids.extend(
                item["trade_ids_list"]
            )

    return (
        repeated_groups,
        repeated_round_trips,
        sorted(set(delete_trade_ids)),
    )


def analyze_exact_like_duplicates(con):
    """
    화면에서 보이는 '거의 같은 가격/사유 SELL 반복' 정도를
    참고용으로 집계한다.

    삭제 기준은 이것을 사용하지 않는다.
    서로 다른 정상 진입이 우연히 같은 가격일 수 있기 때문이다.
    """
    rows = con.execute(
        """
        SELECT
            strategy_name,
            code,
            MAX(code_name) AS code_name,
            substr(exit_at, 1, 8) AS exit_date,
            ROUND(entry_price, 4) AS entry_price,
            ROUND(exit_price, 4) AS exit_price,
            ROUND(exit_ratio, 6) AS exit_ratio,
            sell_reason_code,
            COUNT(*) AS row_count,
            MIN(exit_at) AS first_exit_at,
            MAX(exit_at) AS last_exit_at
        FROM virtual_trade
        GROUP BY
            strategy_name,
            code,
            substr(exit_at, 1, 8),
            ROUND(entry_price, 4),
            ROUND(exit_price, 4),
            ROUND(exit_ratio, 6),
            sell_reason_code
        HAVING COUNT(*) > 1
        ORDER BY row_count DESC
        """
    ).fetchall()

    return [dict(row) for row in rows]


def find_repeated_open_positions(
    con,
    completed_round_trips,
):
    """
    이미 오늘 완료된 round-trip이 있는데
    같은 날 다시 열린 virtual_position이 있으면
    현재 진행 중인 반복 재진입 후보로 본다.
    """
    completed_keys = set()

    for item in completed_round_trips:
        if not item["is_completed"]:
            continue
        completed_keys.add((
            item["strategy_name"],
            item["code"],
            item["entry_date"],
        ))

    rows = con.execute(
        """
        SELECT *
        FROM virtual_position
        ORDER BY strategy_name, code
        """
    ).fetchall()

    repeated_open = []

    for row in rows:
        item = dict(row)
        entry_date = str(
            item.get("entry_at") or ""
        )[:8]

        key = (
            item["strategy_name"],
            item["code"],
            entry_date,
        )

        if key in completed_keys:
            repeated_open.append(item)

    return repeated_open


def save_csv(path, rows, fieldnames):
    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def print_top_groups(groups, limit=20):
    print()
    print("[반복 재진입 상위 그룹]")

    if not groups:
        print("- 없음")
        return

    sorted_groups = sorted(
        groups,
        key=lambda x: (
            -int(x["repeat_count"]),
            x["strategy_name"],
            x["code"],
            x["entry_date"],
        ),
    )

    for item in sorted_groups[:limit]:
        print(
            f"- {item['strategy_name']} / "
            f"{item['code_name']}({item['code']}) / "
            f"{item['entry_date']} / "
            f"당일 {item['round_trip_count']}회 진입 "
            f"-> 반복 {item['repeat_count']}회"
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "monitoring.db 가상매매 당일 반복 재진입 감사/정리"
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "감사 결과의 2번째 이후 당일 완료 round-trip을 "
            "실제 DB에서 삭제한다."
        ),
    )
    parser.add_argument(
        "--delete-repeated-open",
        action="store_true",
        help=(
            "--apply와 함께 사용. 같은 날 이미 완료 거래가 있는데 "
            "다시 열린 현재 가상포지션도 삭제한다."
        ),
    )
    args = parser.parse_args()

    if not DB.exists():
        raise FileNotFoundError(
            f"monitoring.db를 찾을 수 없습니다: {DB}\n"
            "SystemTrading 프로젝트 최상위 폴더에서 실행하세요."
        )

    with connect() as con:
        if not table_exists(
            con,
            "virtual_trade",
        ):
            raise RuntimeError(
                "virtual_trade 테이블이 없습니다."
            )

        if not table_exists(
            con,
            "virtual_position",
        ):
            raise RuntimeError(
                "virtual_position 테이블이 없습니다."
            )

        total_sell_legs = int(
            con.execute(
                "SELECT COUNT(*) FROM virtual_trade"
            ).fetchone()[0]
        )

        round_trips = load_round_trips(con)

        completed_round_trips = [
            item
            for item in round_trips
            if item["is_completed"]
        ]

        (
            repeated_groups,
            repeated_round_trips,
            delete_trade_ids,
        ) = analyze_reentries(round_trips)

        exact_like = analyze_exact_like_duplicates(
            con
        )

        repeated_open = (
            find_repeated_open_positions(
                con,
                round_trips,
            )
        )

        repeated_sell_legs = sum(
            len(item["trade_ids_list"])
            for item in repeated_round_trips
        )

        print("=" * 72)
        print("가상매매 반복 재진입 감사 결과")
        print("=" * 72)
        print(f"DB: {DB}")
        print()
        print(
            f"virtual_trade 전체 SELL leg: "
            f"{total_sell_legs:,}건"
        )
        print(
            f"진입단위 round-trip 전체: "
            f"{len(round_trips):,}건"
        )
        print(
            f"100% 완료 round-trip: "
            f"{len(completed_round_trips):,}건"
        )
        print()
        print(
            "같은 전략+종목+같은 날에 "
            "2회 이상 완료 진입한 그룹: "
            f"{len(repeated_groups):,}개"
        )
        print(
            f"2번째 이후 반복 완료 round-trip: "
            f"{len(repeated_round_trips):,}건"
        )
        print(
            "반복 round-trip에 포함된 SELL leg: "
            f"{repeated_sell_legs:,}건"
        )
        print(
            "같은 날 완료 후 다시 열려 있는 "
            "현재 반복 포지션 후보: "
            f"{len(repeated_open):,}건"
        )
        print(
            "동일 날짜/가격/비중/매도사유가 "
            "2회 이상 나타난 참고 그룹: "
            f"{len(exact_like):,}개"
        )

        if completed_round_trips:
            pct = (
                len(repeated_round_trips)
                / len(completed_round_trips)
                * 100.0
            )
        else:
            pct = 0.0

        print(
            f"완료 거래 중 당일 반복 재진입 비율: "
            f"{pct:.2f}%"
        )

        print_top_groups(
            repeated_groups,
            limit=25,
        )

        stamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        group_csv = (
            ROOT
            / f"virtual_repeat_groups_{stamp}.csv"
        )
        trade_csv = (
            ROOT
            / f"virtual_repeat_roundtrips_{stamp}.csv"
        )
        exact_csv = (
            ROOT
            / f"virtual_exact_like_duplicates_{stamp}.csv"
        )
        open_csv = (
            ROOT
            / f"virtual_repeated_open_positions_{stamp}.csv"
        )

        save_csv(
            group_csv,
            repeated_groups,
            [
                "strategy_name",
                "code",
                "code_name",
                "entry_date",
                "round_trip_count",
                "repeat_count",
                "kept_entry_at",
            ],
        )

        trade_rows = []
        for item in repeated_round_trips:
            row = dict(item)
            row["trade_ids"] = ",".join(
                str(x)
                for x in item["trade_ids_list"]
            )
            trade_rows.append(row)

        save_csv(
            trade_csv,
            trade_rows,
            [
                "strategy_name",
                "code",
                "code_name",
                "entry_date",
                "sequence_in_day",
                "entry_at",
                "entry_price",
                "first_exit_at",
                "last_exit_at",
                "exited_ratio",
                "trade_return_pct",
                "sell_leg_count",
                "trade_ids",
                "first_sell_reason_code",
                "last_sell_reason_code",
            ],
        )

        save_csv(
            exact_csv,
            exact_like,
            [
                "strategy_name",
                "code",
                "code_name",
                "exit_date",
                "entry_price",
                "exit_price",
                "exit_ratio",
                "sell_reason_code",
                "row_count",
                "first_exit_at",
                "last_exit_at",
            ],
        )

        save_csv(
            open_csv,
            repeated_open,
            [
                "strategy_name",
                "code",
                "code_name",
                "entry_price",
                "entry_at",
                "remaining_ratio",
                "current_price",
                "unrealized_return_pct",
                "buy_reason_code",
                "buy_reason",
                "updated_at",
            ],
        )

        print()
        print("[감사 CSV 생성]")
        print(f"- {group_csv.name}")
        print(f"- {trade_csv.name}")
        print(f"- {exact_csv.name}")
        print(f"- {open_csv.name}")

    if not args.apply:
        print()
        print(
            "[DRY RUN] DB는 수정하지 않았습니다."
        )
        print(
            "결과가 의도와 맞으면 프로그램을 종료한 뒤:"
        )
        print(
            "  py audit_cleanup_virtual_reentries.py --apply "
            "--delete-repeated-open"
        )
        return

    backup = (
        ROOT
        / (
            "monitoring.before_virtual_repeat_cleanup_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        )
    )

    shutil.copy2(DB, backup)
    print()
    print(f"[DB 백업] {backup}")

    with connect() as con:
        con.execute("BEGIN IMMEDIATE")

        if delete_trade_ids:
            placeholders = ",".join(
                "?"
                for _ in delete_trade_ids
            )
            con.execute(
                f"""
                DELETE FROM virtual_trade
                WHERE trade_id IN ({placeholders})
                """,
                delete_trade_ids,
            )

        deleted_open = 0

        if (
            args.delete_repeated_open
            and repeated_open
        ):
            for item in repeated_open:
                cur = con.execute(
                    """
                    DELETE FROM virtual_position
                    WHERE strategy_name = ?
                      AND code = ?
                      AND entry_at = ?
                    """,
                    (
                        item["strategy_name"],
                        item["code"],
                        item["entry_at"],
                    ),
                )
                deleted_open += cur.rowcount

        # 이미 생성된 일별 파생지표가 있다면
        # 정리 전 virtual_trade/position을 기준으로 계산됐을 수 있으므로
        # 재계산 전까지 잘못된 수치를 노출하지 않게 초기화한다.
        if table_exists(
            con,
            "virtual_strategy_daily_snapshot",
        ):
            con.execute(
                "DELETE FROM virtual_strategy_daily_snapshot"
            )

        con.commit()

    print()
    print("=" * 72)
    print("정리 완료")
    print("=" * 72)
    print(
        f"삭제한 반복 virtual_trade SELL leg: "
        f"{len(delete_trade_ids):,}건"
    )
    print(
        f"삭제한 현재 반복 가상포지션: "
        f"{deleted_open:,}건"
    )
    print(
        "virtual_strategy_daily_snapshot은 "
        "기존 데이터가 있으면 초기화했습니다."
    )
    print()
    print(
        "주의: main.py가 실행 중이었다면 메모리 가상포지션 캐시가 "
        "DB를 다시 덮어쓸 수 있습니다."
    )
    print(
        "정리는 반드시 main.py 종료 상태에서 실행하세요."
    )


if __name__ == "__main__":
    main()
