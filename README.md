# SystemTrading 작업 환경 세팅 가이드

> 대상: 저장소를 처음 받은 PC에서 **과거 데이터 수집 → Kiwoom 자동매매 → Telegram → 선택적으로 웹 API**까지 실행 가능한 상태를 만드는 절차입니다.
>
> 핵심은 Python 환경이 하나가 아니라는 점입니다.
>
> - **32bit Python 3.9**: Kiwoom OpenAPI+ 자동매매 실행
> - **64bit Python**: pykrx로 과거 일봉/종목 마스터 구축
>
> 두 환경은 같은 프로젝트 폴더의 `market_history.db`를 공유합니다.

---

## 0. 처음 설치할 때 전체 순서

새 PC에서는 아래 순서대로 진행하는 것을 권장합니다.

```text
1. Git 설치
2. 저장소 clone
3. Kiwoom OpenAPI+ 설치 + 서비스 신청/로그인 확인
4. Python 3.9 32bit 설치
5. Kiwoom용 32bit 가상환경 생성 + 패키지 설치
6. 64bit Python/Conda 환경 marketdata 생성 + pykrx 설치
7. Telegram 환경변수 설정
8. 64bit 환경에서 market_history.db 초기 구축
9. ValueQuality를 사용할 경우 재무 CSV 입력
10. DB/환경 사전 점검
11. 32bit 환경에서 main.py 실행
12. 선택: 웹 모니터링 API 실행
```

**중요:** 새 PC에서 `main.py`부터 실행하면 안 됩니다. `HighBreakoutStrategy`와 `PullbackTrendStrategy`는 `market_history.db`의 종목 마스터와 최소 과거 일봉이 준비되어 있어야 정상 초기화됩니다.

---

## 1. 새 PC에서 프로젝트 받기

ZIP 다운로드보다 `git clone`을 권장합니다.

```cmd
cd C:\Users\사용자명\Downloads
git clone https://github.com/leesoochan85/SystemTrading.git
cd SystemTrading
git status
```

정상 예시:

```text
On branch main
Your branch is up to date with 'origin/main'.
```

Git 확인:

```cmd
git --version
where git
```

Git이 인식되지 않으면 Git for Windows를 설치한 뒤 CMD를 새로 엽니다.

### 권장: clone 직후 현재 브랜치/최신 상태 확인

```cmd
git branch --show-current
git fetch origin
git status
```

로컬 수정이 없는 새 PC라면 필요 시:

```cmd
git pull origin main
```

---

## 2. 프로젝트에서 사용하는 두 Python 환경

### 2-1. Kiwoom 자동매매용

```text
Windows
Python 3.9.x 32-bit
PyQt5 QAxContainer
Kiwoom OpenAPI+
```

Kiwoom OpenAPI+가 ActiveX 기반이므로 자동매매 프로세스는 **32bit Python**을 사용합니다.

설치 확인:

```cmd
py -0p
```

목록에 Python 3.9 32-bit가 보여야 합니다.

### 2-2. 과거 데이터 수집용

```text
64-bit Python
pykrx
pandas
numpy
python-dotenv
```

`bootstrap_market_history.py`는 코드에서 64bit가 아니면 실행을 중단하도록 되어 있습니다. 따라서 Kiwoom용 32bit 환경과 별도로 만들어야 합니다.

---

## 3. Kiwoom용 32bit 가상환경 생성

프로젝트 폴더에서 실행합니다.

```cmd
cd C:\Users\사용자명\Downloads\SystemTrading
py -3.9-32 -m venv kiwoom
kiwoom\Scripts\activate
```

정상 활성화 예시:

```text
(kiwoom) C:\Users\사용자명\Downloads\SystemTrading>
```

버전/비트 확인:

```cmd
python --version
python -c "import struct; print(struct.calcsize('P') * 8)"
```

정상 목표:

```text
Python 3.9.x
32
```

---

## 4. 32bit 자동매매 패키지 설치

32bit Python에서는 일부 최신 패키지 wheel이 없을 수 있으므로 기존 검증 버전을 우선 사용합니다.

```cmd
python -m pip install --upgrade pip wheel
pip install "setuptools==80.10.2" --force-reinstall
pip install numpy==1.24.4 pandas==1.5.3 matplotlib==3.7.5 --prefer-binary
pip install requests beautifulsoup4 lxml PyQt5 openpyxl xlrd deprecated multipledispatch --prefer-binary
```

