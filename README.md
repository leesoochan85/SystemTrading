# SystemTrading 작업 환경 세팅 가이드

> 대상: 저장소를 처음 받은 PC에서 **과거 데이터 수집 → Kiwoom 자동매매 → Telegram → FastAPI/React 웹 모니터링**까지 실행 가능한 상태를 만드는 절차입니다.
>
> 핵심은 실행 환경을 분리하는 것입니다.
>
> - **Python 3.9 32bit**: Kiwoom OpenAPI+ 자동매매
> - **64bit Python/Conda**: pykrx 과거 일봉/종목 마스터 수집
> - **별도 webenv**: FastAPI 웹 API
> - **Node.js**: React/Vite 대시보드
>
> 자동매매 프로세스와 웹 프로세스는 직접 연결하지 않고 SQLite DB를 경계로 분리합니다.

---

## 0. 처음 설치할 때 전체 순서

```text
1. Git 설치
2. 저장소 clone
3. Kiwoom OpenAPI+ 설치 + 서비스 신청/로그인 확인
4. Python 3.9 32bit 설치
5. Kiwoom용 32bit 가상환경 생성 + 패키지 설치
6. 64bit Python/Conda marketdata 환경 생성 + pykrx 설치
7. Telegram 환경변수 설정
8. market_history.db 초기 구축
9. ValueQuality 사용 시 재무 CSV 입력
10. DB/환경 사전 점검
11. 32bit 환경에서 main.py 실행
12. webenv에서 FastAPI 실행
13. Node.js 환경에서 React 대시보드 실행
```

**중요:** 새 PC에서 `main.py`부터 실행하면 안 됩니다.  
`HighBreakoutStrategy`와 `PullbackTrendStrategy`는 `market_history.db`의 종목 마스터와 과거 일봉이 준비되어 있어야 정상 초기화됩니다.

---

## 1. 새 PC에서 프로젝트 받기

```cmd
cd C:\Users\사용자명\Downloads
git clone https://github.com/leesoochan85/SystemTrading.git
cd SystemTrading
git status
```

최신 상태 확인:

```cmd
git branch --show-current
git fetch origin
git status
```

로컬 수정이 없다면 필요 시:

```cmd
git pull origin main
```

---

## 2. 실행 환경

### 2-1. Kiwoom 자동매매용

```text
Windows
Python 3.9.x 32-bit
PyQt5 QAxContainer
Kiwoom OpenAPI+
```

확인:

```cmd
py -0p
python -c "import struct; print(struct.calcsize('P') * 8)"
```

자동매매 환경에서는 `32`가 나와야 합니다.

### 2-2. 과거 데이터 수집용

```text
64-bit Python
pykrx
pandas
numpy
python-dotenv
```

`bootstrap_market_history.py`는 64bit 환경에서 실행합니다.

### 2-3. 웹 API용

FastAPI는 Kiwoom ActiveX를 사용하지 않고 SQLite DB만 읽으므로 자동매매용 32bit Python과 분리합니다.

예:

```cmd
py -m venv webenv
webenv\Scripts\activate
pip install -r requirements-web.txt
```

### 2-4. React 대시보드용

Node.js 설치 후 확인:

```cmd
node -v
npm -v
```

---

## 3. Kiwoom용 32bit 가상환경 생성

```cmd
cd C:\Users\사용자명\Downloads\SystemTrading
py -3.9-32 -m venv kiwoom
kiwoom\Scripts\activate
```

확인:

```cmd
python --version
python -c "import struct; print(struct.calcsize('P') * 8)"
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
```

---

## 4. 32bit 자동매매 패키지 설치

```cmd
python -m pip install --upgrade pip wheel
pip install "setuptools==80.10.2" --force-reinstall
pip install numpy==1.24.4 pandas==1.5.3 matplotlib==3.7.5 --prefer-binary
pip install requests beautifulsoup4 lxml PyQt5 openpyxl xlrd deprecated multipledispatch --prefer-binary
```

