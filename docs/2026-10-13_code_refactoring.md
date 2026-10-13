# 미사용 코드 정리 및 리팩토링 — 2026-10-13

후속 DB·주문·잔고 분리는 [별도 작업 기록](2026-10-13_persistence_account_refactoring.md)에 정리했다. 아래 '다음으로 분리할 수 있는 큰 모듈'은 첫 번째 패치 작성 시점의 후보 목록이다.

기준: GitHub `leesoochan85/SystemTrading`의 `main`, 커밋 `24f50dc59c7997c3df5bcea04b0c0463d3aec80c` (2026-10-13 18:42:19 KST).

저장소 전체 Python/React 소스의 import·호출·Qt 콜백·FastAPI 라우트·CLI 진입점과 기존 테스트를 확인했다. 함수 이름이 한 번만 나온다는 이유로 HTTP 엔드포인트나 Qt 콜백을 지우지는 않았다. 저장소 밖에서 별도로 작성한 개인 스크립트의 호출 여부는 확인 범위에 포함되지 않는다.

## 제거한 코드와 근거

| 위치 | 제거 항목 | 확인 근거 / 현재 대체 경로 |
|---|---|---|
| `util/db_helper.py` 최상단 | 전역 `conn`, `cur`, 구형 `balance` 테이블 생성 | 다른 소스에서 사용하는 곳이 없으며 import만 해도 `universe_price.db`가 생성됨. 현재 잔고는 Kiwoom과 `strategy_position.db`·`monitoring.db`로 관리 |
| `util/db_helper.py` | `init_orb_runtime_table`, `save_orb_runtime_state`, `get_orb_runtime_state` | 운영 전략에 ORBStrategy가 없고 외부 호출 없음. 모멘텀의 첫 구간 고가 관측은 별도의 `OpeningRangeTracker`를 사용 |
| `util/db_helper.py` 및 Manager 시작부 | `init_sent_news_table`, `is_news_sent`, `save_sent_news`, `get_last_sent_news_at`, `SENT_NEWS_DB` | 구형 일반 뉴스 기능의 호출 없음. 현재 한경 리포트 전송 이력은 `util/value_quality_data.py`에서 관리. Manager의 불필요한 초기화 호출도 제거 |
| `util/db_helper.py` | `check_table_exists`, `insert_df_to_db`, `execute_sql` | 모든 소스와 테스트에서 호출 없음. 특히 구형 `execute_sql`은 연결을 열고 커서만 반환하던 구조 |
| `util/db_helper.py` | `get_latest_strategy_equity_snapshot`, `get_strategy_equity_snapshots`, `get_position_snapshots` | 호출 없음. 실제 웹 조회는 `web_api/database.py`의 읽기 전용 함수를 사용. 스냅샷 저장과 웹 API는 유지 |
| `util/market_history.py` | `get_current_universe_codes`, `get_missing_history_codes`, `load_daily_price_map`, `load_daily_bar`, `has_completed_daily_bar_after`, 미사용 `MIN_HISTORY_DAYS` | 호출 없음. 현재 엔진은 종목 마스터와 전용 지표 로더를 사용. 마지막 함수는 제거된 눌림목 전략용 |
| `util/breakout_atr.py` | `AtrStateStore` 및 전용 테스트 1개, 미사용 `json` import | 해당 클래스는 테스트에만 남고 운영 호출 없음. A/B는 가상 포지션 상태를, 실거래 모멘텀은 `LiveMomentumStore`를 사용. ATR 계산과 청산 함수·관련 테스트는 유지 |
| `util/make_up_universe.py` | 구형 네이버 시총 상위 100종목 수집기 전체 | 직접 실행용 진입점은 있으나 현재 전략·데이터 수집 BAT·README에서 사용하지 않음. 우량주는 재무 DB, 모멘텀은 공유 시장 DB의 기업주식 마스터를 사용 |
| `demo/my_report.json` | 개인 실행 결과 파일 | 리플레이 출력물. `demo/expected_report.json`과 리플레이 코드는 유지하고 개인 결과 파일은 `.gitignore`에 추가 |
| `.gitignore` | 중복된 동일 패턴 | 같은 패턴의 반복을 정리. 제외 대상은 유지 |

