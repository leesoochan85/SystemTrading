# SystemTrading — 우량주·모멘텀 자동매매와 A/B/C 비교

| 구분 | 현재 역할 | 증권사 주문 |
|---|---|---|
| 우량주 `ValueQualityStrategy` | 재무 필터에 따른 매수와 기존 매도 규칙 | 기존 경로 유지 |
| 모멘텀 `MomentumContractionStrategy` | 수축 돌파 매수, 당일 저가 손절, 부분청산·MA 청산 | 실제 주문 어댑터 추가; 신규매수는 환경변수로 활성화 |
| A/B/C | 전략 간 성과 비교용 가상 계좌 | 주문 없음 |
| 기존 신고가·눌림목 | 실행 전략에서 제거; 과거 기록 보존 | 신규 주문 및 기존 보유 자동 관리 중단 |

실제 주문 계좌가 실계좌인지 모의계좌인지는 Kiwoom 로그인과 계좌 선택에 따릅니다. A/B/C 성과는 실제 계좌 체결·수익을 뜻하지 않습니다.
기존 신고가·눌림목 보유와 미체결은 패치가 매도하거나 취소하지 않으므로 별도로 관리해야 합니다.

- [이번 작업 기록](docs/2026-10-09_README.md)
- [전략 정리 패치 안내](docs/2026-10-08_strategy_cleanup.md)
- [모멘텀 실제 주문 추가 안내](docs/2026-10-08_momentum_live.md)
- [이전 돌파 구현 기록](docs/2026-10-08_breakout_implementation.md)

> 대상: 저장소를 처음 받은 PC에서 **과거 데이터 수집 → Kiwoom 자동매매 → Telegram → FastAPI/React 웹 모니터링**까지 실행 가능한 상태를 만드는 절차입니다.
>
> 핵심은 실행 환경을 분리하는 것입니다.
>
> - **Python 3.9 32bit**: Kiwoom OpenAPI+ 자동매매
> - **Python 3.12 64bit / Conda marketdata**: pykrx 일봉·종목 마스터 및 KRX/OpenDART 재무 수집
> - **별도 webenv**: FastAPI 웹 API
> - **Node.js**: React/Vite 대시보드
>
> 자동매매 프로세스와 웹 프로세스는 직접 연결하지 않고 SQLite DB를 경계로 분리합니다.

---

## 매일 실행하는 순서 

### 1. 데이터 갱신 후 결과 확인

매매 프로그램을 종료한 상태에서 프로젝트 루트의 **CMD**에서 실행합니다.

```cmd
update_market_data.bat
```

기존 `%USERPROFILE%\anaconda3\envs\marketdata\python.exe`로 일봉 수집 → 전체 대상 재무 수집 → 재무 DB 상태 출력을 순서대로 실행합니다. 터미널에서 conda를 활성화할 필요는 없습니다. Anaconda 설치 경로가 다르면 배치 파일의 `DATA_PYTHON`을 수정합니다.

일봉만 갱신하려면:

```cmd
update_market_data.bat history
```

일봉은 충분한 이력이 있으면 증분으로 보충합니다. 재무제표와 별개로 주가에 따라 PER/PBR이 바뀌므로 매일 최신 지표를 사용하려면 재무 스냅샷도 갱신합니다. 현재 재무 스냅샷의 시가총액 기준일 허용 기간은 7일입니다. `history` 옵션으로 재무 갱신을 무기한 생략할 수는 없습니다.

완료 후 일봉 `eligible_code_count`, `failed_codes`, `insufficient_history_count`와 재무 `saved`, `fresh`, `qualified`를 확인합니다. 일부 종목의 일봉 조회 실패는 수집 프로세스 종료 코드에 반영되지 않을 수 있으므로 배치 완료만으로 전체 수집 성공을 판단하지 않습니다. `qualified=0`은 전략의 모든 재무 조건을 만족하는 후보가 없다는 뜻이며 수집 실패와 구분합니다.

### 2. 키움 매매 프로그램 실행

사용 중인 매매 실행 배치 파일을 실행하거나 다음 명령을 사용합니다.

```cmd
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
set "SYSTEMTRADING_PAPER_TICK_CSV=%CD%\ticks\breakout.csv"
py -3.9-32 main.py
```

로그인, 잔고·미체결·예수금 조회, 전략 초기화, 공통 실시간 등록 결과를 확인합니다. 수집 완료가 전략 초기화 성공을 보증하지 않습니다.

사용자가 제공한 매매 배치 파일은 `SYSTEMTRADING_MOMENTUM_LIVE`를 설정하지 않습니다. 환경에서 전달된 값이 `1`이면 모멘텀 신규매수가 활성화되고, 그렇지 않으면 중지됩니다. 확실하게 끄려면 실행 전에 `set "SYSTEMTRADING_MOMENTUM_LIVE=0"`을 지정합니다. 신규매수 활성화 방법은 13절을 참고하세요. 우량주 주문과 기존 모멘텀 실보유 매도 관리는 별도입니다.