> **변경:** `pykrx`는 이 32bit 환경의 필수 패키지에서 제외합니다. 현재 구조에서는 pykrx 조회를 64bit `marketdata` 환경이 담당하고, Kiwoom 프로세스는 `market_history.db`를 읽습니다.

설치 확인:

```cmd
python -c "import numpy, pandas, requests, bs4, lxml, openpyxl; print('basic packages OK')"
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
```

`QAx OK`가 나오면 PyQt5 ActiveX 모듈이 정상적으로 import된 것입니다.

---

## 5. 64bit `marketdata` 환경 생성

Conda를 사용하는 경우 예시입니다.

```cmd
conda create -n marketdata python=3.12 -y
conda activate marketdata
```

64bit 확인:

```cmd
python -c "import struct, platform; print(platform.python_version(), struct.calcsize('P') * 8)"
```

정상 목표:

```text
... 64
```

필수 패키지:

```cmd
python -m pip install --upgrade pip
pip install pykrx pandas numpy python-dotenv
```

설치 확인:

```cmd
python -c "from pykrx import stock; import pandas, numpy, dotenv; print('marketdata packages OK')"
```

> 이 환경에서는 `PyQt5.QAxContainer`가 필요하지 않습니다. 반대로 Kiwoom용 32bit 환경에서 과거 전체시장 수집을 수행하지 않습니다.

---

## 6. Kiwoom OpenAPI+ 설치 및 버전처리

새 PC에는 Kiwoom OpenAPI+를 별도로 설치해야 합니다.

설치 확인 예시:

```cmd
dir C:\OpenAPI
```

일반적으로 아래 구성요소가 존재합니다.

```text
KOAStudioSA.exe
opstarter.exe
opversionup.exe
khopenapi.ocx
```

### 최초 PC 세팅 시 확인

```text
1. Kiwoom 계정 준비
2. OpenAPI+ 서비스 사용 신청 확인
3. 필요한 인증서/보안 프로그램 설치
4. OpenAPI+ 로그인 확인
5. 버전처리 완료
6. 이후 자동매매 실행
```

### 버전처리 순서

`main.py`를 먼저 실행하지 말고 OpenAPI 버전처리를 먼저 완료합니다.

```text
1. PC 재부팅
2. main.py 실행 금지
3. C:\OpenAPI\opstarter.exe 또는 KOAStudioSA.exe 실행
4. OpenAPI 접속/로그인
5. 버전처리 완료
6. KOA Studio 종료
7. 그 다음 main.py 실행
```

직접 실행:

```cmd
C:\OpenAPI\opstarter.exe
```

또는:

```cmd
C:\OpenAPI\KOAStudioSA.exe
```

“OpenAPI를 사용하는 프로그램을 모두 종료하세요”가 나오면 관련 프로세스를 확인합니다.

```cmd
tasklist | findstr /i "python koa opstarter opversion nkmini khopen"
```

필요 시 종료:

```cmd
taskkill /F /IM python.exe
taskkill /F /IM pythonw.exe
taskkill /F /IM KOAStudioSA.exe
taskkill /F /IM opstarter.exe
taskkill /F /IM opversionup.exe
taskkill /F /IM nkmini.exe
```

---

## 7. OpenAPI 등록 사용자 오류

새 PC에서 아래 메시지가 나올 수 있습니다.

```text
등록된 사용자가 아닙니다.
```

확인 항목:

```text
1. 기존 PC에서 쓰던 키움 ID와 같은 ID인지 확인
2. 키움 OpenAPI+ 서비스 사용 신청 여부 확인
3. 모의투자/실전투자 로그인 구분 확인
4. 새 PC의 인증서/보안 프로그램 설정 확인
5. OpenAPI 버전처리가 끝났는지 확인
```

---

## 8. Telegram 환경변수 설정

현재 `util/const.py`는 아래 환경변수를 읽습니다.

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

CMD에서 영구 환경변수로 설정:

```cmd
setx TELEGRAM_BOT_TOKEN "봇_토큰"
setx TELEGRAM_CHAT_ID "채팅_ID"
```

