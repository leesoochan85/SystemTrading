# 180봉 과거 이력 보충 및 전략 초기화 실패 중 계좌 동기화 유지

## 변경 사항

1. 일봉 수집기의 기본 과거 조회 범위를 400달력일, 보충 목표를 180완결봉으로 변경했다.
2. 최근 날짜까지 저장된 종목도 목표 봉 수가 부족하면 과거 구간을 조회한다.
3. 목표를 충족한 종목만 최신 여부에 따라 수집 생략/최근 증분 보충으로 나눈다.
4. 당일 및 미래 날짜는 목표 완결봉 수에서 제외한다.
5. 기존 날짜는 기존 PRIMARY KEY(code,date) 및 INSERT OR REPLACE를 사용한다. 기존 DB 전체를 삭제하지 않는다.
6. 신규 상장 등으로 실제 이력이 짧으면 부족 종목으로 보고하며 가짜 봉을 만들지 않는다.
7. 기존 다른 함수의 MIN_HISTORY_DAYS=65는 유지한다. 수집 목표만 별도 설정으로 분리했다.
8. `--min-history-rows` 옵션을 추가했다. 기본값은 180이다.
9. 전략 초기화 실패 중에도 정상 미체결/잔고 조회 후 수동매도 수량 보정과 전량매도 포지션 정리를 실행한다.
10. 시작 시에도 전량매도 확인을 1회 수행한다. 전량매도 삭제는 정상 조회에서 잔고 0을 2회 확인하는 기존 정책을 유지한다.
11. 대기 모드에서 신규 주문/취소를 호출하지 않으며 is_initialized와 market_data_ready를 활성화하지 않는다.
12. 계좌 조회 복구 알림이 전략 주문 재개로 오해되지 않도록 문구를 수정했다.

## 적용

프로그램을 종료한 뒤 ZIP의 history_account_recovery.patch를 프로젝트 루트에 넣고 실행한다.

```powershell
git apply --check --ignore-space-change .\history_account_recovery.patch
git apply --ignore-space-change .\history_account_recovery.patch
py -3.9-32 -m unittest discover -s tests -p "test_history_account_recovery.py" -v
```

첫 번째 명령이 실패하면 다음 명령을 실행하지 않는다. 기존 텔레그램 수신 패치 재적용은 필요 없다.
ZIP files/는 검토용 수정 파일이다. 전체 파일로 덮어쓰면 다른 로컬 변경을 잃을 수 있어 패치 적용을 우선한다.

## 일봉 보충 실행 — 64bit marketdata 환경

```powershell
conda run -n marketdata python bootstrap_market_history.py --lookback-days 400 --min-history-rows 180
```

수집기 시작 로그의 [수집기 Python]이 64bit인지, [공유 DB] 경로가 매매 프로젝트의 market_history.db인지 확인한다.
Python39-32로 수집기를 실행하지 않는다. 전체시장 수집은 종목별 요청을 보내므로 시간이 걸린다.

```text
과거 이력 보충 (157/180봉)
```

수집 후 확인:

```powershell
py -3.9-32 -c "import sqlite3; from datetime import datetime; c=sqlite3.connect('file:market_history.db?mode=ro',uri=True); print('종목수/최소봉수/최대봉수/180봉이상:', c.execute('SELECT COUNT(*), MIN(n), MAX(n), SUM(n>=180) FROM (SELECT code, COUNT(*) n FROM daily_price WHERE date < ? GROUP BY code)', (datetime.now().strftime('%Y%m%d'),)).fetchone())"
```

완료 후 main.py를 다시 시작하면 새 DB로 전략 기준값을 준비한다. 이 패치는 수집 완료 후 자동 전략 재초기화를 추가하지 않는다.
400달력일 안에 180거래일이 없는 신규 상장/데이터 누락 종목은 여전히 부족할 수 있다. 이 경우 실제 데이터와 조회 범위를 확인한다.

## 계좌 동기화 확인

전략 초기화 실패 중에도 다음 메시지가 나타난다.

```text
[StrategyManager] 전략 초기화 대기 - 계좌 동기화/수동매도 DB 정리 유지
```

장중 기본 60초 간격으로 미체결/잔고를 조회하며, 장외 타이머는 기존 5분 정책이다.
초기 정상 조회 1회 및 이후 정상 조회 1회에서 잔고 0이 확인되면 전량매도 포지션 삭제가 가능하다.
미체결 매도가 남아 있거나 잔고 조회가 실패하면 삭제 확인을 진행하지 않는다.
삭제 대상은 position_strategy 기록이며 거래/체결 이력 전체를 삭제하지 않는다.

## 검증과 한계

- 기존 텔레그램 패치가 포함된 코드에서 기존/신규 테스트 총 108개 통과.
- 신규 테스트 11개: 최신 157봉 보충, 중복 방지, 최근 증분, 신규 상장 부족, 빈 응답 보존,
  당일 봉 제외, 목표 설정, 2회 잔고 확인 삭제, 잔고 실패 보류, 보유 재출현, 부분매도 수량 보정 및 장외 동기화.
- GitHub main 및 기존 Telegram 수신 패치 적용본에 대한 패치 검사/실제 적용/코드 비교를 수행했다.
- LF 및 CRLF 줄바꿈 적용 확인. 변경 Python 파일의 Python 3.9 문법 확인.
- 테스트는 임시 DB, 가상 데이터, Qt/증권사 모의 객체를 사용했다. 실제 pykrx 수집이나 Kiwoom 주문은 실행하지 않았다.
- value_quality.db의 재무 데이터 0건은 별도 입력이 필요하다. 일봉 보충으로 재무 데이터는 채워지지 않는다.

기준 소스: 2026-10-13 GitHub main 8a1cae81591440a195bac73f3daa98180cb8d5de 및 앞서 제공한 Telegram 수신 수정본.