DB 파일을 삭제하거나 기존 DB에서 `DROP TABLE`을 실행하는 처리는 없다. 과거 `orb_runtime_state`, 구형 ATR 상태 및 뉴스 DB가 로컬에 있다면 기존 데이터는 그대로 남는다.

## 리팩토링 전후

| 대상 | 이전 | 변경 후 | 사용자에게 보이는 동작 |
|---|---|---|---|
| `StrategyManager.build_status_message` 및 보조 메서드 | 계좌·주문 운영 클래스에 메시지 구성 코드까지 포함 | `util/status_message.py`의 `StatusMessageBuilder`가 작성. Manager의 공개 `build_status_message()`는 위임 | `/status` 내용 유지. 보유·미체결·비용 미확인 안내 포함 |
| `main.py` | import 시 QApplication 생성, Kiwoom 로그인, 매매 시작 | `main()`으로 이동하고 `if __name__ == '__main__'`에서 실행 | `py -3.9-32 main.py`, `start_trading.bat` 사용법 유지. import만 할 때는 시작하지 않음 |
| `/api/dashboard`, `/api/strategies` | 동일한 전략 요약 계산이 두 곳에 반복 | `_strategy_summaries(event_date)`로 통합 | 기존 응답 키·전략 목록 유지. 대시보드 날짜와 표시 시각은 같은 시각 기준 |
| Dashboard·Momentum·Strategy 페이지 | 페이지마다 타이머·오류·화면 종료 후 결과 무시 로직 반복 | `web_frontend/src/hooks/usePolling.js`로 통합 | 5초 갱신 유지. 요청 처리 중 중복 실행 방지, 화면 종료 후 응답 무시, 오류 발생 시 마지막 정상 데이터 유지 |
| `LegacyStrategyPage` | 우량주 상세 화면에도 Legacy 명칭 사용 | `VirtualStrategyPage`로 이름 정리 | 라우팅·화면 내용 유지 |

신규 주문 제한, 공용현금 선예약, 계좌 최대 10종목·종목당 10%, 손절·부분매도·MA 청산 조건 및 설정 적용일 정책은 변경하지 않았다. 매매와 웹 실행 BAT, 수집기 CLI도 유지했다.

## 유지해야 하는 파일

- `util/breakout_atr.py`, `util/paper_breakout_baseline.py`: A/B 비교계좌 및 모멘텀 ATR 계산에 사용하므로 유지.
- `util/paper_strategy_engine.py`, `util/paper_trading.py`, `util/paper_tick_recorder.py`: A/B/C 계좌, 재시작 복원, 틱 기록에 사용.
- `util/virtual_strategy_engine.py`, `util/virtual_trading.py`: 우량주 웹 가상체결과 실시간 이벤트 경로에 사용.
- `bootstrap_value_quality_data.py`: 자동 수집과 별개로 CSV 입력 경로를 제공하므로 유지.
- `replay_momentum_breakout.py`, `demo/expected_report.json`, `audit_cleanup_virtual_reentries.py`: 각각 재현 검증과 과거 데이터 감사용 독립 CLI. main.py에서 import하지 않아도 용도가 있음.
- 날짜별 `docs/`, 기존 `.patch`, 작업 메모: 실행 코드가 아닌 작업 이력. 과거 변경 내역 확인용으로 유지. 현재 main에 과거 패치를 다시 적용하는 절차를 뜻하지 않음.
- FastAPI 데코레이터 함수, Qt signal 콜백 및 전략 인터페이스: 소스 내 직접 호출이 없어도 외부 요청·이벤트·동적 호출로 실행될 수 있어 유지.

## 다음으로 분리할 수 있는 큰 모듈