`setx` 실행 후에는 **기존 CMD를 닫고 새 CMD를 열어야** 적용됩니다.

확인:

```cmd
echo %TELEGRAM_BOT_TOKEN%
echo %TELEGRAM_CHAT_ID%
```

Python에서 값 존재 여부만 확인하려면 토큰 전체를 출력하기보다 다음처럼 확인하는 것을 권장합니다.

```cmd
python -c "import os; print(bool(os.getenv('TELEGRAM_BOT_TOKEN')), bool(os.getenv('TELEGRAM_CHAT_ID')))"
```

전송 테스트:

```cmd
python -c "from util.notifier import send_message; send_message('SystemTrading Telegram 테스트')"
```

오류 예시:

```text
401 Unauthorized: 봇 토큰 오류 가능성
404 Not Found: 잘못된 Bot API URL/토큰 형식 가능성
400 Bad Request: chat_id 또는 요청값 확인 필요
```

봇 토큰이 외부에 노출되면 BotFather에서 즉시 재발급합니다.

---

## 9. 최초 `market_history.db` 구축 — 필수

신고가/눌림목 전략은 `market_history.db`의 다음 데이터를 사용합니다.

```text
stock_master : KOSPI/KOSDAQ 기업주식 목록
daily_price  : 과거 OHLCV 일봉
```

새 clone에 충분한 DB가 없다면 **64bit `marketdata` 환경에서 먼저 구축**합니다.

```cmd
cd C:\Users\사용자명\Downloads\SystemTrading
conda activate marketdata
python bootstrap_market_history.py
```

스크립트 자체가 64bit 여부를 검사합니다.

기본 동작:

```text
- KOSPI + KOSDAQ 기업주식 마스터 갱신
- SPAC/리츠 제외
- 최소 전략 계산용 과거 일봉 수집
- 기존 DB가 있으면 누락 구간 위주 증분 보충
- market_history.db에 저장
```

필요 시 옵션 확인:

```cmd
python bootstrap_market_history.py --help
```

예:

```cmd
python bootstrap_market_history.py --lookback-days 180 --overlap-days 10 --sleep 1.0
```

### 정상 여부 확인

실행 로그에서 최소한 아래 내용을 확인합니다.

```text
[수집기 Python] ... / 64bit
[공유 DB] ...\market_history.db
[기업주식 마스터 갱신] ...종목
[초기 일봉 구축 완료] ...
```

**주의:** 전체시장 종목별 수집이므로 시간이 걸릴 수 있으며, 요청 간격을 과도하게 줄이지 않습니다.

---

## 10. ValueQuality 재무데이터 준비 — 전략 사용 시 필수

`ValueQualityStrategy`는 가격만으로 후보를 만들지 않습니다. `value_quality.db`의 재무 스냅샷이 필요합니다.

입력 CSV 컬럼:

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

예시 경로:

```text
data/value_fundamentals.csv
```

32bit/64bit 어느 쪽에서도 SQLite 입력 자체는 가능하지만, 보통 프로젝트의 일반 Python 환경에서 실행합니다.

```cmd
python bootstrap_value_quality_data.py data\value_fundamentals.csv
```

정상 예시:

```text
[ValueQuality] fundamental N건 저장 완료
```

재무데이터를 넣지 않으면 실행 자체가 완전히 실패하는 것이 아니라 **ValueQuality 후보가 0종목**으로 나올 수 있습니다.

현재 필터:

```text
0 < PER(TTM) <= 15
0 < PBR <= 1
30 <= 매출총이익률 <= 95
1 <= 총자산회전율 <= 10
```

---

## 11. DB 파일에 대한 이해

새 PC에서는 모든 DB가 처음부터 존재할 필요는 없습니다. 코드가 일부 테이블/DB를 자동 생성합니다.

### 초기 실행 전에 사실상 필요한 핵심 데이터

```text
market_history.db
  - stock_master
  - daily_price

value_quality.db
  - ValueQuality를 실제 운용할 경우 fundamental snapshot 필요
```

### 실행 중 생성/갱신되는 주요 DB

```text
strategy_position.db
monitoring.db
value_quality.db
market_history.db
```

`universe_price.db`는 현재 `db_helper.py`의 레거시 호환 코드가 남아 있으므로 임의 삭제 전에 코드 참조를 확인합니다.