현재 구조에서는 `pykrx`를 Kiwoom 32bit 환경의 필수 패키지로 사용하지 않습니다.

---

## 5. 64bit marketdata 환경

Conda 예시:

```cmd
conda create -n marketdata python=3.12 -y
conda activate marketdata
pip install pykrx pandas numpy python-dotenv
```

확인:

```cmd
python -c "import struct, platform; print(platform.python_version(), struct.calcsize('P') * 8)"
```

`64`가 나와야 합니다.

---

## 6. Kiwoom OpenAPI+ 설치/버전처리

새 PC에서는 Kiwoom OpenAPI+ 설치와 서비스 신청이 필요합니다.

일반 설치 경로:

```text
C:\OpenAPI
```

주요 파일 예:

```text
KOAStudioSA.exe
opstarter.exe
opversionup.exe
khopenapi.ocx
```

버전처리 후 자동매매를 실행합니다.

---

## 7. Telegram 환경변수

현재 `util/const.py`는 다음 환경변수를 사용합니다.

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

설정:

```cmd
setx TELEGRAM_BOT_TOKEN "봇_토큰"
setx TELEGRAM_CHAT_ID "채팅_ID"
```

새 CMD를 열고 확인:

```cmd
python -c "import os; print(bool(os.getenv('TELEGRAM_BOT_TOKEN')), bool(os.getenv('TELEGRAM_CHAT_ID')))"
```

---

## 8. market_history.db 초기 구축

64bit `marketdata` 환경에서:

```cmd
conda activate marketdata
python bootstrap_market_history.py
```

주요 데이터:

```text
stock_master
daily_price
```

현재 신고가/눌림목 전략은 장중에 전체 일봉을 반복 조회하지 않고, 시작 전에 `market_history.db`에서 기준값을 계산한 뒤 Kiwoom 실시간 체결 이벤트로 판단합니다.

---

## 9. ValueQuality 재무 데이터

필요 CSV:

```text
code
code_name
as_of_date
per_ttm
pbr
gross_margin_pct
asset_turnover
latest_annual_year
source_note
```

입력:

```cmd
python bootstrap_value_quality_data.py data\value_fundamentals.csv
```

현재 필터:

```text
0 < PER(TTM) <= 15
0 < PBR <= 1
30 <= 매출총이익률 <= 95
1 <= 총자산회전율 <= 10
```

재무데이터가 없거나 조건 충족 종목이 없으면:

```text
[ValueQuality] 저평가 우량주 후보 준비: 0종목
```

이 출력될 수 있습니다.

---

## 10. 현재 운영 전략

```text
HighBreakoutStrategy
PullbackTrendStrategy
ValueQualityStrategy
```

실제 매수 수량/현금/계좌 전체 포지션 제한은 `StrategyManager`가 중앙 관리합니다.

현재 주요 실제 주문 정책:

```text
전략별 별도 예산 없음
모든 전략 공용 계좌 현금 사용
신규 종목 1개당 총자산 최대 10%
계좌 전체 최대 10포지션
```

---

## 11. 주요 DB

```text
market_history.db
  - 전체 종목 마스터/과거 OHLCV

strategy_position.db
  - 실제 보유종목과 전략 매핑

monitoring.db
  - 주문/체결/성과
  - 웹 조회용 스냅샷
  - 가상매매 virtual_position / virtual_trade

value_quality.db
  - ValueQuality 재무 스냅샷
  - 보유 상태
  - 한경 리포트 상태
```

SQLite WAL 사용 중에는 다음 파일이 생성될 수 있습니다.

```text
*.db-wal
*.db-shm
```

프로그램 실행 중 임의 삭제하지 않습니다.

---

## 12. 자동매매 실행 전 점검

```cmd
kiwoom\Scripts\activate

python -c "import struct; print('bitness=', struct.calcsize('P') * 8)"
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
python -c "import sqlite3; c=sqlite3.connect('market_history.db'); print(c.execute('select count(*) from stock_master where active=1').fetchone()); print(c.execute('select count(*) from daily_price').fetchone()); c.close()"
```