이번 변경은 미사용 제거와 입출력 보존을 검증할 수 있는 책임 분리에 집중했다. 다음 항목은 구조 개선 후보이며 이번 패치에는 구현하지 않았다.

| 후보 | 분리 방향 | 추가로 필요한 검증 |
|---|---|---|
| `StrategyManager`의 주문 승인·예약·취소 | 주문 조정 객체로 이동. 동일 Qt 이벤트 흐름과 공용 예약 상태 유지 | 중첩 콜백, 부분체결, 취소 실패, 재조회 중 자금 중복 예약 검증 |
| `StrategyManager`의 잔고 동기화·보유 삭제 | 계좌 상태 동기화 객체로 이동 | 조회 실패 시 기존 상태 보존, 2회 잔고 0 확인, 수동 매도·재시작 검증 |
| `util/db_helper.py` | 포지션·주문/체결·스냅샷 저장 책임별 모듈 분리 | 여러 DB 간 기록 순서와 체결 멱등성 검증, 기존 import 경로 호환 |
| `api/Kiwoom.py`의 TR·Chejan 응답 처리 | 요청 종류별 파서와 상태 반영을 분리 | Windows 32bit QAx, 연속조회·타임아웃·지연 응답·주문 체결 이벤트 검증 |

## 검증 결과와 한계

| 검증 | 결과 |
|---|---|
| 수정 전 전체 unittest | 136개 통과 |
| 수정 후 전체 unittest | 141개 통과: 구형 저장 클래스 전용 테스트 1개 제거, 새 회귀 테스트 6개 추가 |
| 새 Python 회귀 검사 | import 부작용 없음, main의 모멘텀 opt-in 연결 유지, 상태 메시지·빈 계좌·주문 경과시간·API 응답 호환 |
| 수정 전/후 상태 메시지 대조 | 보유·미체결·동기화 여부를 조합한 8개 상황에서 동일 문자열 |
| `npm test` | 4개 통과: 느린 요청 중복 방지, 종료 후 늦은 응답 무시, 오류 후 데이터 유지·복구, 이전 화면 오류 무시 |
| `npm run build` | Vite 프로덕션 빌드 성공 |
| `compileall`, `git diff --check` | 성공 |

테스트는 합성 일봉·가짜 브로커·Qt 대체 인터페이스와 API mocking을 사용했다. Node 테스트는 브라우저 DOM이 아닌 훅의 effect와 상태 갱신 동작을 검증한다. Windows 32bit Kiwoom 로그인·실제 장중 이벤트·실제 주문은 실행하지 않았으므로 실계좌 동작까지 검증했다고 볼 수 없다. 의존성 버전과 거래소 정책은 변경하지 않았으며 최신 금융 규정 검토를 수행한 작업도 아니다.

## 로컬에 패치 적용

GitHub PR 브랜치의 변경을 가져오거나, 제공 ZIP의 `cleanup_refactor_20261013.patch`를 프로젝트 루트에 복사해 적용한다. 두 방법을 동시에 적용할 필요는 없다.

매매·웹 프로세스를 종료한 뒤, 프로젝트 루트 CMD에서:

```cmd
git status
git apply --check cleanup_refactor_20261013.patch
git apply cleanup_refactor_20261013.patch
webenv\Scripts\python.exe -m pip install -r requirements-test.txt
run_tests.bat
cd web_frontend
npm ci
npm test
npm run build
```

패치는 위 기준 커밋을 대상으로 작성했다. 로컬 수정이나 이후 커밋과 겹쳐 `--check`가 실패하면 원인을 확인하기 전 강제 적용하지 않는다. 기존 BAT 실행 순서는 유지한다: `update_market_data.bat` → `start_trading.bat` → `start_web_api.bat` → 프론트엔드 실행.

확인할 검색 키워드: `Python sqlite3 context manager close`, `React useEffect polling cleanup`, `Qt event loop reentrancy`, `Kiwoom Chejan partial fill idempotency`. 기능 설명을 확인할 때에는 현재 사용하는 라이브러리 버전의 공식 문서를 기준으로 한다.