SQLite가 WAL 모드로 동작할 때 아래 보조 파일이 일시적으로 생길 수 있습니다.

```text
*.db-wal
*.db-shm
```

프로그램이 실행 중인 상태에서 임의 삭제하지 않습니다.

---

## 12. 자동매매 실행 전 사전 점검

32bit 환경으로 돌아옵니다.

```cmd
cd C:\Users\사용자명\Downloads\SystemTrading
kiwoom\Scripts\activate
```

확인:

```cmd
python -c "import struct; print('bitness=', struct.calcsize('P') * 8)"
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
python -c "import sqlite3; c=sqlite3.connect('market_history.db'); print(c.execute('select count(*) from stock_master where active=1').fetchone()); print(c.execute('select count(*) from daily_price').fetchone()); c.close()"
python -c "import os; print('telegram=', bool(os.getenv('TELEGRAM_BOT_TOKEN')), bool(os.getenv('TELEGRAM_CHAT_ID')))"
```

목표:

```text
bitness= 32
QAx OK
stock_master 활성 종목 수 > 0
daily_price 행 수 > 0
telegram= True True   # Telegram을 사용할 경우
```

---

## 13. `main.py` 실행

Kiwoom용 **32bit 가상환경**에서 실행합니다.

```cmd
python main.py
```

현재 `main.py`는 다음 3개 전략을 생성해 `StrategyManager`에 전달합니다.

```text
HighBreakoutStrategy
PullbackTrendStrategy
ValueQualityStrategy
```

처음에는 반드시 **모의투자 계좌 또는 주문 위험이 없는 환경에서 초기화 로그부터 검증**하는 것을 권장합니다.

### 정상 초기화 시 확인할 핵심 로그

```text
로그인 성공
HighBreakout 객체 생성 완료
PullbackTrend 객체 생성 완료
ValueQuality 객체 생성 완료
[StrategyManager] start 호출
[Kiwoom] 미체결 주문 조회 정상 완료: ...건
[Kiwoom] 잔고 조회 정상 완료: ...
[Kiwoom] 자금 조회 정상 완료: ...
[HighBreakout] 신고가 기준값 준비: ...
[PullbackTrend] 기준값 준비: ...
[ValueQuality] 저평가 우량주 후보 준비: ...
[StrategyManager] ... 실시간 등록 ...
[StrategyManager] 타이머 시작
[StrategyManager] Telegram /status 명령대기
```

초기화 중 계좌/예수금/미체결 조회가 실패했다면 주문을 맡기기 전에 원인을 먼저 해결합니다.

---

## 14. 장중 확인 항목

현재 공통 주식체결 FID는 최소화되어 있습니다.

```text
현재가
시가
누적거래량
(최우선)매수호가
```

장중에는 다음을 확인합니다.

- 실시간 이벤트가 실제로 들어오는지
- 신고가/눌림목/ValueQuality 신호가 지연 없이 처리되는지
- 매수 주문 전 종목당 총자산 10% 제한이 적용되는지
- 보유 + 매수 미체결 + 예약 종목이 계좌 전체 최대 10개를 넘지 않는지
- 주문/체결 후 `strategy_position.db`, `monitoring.db`가 갱신되는지
- Telegram 주문/체결 알림 및 `/status`가 동작하는지
- CPU/메모리와 실시간 틱 처리 지연이 과도하지 않은지

---

## 15. 웹 모니터링 API 실행 — 선택 기능

웹 API는 자동매매 프로세스와 분리되어 SQLite DB를 **조회 전용**으로 읽습니다.

필요 패키지 예:

```cmd
pip install fastapi uvicorn
```

저장소에 `requirements-web.txt`가 있다면 개별 설치보다 다음을 우선 사용합니다.

```cmd
pip install -r requirements-web.txt
```

프로젝트 루트에서:

```cmd
python run_web_api.py
```

기본 주소:

```text
http://127.0.0.1:8000
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/health
```

FastAPI는 기본적으로 프로젝트 루트의 `monitoring.db`, `strategy_position.db`를 읽습니다.

프로젝트 외부 위치에서 실행하는 특수한 경우에는 `SYSTEMTRADING_ROOT` 환경변수로 루트를 지정할 수 있습니다.