목표:

```text
bitness= 32
QAx OK
stock_master 활성 종목 > 0
daily_price 행 수 > 0
```

---

## 13. main.py 실행

```cmd
C:\Users\사용자명\AppData\Local\Programs\Python\Python39-32\python.exe main.py
```

정상 초기화 로그 예:

```text
로그인 성공
HighBreakout 객체 생성 완료
PullbackTrend 객체 생성 완료
ValueQuality 객체 생성 완료

[VirtualStrategyEngine] 가상 포지션 캐시 복원: N건

[StrategyManager] start 호출
[Kiwoom] 미체결 주문 조회 정상 완료: ...건
[Kiwoom] 잔고 조회 정상 완료: ...종목
[Kiwoom] 자금 조회 정상 완료: ...
[HighBreakout] 신고가 기준값 준비: ...
[PullbackTrend] 기준값 준비: ...
[ValueQuality] 저평가 우량주 후보 준비: ...
[StrategyManager] event-driven 공통 실시간 등록: ...종목 / 3전략 공유
[StrategyManager] 타이머 시작
```

`가상 포지션 캐시 복원`은 `monitoring.db`에 남아 있는 열린 가상 포지션을 메모리로 복구했다는 의미입니다.

---

## 14. 웹 모니터링 구조

### 핵심 원칙

**React와 FastAPI는 Kiwoom 객체를 직접 접근하지 않습니다.**

```text
Kiwoom OpenAPI
      │
      │ 실시간 체결
      ▼
StrategyManager / VirtualStrategyEngine
      │
      │ SQLite 저장
      ▼
monitoring.db
      ▲
      │ READ ONLY
      │
FastAPI
      ▲
      │ HTTP
      │
React
```

FastAPI의 DB 연결은 조회 전용으로 사용합니다.

따라서 웹 서버나 브라우저 오류가 Kiwoom ActiveX 객체를 직접 건드리는 구조가 아닙니다.

---

## 15. 가상매매의 목적과 실제 계좌와의 차이

웹의 가상매매는 실제 주문 성공 여부를 그대로 복사하는 기능이 아닙니다.

목적:

> **실제 계좌의 현금 부족이나 최대 보유종목 제한과 무관하게 전략 신호가 전부 체결되었다고 가정했을 때 전략 자체의 성과를 검증한다.**

따라서:

```text
실제 주문
- 실제 현금 필요
- 최대 10포지션 제한
- 미체결/주문 가능 금액 등의 안전 조건 적용

가상매매
- 실제 계좌 잔고와 독립
- 전략 조건 충족 시 가상 진입
- 금액/수량 대신 비중과 수익률 중심
- 전략별 성능 검증 목적
```

PullbackTrend의 전고점 30% 부분청산도 가상매매에 반영되며 잔여 70%는 계속 관리합니다.

---

## 16. 가상 포지션 5초 DB 스냅샷

2026-08-17부터 가상 포지션의 장중 현재가를 **틱마다 SQLite에 UPDATE하지 않습니다.**

### 변경 전

```text
Kiwoom 틱
 → DB SELECT
 → 전략 판단
 → DB UPDATE
```

활발한 종목이 여러 개 열려 있으면 SQLite write가 지나치게 자주 발생할 수 있었습니다.

### 변경 후

```text
Kiwoom 틱
      │
      ├─ BUY/SELL 조건 즉시 검사
      │
      └─ 메모리 current_price 갱신
                 │
              최대 5초
                 ▼
          monitoring.db 일괄 UPDATE
```

`VirtualStrategyEngine`은 열린 가상 포지션을 메모리 캐시에 유지하고 변경된 포지션만 dirty 상태로 표시합니다.

최대 5초마다 dirty 포지션을 한 트랜잭션으로 저장합니다.