### 3. 웹 API 실행 — 별도 터미널

```cmd
webenv\Scripts\activate
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
python run_web_api.py
```

`http://127.0.0.1:8000/health`와 `/docs`를 확인합니다. 매매 프로그램과 웹 API의 비교계좌 DB 경로가 같아야 합니다.

### 4. React 실행 — 별도 터미널

```cmd
cd web_frontend
npm run dev
```

최초 설치 때만 `npm install`이 필요합니다. 실행 로그에 표시된 주소로 접속합니다. 기본 주소는 `http://localhost:5173`입니다.

### 요청 대상

| 프로그램 | 요청 대상 | 실행 환경 |
|---|---|---|
| 일봉 수집기 | pykrx가 사용하는 외부 데이터 서버 | marketdata 64bit |
| 재무 수집기 | KRX 시가총액 + OpenDART 기업·재무·주식수 정보 | marketdata 64bit |
| main.py | Kiwoom OpenAPI, Telegram 등 | Python 3.9 32bit |
| 웹 API | SQLite 조회, 웹 API 제공 및 기존 웹 기능 | 웹 의존성이 설치된 Python |
| React | FastAPI HTTP 조회 | Node.js |

두 수집기는 키움 API를 호출하지 않습니다. 일봉 요청 후 기본 1초, 캐시 없는 DART 요청 전 0.3초 대기를 적용하지만 KRX/DART 요청 제한을 보장하지는 않습니다. DART는 종목마다 여러 요청이 필요하며 성공 응답 캐시는 24시간입니다. `DART_STATUS:020` 등 DART 오류가 발생하면 수집을 중단합니다. 동시에 같은 수집기를 중복 실행하지 마세요.

[OpenDART 공식 제한 안내](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020)는 일반적으로 20,000건 이상 요청에서 제한 오류가 발생하며 설정에 따라 한도가 다를 수 있다고 설명합니다. 키움 매매 프로세스 자체의 TR/주문 제한 준수 여부는 별도입니다.

---

## 0. 처음 설치할 때 전체 순서

```text
1. Git 설치
2. 저장소 clone
3. Kiwoom OpenAPI+ 설치 + 서비스 신청/로그인 확인
4. Python 3.9 32bit 설치
5. Kiwoom용 32bit 가상환경 생성 + 패키지 설치
6. 기존 marketdata 환경 재사용 또는 새 PC에서 생성 + 수집용 패키지 설치
7. Telegram 환경변수 설정
8. market_history.db 초기 구축
9. KRX/OpenDART 인증 설정 후 ValueQuality 재무 자동 수집
10. DB/환경 사전 점검
11. 32bit 환경에서 main.py 실행
12. webenv에서 FastAPI 실행
13. Node.js 환경에서 React 대시보드 실행
```

**중요:** 새 PC에서 `main.py`부터 실행하면 안 됩니다.  
모멘텀 및 A/B/C 비교에는 `market_history.db`의 종목 마스터와 과거 일봉이 필요하며, 우량주에는 재무 스냅샷이 필요합니다.

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

### 2-2. 과거 일봉·재무 데이터 수집용

현재 확인한 환경은 **Conda `marketdata` / Python 3.12.13 / 64bit**입니다. 기존 환경을 재사용하며 새 Python이나 `.venv-data`를 추가할 필요는 없습니다. 수집 수정본의 요구 버전은 `requirements-value-data.txt`에 지정된 `pykrx==1.2.9`입니다.

`bootstrap_market_history.py`와 `collect_value_fundamentals.py`는 이 환경에서 실행합니다. 수집 패키지를 Kiwoom용 Python 3.9 32bit에 설치하지 않습니다.

---

### 2-3. 웹 API용

FastAPI는 Kiwoom ActiveX를 사용하지 않고 모니터링 DB 조회와 MA 설정 DB 저장을 담당하므로 자동매매용 32bit Python과 분리합니다.

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

### 기존 환경 재사용

Anaconda Prompt에서:

```cmd
conda activate marketdata
cd /d "C:\Users\leesoochan\OneDrive\바탕 화면\포트폴리오\SystemTrading"
python -c "import sys,struct; from importlib.metadata import version; print(sys.executable); print(sys.version); print('bit:',struct.calcsize('P')*8); print('pykrx:',version('pykrx'))"
```

실행 경로가 `envs\marketdata\python.exe`, 비트 수가 `64`인지 확인합니다. 진행 중인 수집이 끝난 뒤 최초 설치 또는 요구 버전 변경 시:

```cmd
python -m pip install -r requirements-value-data.txt
```

매일 패키지를 재설치할 필요는 없습니다.

VS Code의 CMD에서 conda 명령을 찾지 못하면:

```cmd
call "%USERPROFILE%\anaconda3\condabin\conda.bat" activate marketdata
python -c "import sys; print(sys.executable)"
```

위 경로는 현재 사용자 설치 기준입니다. 설치 위치가 다르면 수정합니다. VS Code 하단 인터프리터 선택만으로 기존 터미널의 `python` 환경이 바뀌었다고 판단하지 말고 실행 경로를 확인하세요. `update_market_data.bat`는 marketdata 실행 파일을 직접 지정합니다.

### 새 PC에 환경이 없는 경우에만

```cmd
conda create -n marketdata python=3.12 -y
conda activate marketdata
python -m pip install -r requirements-value-data.txt
```

기존 marketdata가 있다면 환경을 다시 만들지 않습니다.

### 인증 설정

프로젝트 루트의 `.env`에 입력합니다.

```dotenv
DART_API_KEY=발급받은40자리인증키
KRX_ID=KRX로그인아이디
KRX_PW=KRX로그인비밀번호
```

KRX 로그인과 DART 인증키는 서로 다릅니다. 재무 수집기는 pykrx import 전에 `.env`를 읽으며 이미 설정된 운영체제 환경변수가 우선합니다. `.env`와 인증 정보를 Git에 올리거나 로그 전체와 함께 공유하지 않습니다.

```cmd
python collect_value_fundamentals.py --check-market
```

이 명령은 KRX 시가총액 조회를 점검하며 DART 재무 수집이나 스냅샷 저장은 수행하지 않습니다.

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

## 8. market_history.db 초기 구축 및 증분 갱신

64bit marketdata 환경에서:

```cmd
python bootstrap_market_history.py --lookback-days 400 --min-history-rows 180 --sleep 1
```

A/B 비교의 신고가 기준에는 **당일을 제외한 직전 180거래일** 일봉이 필요합니다. C 모멘텀은 최소 127봉과 기준 거래일의 최신 일봉을 확인합니다. 실제 모멘텀도 동일한 후보·진입 규칙을 사용합니다.

- `--lookback-days 400`은 달력일 조회 범위이며 400거래일을 뜻하지 않습니다.
- 180봉 미만 종목은 최신 날짜가 저장되어 있어도 지정한 과거 범위로 보충 조회합니다.
- 충분한 이력이 있는 종목은 마지막 저장일 주변 중복 구간과 누락분을 증분 조회합니다.
- 최신 날짜와 180봉을 모두 충족한 종목은 일봉 조회를 생략합니다. 종목 마스터 갱신은 별도 수행합니다.
- 신규 상장 등 실제 거래일이 부족한 종목은 이력이 쌓여야 합니다. 봉을 인위적으로 만들지 않습니다.
- 휴장일은 조회 종료일이 달력상 어제로 계산되어 이미 저장된 구간을 재조회할 수 있습니다.

이력 보충을 위해 `--overlap-days`를 수백 일로 늘릴 필요는 없습니다. 해당 옵션은 충분한 이력이 있는 종목의 증분 중복 구간을 지정합니다.

주요 데이터는 `stock_master`, `daily_price`입니다. 시작 전 기준값을 계산하고 장중에는 Kiwoom 실시간 틱으로 판단합니다. 수집 완료 후 `eligible_code_count`, `insufficient_history_count`, `failed_codes`를 확인하세요.

---

## 8-1. 가상 비교 DB와 틱 기록 실행

프로젝트 최상위에서 매매 프로그램을 실행할 CMD에 설정합니다.

```cmd
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
set "SYSTEMTRADING_PAPER_TICK_CSV=%CD%\ticks\breakout.csv"
py -3.9-32 main.py
```

같은 실험은 같은 DB를 계속 사용합니다. 날짜별 새 DB로 바꾸면 현금과 보유 상태가 새 실험으로 시작하며, 기존 DB의 기록이 자동 이전되지 않습니다. 환경변수를 생략하면 기본 가상 저장 위치는 `monitoring.db`입니다. 틱 CSV 기록은 선택 사항입니다.

웹 API를 실행할 별도 CMD에서도 웹 환경을 활성화하고 프로젝트 최상위로 이동한 뒤 같은 `SYSTEMTRADING_PAPER_DB`를 설정합니다.

```cmd
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
python run_web_api.py
```