```cmd
set SYSTEMTRADING_ROOT=C:\경로\SystemTrading
```

---

## 16. 자주 발생하는 오류

### Git 명령어 인식 안 됨

```text
'git'은(는) 내부 또는 외부 명령...
```

해결:

```text
Git for Windows 설치 후 CMD 새로 열기
```

### libcurl-4.dll 오류

```text
fatal: failed to load library 'libcurl-4.dll'
```

해결:

```text
Git 재설치 또는 Git Bash에서 clone 시도
```

### `ModuleNotFoundError: openpyxl`

```cmd
pip install openpyxl
```

### `ModuleNotFoundError: pkg_resources`

```cmd
pip install "setuptools==80.10.2" --force-reinstall
```

### `QAxContainer` import 실패

먼저 현재 Python 비트를 확인합니다.

```cmd
python -c "import struct; print(struct.calcsize('P') * 8)"
```

자동매매 환경이라면 `32`가 나와야 합니다.

```cmd
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
```

### `stock_master가 비어 있습니다`

원인:

```text
market_history.db 초기 구축을 하지 않았거나 잘못된 DB를 보고 있음
```

해결:

```cmd
conda activate marketdata
python bootstrap_market_history.py
```

### `신고가/눌림목 계산 가능한 종목이 없습니다`

원인:

```text
과거 일봉이 전략 최소 요구 거래일 수보다 부족함
```

해결:

```text
bootstrap_market_history.py를 다시 실행해 누락 데이터를 보충
필요 시 --lookback-days를 늘림
```

### ValueQuality 후보가 0종목

확인:

```text
1. value_quality.db에 fundamental snapshot이 들어갔는지
2. CSV 컬럼명이 정확한지
3. PER/PBR/매출총이익률/총자산회전율 필터를 만족하는 데이터가 있는지
```

---

## 17. 운영 전 주의사항

1. `main.py` 실행 전 Kiwoom OpenAPI 버전처리를 완료합니다.
2. 자동매매는 **Python 3.9 32bit**, 과거 전체시장 수집은 **64bit marketdata**로 분리합니다.
3. 처음 받은 PC에서는 `market_history.db`를 먼저 구축합니다.
4. ValueQuality를 실제 사용하려면 `value_quality.db`에 재무 스냅샷을 입력합니다.
5. Telegram 토큰은 소스코드/GitHub에 저장하지 않습니다.
6. `.db-wal`, `.db-shm`은 실행 중인 SQLite가 사용하는 파일일 수 있으므로 임의 삭제하지 않습니다.
7. 처음 실전 계좌에서 바로 검증하지 말고 초기화/조회/주문 흐름을 모의투자에서 먼저 확인합니다.
8. `util/time_helper.py`의 휴장일/특별 개장시간 목록은 연도가 바뀌거나 KRX 일정이 변경되면 반드시 갱신합니다.
9. 네이버/한경처럼 비공식 또는 웹 페이지 기반 데이터 소스는 응답 구조 변경으로 고장날 수 있으므로 오류 로그를 확인합니다.
10. 장중 실시간 최적화가 검증될 때까지 `Kiwoom.before_fid_opt_*` 같은 백업 파일은 보존하는 편이 안전합니다.

---

## 18. 새 PC 설치 완료 체크리스트

- [ ] Git clone 완료
- [ ] `git status` 정상
- [ ] Kiwoom OpenAPI+ 설치
- [ ] OpenAPI+ 서비스 신청/로그인 확인
- [ ] OpenAPI 버전처리 완료
- [ ] Python 3.9 32bit 설치
- [ ] `kiwoom` 32bit 가상환경 생성
- [ ] `QAx OK` 확인
- [ ] 64bit `marketdata` 환경 생성
- [ ] `pykrx` import 확인
- [ ] Telegram 환경변수 설정
- [ ] `bootstrap_market_history.py` 성공
- [ ] `stock_master` 데이터 존재
- [ ] `daily_price` 데이터 존재
- [ ] ValueQuality 사용 시 fundamental CSV 적재
- [ ] `main.py` 로그인/초기화 완료
- [ ] 미체결/잔고/예수금 정상 조회
- [ ] 실시간 FID 등록 확인
- [ ] Telegram `/status` 확인
- [ ] 선택 시 FastAPI `/health`, `/docs` 확인