### 즉시 저장되는 데이터

다음 이벤트는 5초를 기다리지 않습니다.

```text
가상 BUY
가상 SELL
PullbackTrend 부분청산
중요한 청산 상태 변경
```

즉 **매수/매도 판단은 실시간이고, 웹 표시용 현재가 저장만 최대 5초 간격**입니다.

프로그램 재시작 시에는 `virtual_position`을 DB에서 한 번 읽어 메모리 캐시를 복원합니다.

열린 가상 포지션 종목은 신규 후보에서 빠지더라도 실시간 등록 대상에 강제 포함되어 매도 관리가 계속됩니다.

---

## 17. FastAPI 실행

프로젝트 루트에서:

```cmd
webenv\Scripts\activate
python run_web_api.py
```

기본 주소:

```text
http://127.0.0.1:8000
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/health
```

FastAPI는 `monitoring.db`, `strategy_position.db`를 읽습니다.

---

## 18. React 대시보드 실행

최초 1회:

```cmd
cd web_frontend
npm install
```

실행:

```cmd
npm run dev
```

기본 개발 주소:

```text
http://localhost:5173
```

React는 API 데이터를 약 5초 간격으로 다시 조회합니다.

따라서 장중 가상 포지션 화면은 일반적으로:

```text
Kiwoom 틱
→ 메모리 즉시 갱신
→ 최대 5초 내 monitoring.db 반영
→ React 다음 polling에서 표시
```

순서로 갱신됩니다.

프론트엔드가 Kiwoom을 직접 호출해 현재가를 가져오는 구조가 아닙니다.

---

## 19. 운영 시 권장 프로세스

### CMD 1 — Kiwoom 자동매매

```cmd
cd C:\경로\SystemTrading
C:\Users\사용자명\AppData\Local\Programs\Python\Python39-32\python.exe main.py
```

### CMD 2 — FastAPI

```cmd
cd C:\경로\SystemTrading
webenv\Scripts\activate
python run_web_api.py
```

### CMD 3 — React

```cmd
cd C:\경로\SystemTrading\web_frontend
npm run dev
```

역할:

```text
CMD 1 = 거래/실시간 데이터 생산
CMD 2 = SQLite 읽기 전용 API
CMD 3 = 사용자 화면
```

---

## 20. 2026-08-17 검증 완료 항목

### Python 문법 검사

```cmd
python -m py_compile util\virtual_trading.py util\virtual_strategy_engine.py strategy\StrategyManager.py
```

정상 통과.

### main.py 초기화 검증

실제 확인 로그:

```text
[VirtualStrategyEngine] 가상 포지션 캐시 복원: 2건
[HighBreakout] 기업주식 유니버스: 2667종목
[HighBreakout] 신고가 기준값 준비: 2576/2667종목
[PullbackTrend] 기업주식 유니버스: 2667종목
[PullbackTrend] 기준값 준비: 2546/2667종목
[StrategyManager] event-driven 공통 실시간 등록: 2578종목 / 3전략 공유
```

### 5초 스냅샷 검증

테스트 결과:

```text
5초 전 DB 미변경 : PASS
5초 후 현재가 반영: PASS
수익률 재계산     : PASS
updated_at 갱신   : PASS
```

예:

```text
메모리: 103,000 → 104,000

5초 전 DB:
103,000 / +3.00%

5초 후 DB:
104,000 / +4.00%
```

### React polling 검증용 DB 변경

테스트 가격:

```text
103,000 / +3%
105,000 / +5%
102,000 / +2%
107,000 / +7%
104,000 / +4%
```

이 테스트는 DB → FastAPI → React 자동 갱신 확인용입니다.

---

## 21. 테스트 데이터 정리

운영 전 `T90001`, `T90002`, `T90003` 같은 테스트 가상 데이터를 제거합니다.

```cmd
python test_virtual_data.py clean
```

테스트 스크립트는 운영 루트에 둘 필요가 없다면:

```text
tests/manual/
```