`set`은 해당 CMD에서 시작하는 프로세스에만 적용됩니다. 실행할 때마다 입력하지 않으려면 프로젝트 최상위 `.bat`에 저장할 수 있습니다. [작업 기록의 실행 예시](docs/2026-10-08_README.md#6-가상-db와-틱-csv-설정)를 참고하세요.

### 현재 사용 중인 두 배치 파일

아래 파일명은 설명용 예시입니다. 기존 배치 파일의 이름을 바꾸지 않아도 됩니다. 두 파일을 프로젝트 루트에 둡니다.

매매 실행용 배치 파일:

```bat
@echo off
setlocal
cd /d "%~dp0"
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
set "SYSTEMTRADING_PAPER_TICK_CSV=%CD%\ticks\breakout.csv"
py -3.9-32 main.py
pause
```

웹 API 실행용 배치 파일:

```bat
@echo off
setlocal
cd /d "%~dp0"
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
python run_web_api.py
pause
```

웹 배치 파일의 `python`은 실행 터미널의 환경을 사용합니다. 먼저 webenv를 활성화한 CMD에서 실행하거나, 배치 안에서 `webenv\Scripts\python.exe run_web_api.py`처럼 웹 Python 경로를 명시합니다. 더블클릭할 때 기존 터미널의 marketdata/webenv가 자동으로 전달된다고 가정하지 않습니다.

이 두 파일은 데이터 수집이나 React를 실행하지 않습니다. 데이터 갱신은 `update_market_data.bat`, React는 `web_frontend`에서 `npm run dev`로 따로 실행합니다. `setlocal`은 변수 설정 범위를 제한하고 `pause`는 종료 후 로그 확인을 위해 창을 유지합니다.

A/B/C는 모두 가상 비교계좌입니다. 실제 모멘텀 주문은 별도 실행 어댑터가 담당하며 `SYSTEMTRADING_MOMENTUM_LIVE=1`로 신규매수를 켭니다. 우량주의 기존 증권사 주문은 유지됩니다.
가상 DB 경로를 설정하는 것은 전체 프로그램을 가상 전용으로 바꾸는 기능이 아닙니다. CSV 기록과 `replay_momentum_breakout.py`는 C 대상이며 A/B 전체 재현용이 아닙니다.

---

## 9. ValueQuality 재무 데이터 자동 수집

marketdata 환경에서 KRX 점검 후 20종목 시험 수집:

```cmd
python collect_value_fundamentals.py --check-market
python collect_value_fundamentals.py --limit 20
```

전체 대상 수집 및 상태 확인:

```cmd
python collect_value_fundamentals.py --limit 0
python collect_value_fundamentals.py --stats
```

`--limit` 기본값은 20이며 `0`은 전체 대상입니다. 수집은 KRX 시가총액과 OpenDART 재무정보를 결합해 `value_quality.db`의 `value_fundamental_snapshot`에 검증된 지표를 저장합니다.

| 출력 | 의미 |
|---|---|
| attempted | 수집 시도 종목 수 |
| saved | 이번 실행에서 저장한 종목 수 |
| skipped | 자료 부족 또는 검증 조건으로 제외된 종목 수 |
| total | DB 스냅샷 행 수 |
| validated | 수집기 검증 표시가 있는 행 수 |
| fresh | 검증과 최신성 조건을 만족하는 행 수 |
| qualified | 최신성 및 전략 재무 필터를 모두 통과한 후보 수 |

2026-10-13 사용자 PC 로그에서 KRX 조회 2,781행, 20종목 시도 중 8종목 저장, validated/fresh 각각 8, qualified 0을 확인했습니다. 이는 당시 20종목 시험 결과이며 전체 시장 후보가 0이라는 뜻은 아닙니다.

현재 필터:

```text
0 < PER(TTM) <= 15
0 < PBR <= 1
30 <= 연간 매출총이익률 <= 95
1 <= 연간 총자산회전율 <= 10
```

최근 12개월 이익으로 PER을 계산하고, 최신 자본으로 PBR을 계산합니다. 연결/별도 재무제표 기준을 혼합하지 않습니다. 현재 수집기는 12월 결산·원화·지원 계정 항목 및 단일 보통주 종류 등 제한된 조건을 지원합니다. 여러 주식 종류, 주식수 변경, 필수 항목 누락, 비양수 분모/이익은 제외 사유가 될 수 있으며 기업 자체의 투자 적격 여부를 뜻하지 않습니다.

최신성 정책은 시가총액 기준일 7일, 최신 재무기간 말일 240일, 연간 재무기간 말일 550일 이내입니다. 수집 성공과 투자 조건 충족은 다릅니다. `qualified=0`이어도 `saved/fresh>0`이면 수집이 성공한 사례일 수 있습니다.

수집을 완료한 뒤 매매 프로그램을 재시작해 후보를 다시 로드합니다. 실행 중인 프로그램의 메모리 캐시가 수집 결과로 즉시 교체되는 방식은 아닙니다.

### 기존 CSV 입력 경로

`bootstrap_value_quality_data.py`는 수동 입력용으로 남아 있습니다. 기존 지표 CSV만 넣으면 검증되지 않은 입력으로 저장되므로 자동매매 후보에 포함되지 않습니다. 검증 여부·재무기간·출처 등 수정본의 검증 메타데이터가 필요하며, 현재 운영 절차에서는 자동 수집을 우선 사용합니다. 후보 편입을 위해 검증 표시만 임의로 바꾸지 않습니다.

---

## 10. 현재 운영 전략

실행 등록 전략은 우량주와 모멘텀 두 개입니다. `main.py`는 `LiveMomentumContractionStrategy`를 사용하되 DB의 모멘텀 전략 식별자는 `MomentumContractionStrategy`로 유지합니다.

실제 매수 수량·현금·계좌 전체 보유/대기 제한은 `StrategyManager`가 중앙 관리합니다.

- 전략별 별도 예산 없이 공용 계좌 현금을 사용합니다.
- 신규 종목당 총자산 최대 10%, 계좌 전체 최대 10포지션 제한을 적용합니다.
- 모멘텀은 초기 손절 거리와 거래 비용 가정에 따른 위험 수량 한도도 적용합니다. 기본 위험 비율은 총자산의 0.5%입니다. 갭·체결 지연으로 실제 손실은 계획값을 넘을 수 있습니다.
- 잔고·자금·주문 조회 등 기존 주문 안전 조건을 유지합니다.
- 각 전략은 자신의 전략으로 매핑된 실제 보유를 관리합니다. 기존 신고가·눌림목 보유를 모멘텀으로 자동 이관하지 않습니다.

### 모멘텀 매도 규칙

| 조건 | 처리 |
|---|---|
| 저장된 손절 가격 이하 | 전량 매도 요청; 초기 손절은 진입 신호의 당일 저가 |
| 기본 D+4 이후 비용 가정 반영 수익 상태 | 초기 실제 체결 수량의 약 1/3 부분매도 |
| 목표 부분매도 수량 체결 완료 | 비용 가정 반영 본전 가격 이상으로 손절 상향 |
| 부분청산 이후 확정 종가가 기본 MA10 아래 | 잔여 수량 청산 |

1주 보유는 부분청산 대신 D+4 이후 MA 청산 조건을 사용합니다. 설정값은 `config/momentum_contraction.json`을 확인합니다.
손절은 실행 중인 프로그램이 시세를 받아 주문하는 방식이며 증권사에 상시 저장된 손절 주문이 아닙니다.

### A/B/C 비교

| 계좌 | 후보·매수 | 청산 |
|---|---|---|
| A | 기존 180거래일 신고가 기준 | 초기 2×ATR·추적 3×ATR 및 장 마감 전 MA 수익 청산 |
| B | A + 모멘텀 후보 필터 | A와 동일 |
| C | 모멘텀 후보·수축·첫 구간 돌파 | 당일 저가·D+4 수익 부분청산·확정 종가 MA10 |

A/B 신고가 규칙은 증권사 주문 기능 없는 `util/paper_breakout_baseline.py`에 남깁니다.
A/B MA 설정은 모멘텀 상세 화면에서 저장하고 다음 거래일 적용 정책을 유지합니다. C의 MA 규칙과 별개입니다.
과거 `HighBreakoutStrategy` 설정 키는 DB 호환용으로 남을 수 있으며 실행 전략이 복원된다는 뜻은 아닙니다.

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
  - 별도 현금 가상계좌 paper_account / paper_position / paper_trade 등

value_quality.db
  - ValueQuality 재무 스냅샷
  - 보유 상태
  - 한경 리포트 상태

momentum_live.db
  - 시장 이력 DB와 같은 디렉터리에 생성
  - 계좌별 실제 모멘텀 손절·체결 수량·부분청산·대기 주문 상태
  - 중복 체결 반영 방지 기록

strategy_settings.db
  - A/B MA 설정과 다음 거래일 적용 상태

paper_breakout.db (SYSTEMTRADING_PAPER_DB로 지정한 경우)
  - A/B/C 현금·보유·거래·평가액 이력
```

SQLite WAL 사용 중에는 다음 파일이 생성될 수 있습니다.

```text
*.db-wal
*.db-shm
```

프로그램 실행 중 임의 삭제하지 않습니다. 실제 모멘텀 상태 복원에는 `momentum_live.db`와 `monitoring.db`를 함께 보존합니다. 코드 백업 브랜치만으로 Git에 없는 DB·CSV는 복구되지 않습니다.

---

## 12. 자동매매 실행 전 점검

```cmd
kiwoom\Scripts\activate

python -c "import struct; print('bitness=', struct.calcsize('P') * 8)"
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
python -c "import sqlite3; c=sqlite3.connect('file:market_history.db?mode=ro',uri=True); print(c.execute('select count(*) from stock_master where active=1').fetchone()); print(c.execute('select count(*) from daily_price').fetchone()); c.close()"
python collect_value_fundamentals.py --stats
```

목표:

```text
bitness= 32
QAx OK
stock_master 활성 종목 > 0
daily_price 행 수 > 0 (이것만으로 필요한 180봉 충족을 의미하지 않음)
```

---

## 13. main.py 실행

기존 환경에서 다음을 실행합니다. `set`은 해당 CMD에서 새로 실행하는 프로세스에만 적용됩니다.

```cmd
kiwoom\Scripts\activate
set "SYSTEMTRADING_MOMENTUM_LIVE=1"
python main.py
```

가상환경을 쓰지 않는 기존 실행 방식이라면 `py -3.9-32 main.py`를 사용합니다. 기존 `SYSTEMTRADING_PAPER_DB` 경로도 유지하세요.

모멘텀 신규매수 활성화 로그:

```text
[Momentum] 증권사 신규매수 활성
```

신규매수를 끄려면 매매 프로세스를 종료한 뒤 다음처럼 재실행합니다.

```cmd
set "SYSTEMTRADING_MOMENTUM_LIVE=0"
python main.py
```

이 경우 **신규 모멘텀 매수만 중지**하며 이 코드로 매수한 기존 모멘텀 실보유의 매도 관리는 유지합니다. 우량주 주문을 끄는 변수는 아닙니다.

```text
[Momentum] 신규매수 중지 · 기존 실보유 매도관리 유지
```

후보 준비 로그의 `가상 전용 후보` 표현은 공통 후보 클래스에 남아 있습니다. 실제 신규매수 활성 여부는 위 `[Momentum]` 시작 로그와 실행 CMD 설정으로 확인합니다.
초기화 성공·정상 잔고/미체결/자금 조회·공통 실시간 등록을 확인하세요. 첫 구간 틱이 불완전한 장중 시작은 당일 모멘텀 신규 진입을 보류할 수 있습니다.
실보유와 저장 수량이 불일치하거나 손절 상태가 없으면 실제 모멘텀 자동 관리가 보류됩니다.

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

모니터링 DB 연결은 조회 전용으로 사용합니다. 2026-09-30 추가한 MA 설정 변경은 별도 `strategy_settings.db`에 저장하며 매매 프로그램이 다음 거래일에 읽습니다.

따라서 웹 서버나 브라우저 오류가 Kiwoom ActiveX 객체를 직접 건드리는 구조가 아닙니다.

---

## 15. 가상매매의 목적과 실제 계좌와의 차이

실제 주문 결과와 가상 거래 결과는 독립적으로 계산합니다.

| 구분 | 실제 우량주·모멘텀 | 우량주 기존 가상 지수 | A/B/C 비교 |
|---|---|---|---|
| 자금 | 증권사 공용 계좌 현금 | 비중·수익률 중심 | 계좌별 가상 현금 |
| 수량·비용 | 실제 체결과 계좌 조건 | 기존 가상 계산 방식 | 정수 수량·가상 비용 가정 |
| 성과 | 실제 체결에 따른 결과 | 전략 조건에 따른 가상 성과 | 비교 계좌의 가상 평가액·MDD |

모멘텀 상세의 C 보유·수익률은 실제 모멘텀 잔고가 아닙니다. 실제 포지션은 증권사 잔고와 실제 보유 스냅샷으로 확인합니다.
같은 신호라도 실제 현금 제한, 체결 지연, 미체결 및 체결 가격 차이로 실제 거래와 C 거래가 달라질 수 있습니다.
기존 신고가·눌림목의 가상 기록은 DB에 보존하지만 현재 가상 캐시에 로드하여 계속 거래하지 않습니다.
A/B는 기존 신고가 기준 규칙을 비교 목적으로 계속 평가합니다.

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
우량주 가상 청산 및 중요 상태 변경
중요한 청산 상태 변경
```

즉 **매수/매도 판단은 실시간이고, 웹 표시용 현재가 저장만 최대 5초 간격**입니다.

프로그램 재시작 시에는 `virtual_position`을 DB에서 한 번 읽어 메모리 캐시를 복원합니다.

현재 운영 중인 우량주 가상 보유, A/B/C 보유 및 실제 모멘텀 보유는 신규 후보에서 빠지더라도 실시간 등록 대상에 포함하여 관리를 이어갑니다. 제거된 전략의 과거 legacy 가상 보유는 제외합니다.

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

FastAPI는 `monitoring.db`, `strategy_position.db`를 조회하고, A/B MA 설정을 별도 `strategy_settings.db`에 저장합니다. MA API는 `/api/strategies/MomentumContractionStrategy/ma-settings`이며 과거 신고가 경로는 제거되었습니다.

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

전체 전략 목록에는 우량주·모멘텀 카드만 표시합니다.
우량주 카드에는 기존 YouTube 전략 영상 버튼을 유지하고, 모멘텀 카드와 상세 페이지에는 새 탭에서 열리는 **전략 원문** 버튼을 추가합니다.

원문: [Qullamaggie — 3 timeless setups](https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/).
원문은 2021년 게시 글이며 버튼은 참고 자료 바로가기입니다. 구현의 수축 정량화 등 실험값까지 원문의 유일한 규칙으로 해석하지 않습니다.

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

먼저 프로젝트 루트의 CMD에서 수집을 완료하고 결과를 확인합니다.

```cmd
update_market_data.bat
```

이후 세 프로세스를 각각 유지합니다.

### CMD 1 — Kiwoom 자동매매

현재 매매 실행 배치 파일 또는:

```cmd
cd /d C:\경로\SystemTrading
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
set "SYSTEMTRADING_PAPER_TICK_CSV=%CD%\ticks\breakout.csv"
py -3.9-32 main.py
```

모멘텀 신규매수 변수는 13절에 따라 별도로 선택합니다. 위 예시는 변수를 설정하지 않으므로 이미 전달된 환경값을 사용합니다.

### CMD 2 — FastAPI

```cmd
cd /d C:\경로\SystemTrading
webenv\Scripts\activate
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
python run_web_api.py
```

현재 웹 배치 파일을 사용한다면 웹 환경이 준비된 이 CMD에서 실행합니다.

### CMD 3 — React

```cmd
cd /d C:\경로\SystemTrading\web_frontend
npm run dev
```

| 프로세스 | 역할 |
|---|---|
| CMD 1 | 거래·실시간 처리 및 DB 기록 |
| CMD 2 | 모니터링 조회·MA 설정 API |
| CMD 3 | 사용자 대시보드 |

수집 배치는 세 프로세스를 자동 실행하지 않습니다. 매일 환경을 다시 만들거나 패키지를 재설치할 필요는 없습니다.

---

## 20. 검증 기록

### 2026-10-13 실행 절차 갱신

- 기존 marketdata Python 3.12.13 / 64bit 실행 경로를 사용자 PC에서 확인했습니다.
- KRX 점검과 재무 20종목 시험 수집의 실제 사용자 로그를 확인했습니다. 전체 수집 및 변경 후 전체 전략 초기화 성공까지 확인한 것은 아닙니다.
- README는 현재 GitHub main의 내용을 기준으로 실행 절차를 수정했습니다. 데이터 수집 수정본과 배치 파일은 별도로 설치되어 있어야 합니다.
- `update_market_data.bat`는 정적 검토만 수행했으며 Windows 실제 실행은 아직 확인하지 않았습니다.


### 2026-10-08~09 패치 검증

- 전략 정리 단계: Python 57개 테스트 통과.
- 모멘텀 실제 주문 어댑터 추가 후: Python 68개 테스트 및 compileall 통과.
- 원문 바로가기 추가 후: Vite 프로덕션 빌드 통과.
- 추가 패치 적용 검사 통과; 링크 패치는 정리 버전과 실제 주문 추가 버전 모두에서 적용 검사 통과.
- Windows Kiwoom ActiveX·실제 주문 체결·사용자 PC 화면은 이 환경에서 검증하지 않았습니다. 테스트 통과는 수익성 검증이 아닙니다.

```cmd
python -m unittest discover -s tests -v
python -m compileall -q api strategy util web_api tests replay_momentum_breakout.py
cd web_frontend
npm run build
```

### 2026-08-17 과거 버전 검증 (삭제된 전략 포함)

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
- 실제 모멘텀 부분체결·부분청산 후 잔여 수량과 손절 상태 확인
- A/B/C 가상 거래·현금·평가액 갱신 확인
- 실제 계좌 미체결/잔고 동기화 정상
- DB lock 관련 오류가 없는지
- CPU/메모리 사용량
- ValueQuality 재무 데이터가 준비되어 있다면 후보/매도 관리 확인

---

## 23. 자주 발생하는 오류

### npm ENOENT: package.json을 찾을 수 없음

프로젝트 최상위에서 실행하면 발생합니다. `package.json`이 있는 폴더에서 실행하세요.

```cmd
cd web_frontend
npm run dev
```

### MA 설정이 적용 대기로 계속 표시됨

- 저장 날짜 이후의 거래일인지 확인합니다.
- 매매 프로그램의 A/B 기준 초기화·실시간 틱 수신을 확인합니다.
- 웹과 매매 프로그램이 같은 `strategy_settings.db`를 쓰는지 확인합니다.
- CMD의 `MA 청산 설정 적용` 또는 `MA 설정 적용 지연` 로그를 확인합니다.
- 적용 확인 날짜·시간도 확인합니다. 예전 기록은 현재 실행 여부를 보장하지 않습니다.

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
value_quality.db의 total / validated / fresh / qualified
수집 결과의 제외 사유
시가총액 기준일 및 재무기간 최신성
현재 필터 충족 여부
수집 후 main.py 재시작 여부
```

### 터미널 python이 선택한 VS Code 인터프리터와 다름

```cmd
python -c "import sys; print(sys.executable)"
call "%USERPROFILE%\anaconda3\condabin\conda.bat" activate marketdata
python -c "import sys; print(sys.executable)"
```

위 명령은 CMD 기준입니다. 인터프리터 선택과 기존 터미널 환경을 구분하고 실제 경로를 확인합니다.

### KRX 조회 실패 또는 DataFrame 컬럼 KeyError

marketdata 환경의 pykrx 요구 버전과 `.env`의 `KRX_ID`, `KRX_PW`를 확인한 뒤:

```cmd
python collect_value_fundamentals.py --check-market
```

DART 인증키만으로 KRX 로그인을 대체할 수 없습니다. 인증 정보를 포함한 로그는 공유하지 마세요.

### DART_STATUS:020

요청 제한 오류입니다. 수집을 계속 재실행하지 말고 OpenDART 이용현황과 인증키의 요청 한도를 확인합니다. 종목 수가 곧 요청 횟수는 아닙니다.

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
4. `monitoring.db`, `strategy_position.db`, `value_quality.db`, `market_history.db`, `momentum_live.db`, `strategy_settings.db`, 사용 중인 가상 비교 DB 및 과거 `breakout_atr_state.db`를 실행 중 임의 삭제하지 않습니다.
5. 테스트용 `T9...` 가상 포지션은 운영 전에 제거합니다.
6. Telegram token을 Git에 저장하지 않습니다.
7. 실전 계좌 전환 전 모의투자에서 충분히 검증합니다.
8. `time_helper.py`의 휴장일 목록을 매년 갱신합니다.
9. ValueQuality를 실제 운용하려면 재무 스냅샷을 주기적으로 갱신해야 합니다.
10. 웹 화면의 최대 수 초 지연은 의도된 DB 스냅샷/polling 구조입니다.
11. 제거된 전략의 실제 보유·미체결은 수동 관리 후 증권사 재조회로 확인합니다. DB의 전략 이름만 바꾸어 모멘텀에 넘기지 않습니다.
12. 모멘텀 상태가 없거나 주문 접수 여부가 불명확하면 자동 관리를 보류할 수 있습니다. 실제 체결·미체결과 저장 상태를 확인합니다.
13. 실제 보유 중 이전 코드로 되돌리면 해당 자동 관리가 중단될 수 있습니다. 코드 복구와 포지션 관리는 별개입니다.

---

## 25. 설치/운영 체크리스트

- [ ] Git clone / pull 완료
- [ ] Kiwoom OpenAPI 설치/버전처리 완료
- [ ] Python 3.9 32bit 확인
- [ ] QAxContainer import 확인
- [ ] marketdata Python 실행 경로·64bit·pykrx 요구 버전 확인
- [ ] KRX 로그인 및 DART 인증키 설정
- [ ] `collect_value_fundamentals.py --check-market` 성공
- [ ] `update_market_data.bat`를 프로젝트 루트에 배치
- [ ] market_history.db 구축 및 필요한 종목의 180봉 이력 확인
- [ ] ValueQuality 재무 수집 후 fresh/qualified 및 제외 사유 확인
- [ ] Telegram 환경변수 설정
- [ ] main.py 초기화 성공
- [ ] `VirtualStrategyEngine` 캐시 복원 로그 확인
- [ ] 공통 실시간 등록 성공
- [ ] webenv 설치
- [ ] FastAPI `/health` 확인
- [ ] Node.js/npm 설치
- [ ] React `npm run dev` 확인
- [ ] 테스트 데이터 제거
- [ ] 같은 가상 DB 경로를 매매·웹 CMD에 설정
- [ ] 모멘텀 신규매수 환경변수 및 시작 로그 확인
- [ ] 기존 신고가·눌림목 실제 보유·미체결 관리 확인
- [ ] 다음 거래일 장중 실시간 화면 갱신 최종 확인
---

## License

This project is licensed under the GNU General Public License v3.0 only (GPL-3.0-only).

See [LICENSE](./LICENSE) for details.

Third-party open-source dependencies and licenses are documented in [THIRD_PARTY.md](./THIRD_PARTY.md).