같은 위치로 이동하거나 삭제합니다.

권장 테스트 파일:

```text
test_virtual_data.py
test_virtual_snapshot_5s.py
test_frontend_polling.py
```

---

## 22. 장중 확인 항목

다음 거래일에는 다음을 최종 확인합니다.

- Kiwoom 실시간 틱 정상 수신
- 실시간 BUY/SELL 판단
- 가상매매도 동일 틱을 병렬 처리
- 가상 현재가가 `monitoring.db`에 최대 약 5초 간격으로 갱신
- React 화면이 새로고침 없이 갱신
- 실제 주문 제한과 가상매매가 서로 독립
- PullbackTrend 30% 부분청산 후 잔여 70% 유지
- 실제 계좌 미체결/잔고 동기화 정상
- DB lock 관련 오류가 없는지
- CPU/메모리 사용량
- ValueQuality 재무 데이터가 준비되어 있다면 후보/매도 관리 확인

---

## 23. 자주 발생하는 오류

### QAxContainer import 실패

```cmd
python -c "import struct; print(struct.calcsize('P') * 8)"
```

Kiwoom 환경은 `32`여야 합니다.

### stock_master가 비어 있음

```cmd
conda activate marketdata
python bootstrap_market_history.py
```

### ValueQuality 후보 0종목

다음 확인:

```text
value_quality.db 재무 데이터 존재 여부
CSV 입력 여부
현재 필터 충족 여부
```

### 웹 화면의 가격이 움직이지 않음

확인 순서:

```text
1. main.py 실행 여부
2. 장중인지 확인
3. virtual_position.updated_at이 변경되는지 확인
4. FastAPI /api/virtual/positions 응답 확인
5. React 개발자도구 Network에서 약 5초 polling 확인
```

### 휴장일에 가상 가격이 갱신되지 않음

정상일 수 있습니다.

`VirtualStrategyEngine`은 `check_transaction_open()`이 False이면 실시간 전략 처리를 하지 않습니다.

`util/time_helper.py`의 휴장일 목록을 확인합니다.

---

## 24. 운영 전 주의사항

1. 자동매매는 Python 3.9 32bit에서 실행합니다.
2. 과거 전체시장 수집은 64bit `marketdata`에서 실행합니다.
3. 웹은 Kiwoom을 직접 접근하지 않고 DB를 통해 분리합니다.
4. `monitoring.db`, `strategy_position.db`, `value_quality.db`, `market_history.db`를 실행 중 임의 삭제하지 않습니다.
5. 테스트용 `T9...` 가상 포지션은 운영 전에 제거합니다.
6. Telegram token을 Git에 저장하지 않습니다.
7. 실전 계좌 전환 전 모의투자에서 충분히 검증합니다.
8. `time_helper.py`의 휴장일 목록을 매년 갱신합니다.
9. ValueQuality를 실제 운용하려면 재무 스냅샷을 주기적으로 갱신해야 합니다.
10. 웹 화면의 최대 수 초 지연은 의도된 DB 스냅샷/polling 구조입니다.

---

## 25. 설치/운영 체크리스트

- [ ] Git clone / pull 완료
- [ ] Kiwoom OpenAPI 설치/버전처리 완료
- [ ] Python 3.9 32bit 확인
- [ ] QAxContainer import 확인
- [ ] marketdata 64bit 확인
- [ ] market_history.db 구축
- [ ] ValueQuality 사용 시 재무 데이터 적재
- [ ] Telegram 환경변수 설정
- [ ] main.py 초기화 성공
- [ ] `VirtualStrategyEngine` 캐시 복원 로그 확인
- [ ] 공통 실시간 등록 성공
- [ ] webenv 설치
- [ ] FastAPI `/health` 확인
- [ ] Node.js/npm 설치
- [ ] React `npm run dev` 확인
- [ ] 테스트 데이터 제거
- [ ] 다음 거래일 장중 실시간 화면 갱신 최종 확인
