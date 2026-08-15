# SystemTrading 작업 환경 세팅 가이드


## 1. 새 PC에서 프로젝트 받기

ZIP 다운로드보다 `git clone`을 권장합니다.

```cmd
cd C:\Users\사용자명\Downloads
git clone https://github.com/leesoochan85/SystemTrading.git
cd SystemTrading
git status
```

정상 예시:

```cmd
On branch main
Your branch is up to date with 'origin/main'.
```

Git이 인식되지 않으면 Git for Windows를 설치해야 합니다.

```cmd
git --version
where git
```

---

## 2. 필수 환경

### Python

Kiwoom OpenAPI는 32비트 ActiveX 기반이므로 일반적으로 **Python 3.9 32-bit** 환경을 사용합니다.

권장:

```text
Python 3.9.x 32-bit
```

설치된 Python 확인:

```cmd
py -0p
```

`-3.9-32`가 보여야 합니다.

---

## 3. 가상환경 생성

프로젝트 폴더에서 실행합니다.

```cmd
cd C:\Users\사용자명\Downloads\SystemTrading
py -3.9-32 -m venv kiwoom
kiwoom\Scripts\activate
```

정상 활성화 예시:

```cmd
(kiwoom) C:\Users\사용자명\Downloads\SystemTrading>
```

Python 버전과 비트 확인:

```cmd
python --version
python -c "import platform; print(platform.architecture())"
```

정상 목표:

```cmd
Python 3.9.x
('32bit', 'WindowsPE')
```

---

## 4. 패키지 설치

32비트 Python에서는 일부 최신 패키지가 설치 실패할 수 있으므로 버전을 고정합니다.

```cmd
python -m pip install --upgrade pip wheel
pip install "setuptools==80.10.2" --force-reinstall
pip install numpy==1.24.4 pandas==1.5.3 matplotlib==3.7.5 --prefer-binary
pip install requests beautifulsoup4 lxml PyQt5 openpyxl --prefer-binary
pip install pykrx --no-deps
pip install datetime xlrd deprecated multipledispatch
```

설치 확인:

```cmd
python -c "import numpy, pandas, matplotlib, requests, bs4, lxml, pykrx, openpyxl; print('basic packages OK')"
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
python -c "from pykrx import stock; print('pykrx OK')"
```

`pykrx` 실행 시 `pkg_resources is deprecated` 경고가 나올 수 있지만, `pykrx OK`가 출력되면 실행 자체는 가능합니다.  
단, `setuptools`를 최신 버전으로 올리면 `pykrx`가 깨질 수 있으므로 `setuptools==80.10.2` 또는 `setuptools<81` 상태를 유지합니다.

---

## 5. Kiwoom OpenAPI+ 설치 및 버전처리

새 PC에는 Kiwoom OpenAPI+를 설치해야 합니다.

설치 확인:

```cmd
dir C:\OpenAPI
```

아래 파일들이 있으면 정상입니다.

```text
KOAStudioSA.exe
opstarter.exe
opversionup.exe
khopenapi.ocx
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

만약 “OpenAPI를 사용하는 프로그램을 모두 종료하세요”가 나오면 관련 프로세스를 확인합니다.

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

## 6. OpenAPI 등록 사용자 오류

새 PC에서 아래 메시지가 나올 수 있습니다.

```text
등록된 사용자가 아닙니다.
```

확인할 것:

```text
1. 기존 PC에서 쓰던 키움 ID와 같은 ID인지 확인
2. 키움 OpenAPI+ 서비스 사용 신청 여부 확인
3. 모의투자/실전투자 계정 구분 확인
4. 새 PC의 공동인증서/보안 프로그램 설정 확인
```

OpenAPI 사용 신청은 보통 계정 단위이지만, 새 PC에서는 인증서, 보안 모듈, 버전처리를 다시 해야 할 수 있습니다.

---

## 7. Telegram 환경변수 설정

Telegram 알림을 사용하려면 새 PC에서도 환경변수를 설정해야 합니다.

필요한 환경변수:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

CMD에서 설정:

```cmd
setx TELEGRAM_BOT_TOKEN "봇_토큰"
setx TELEGRAM_CHAT_ID "채팅_ID"
```

설정 후 CMD를 완전히 닫고 새로 열어야 적용됩니다.

확인:

```cmd
echo %TELEGRAM_BOT_TOKEN%
echo %TELEGRAM_CHAT_ID%
```

Python에서 확인:

```cmd
python -c "import os; print(os.getenv('TELEGRAM_BOT_TOKEN')); print(os.getenv('TELEGRAM_CHAT_ID'))"
```

전송 테스트:

```cmd
python -c "from util.notifier import send_message; send_message('텔레그램 테스트 메시지')"
```

Telegram 오류 의미:

```text
401 Unauthorized: 봇 토큰 오류
404 Not Found: 봇 토큰 형식 오류 또는 잘못된 토큰
400 Bad Request: chat_id 오류 가능성
```

봇 토큰이 외부에 노출되면 BotFather에서 반드시 재발급합니다.

---

## 8. 자주 발생한 오류 정리

### git 명령어 인식 안 됨

```text
'git'은(는) 내부 또는 외부 명령...
```

해결:

```text
Git for Windows 설치 후 CMD 새로 열기
```

---

### libcurl-4.dll 오류

```text
fatal: failed to load library 'libcurl-4.dll'
```

해결:

```text
Git 재설치 또는 Git Bash에서 clone 시도
```

---

### openpyxl 없음

```text
ModuleNotFoundError: No module named 'openpyxl'
```

해결:

```cmd
pip install openpyxl
```

---

### pkg_resources 없음

```text
ModuleNotFoundError: No module named 'pkg_resources'
```

해결:

```cmd
pip install "setuptools==80.10.2" --force-reinstall
```

---

### PyQt5 QAxContainer 확인

```cmd
python -c "from PyQt5.QAxContainer import QAxWidget; print('QAx OK')"
```

`QAx OK`가 나오면 PyQt5 ActiveX 모듈은 정상입니다.

---

## 9. 주의사항

```text
1. main.py 실행 전 Kiwoom OpenAPI 버전처리 완료
2. Kiwoom OpenAPI는 Python 3.9 32-bit 환경 사용
3. Telegram 토큰은 GitHub에 올리지 말고 환경변수로 관리
4. 새 PC에서는 .db 파일이 없으면 조회 제한에 걸릴 수 있음
5. 초기화 완료 로그를 확인한 뒤 장중 자동매매를 맡기는 것이 안전함
6. BotFather 토큰이 노출되면 즉시 재발급
7. 처음 세팅하는 PC에서는 유니버스 수를 줄이거나 TR 요청 간격을 늘려 테스트 권장
```

# SystemTrading

Kiwoom OpenAPI+ 기반의 국내 주식 자동매매 프로젝트입니다.



## 현재 실행 전략

`main.py` 기준 활성 전략은 3개입니다.

1. **HighBreakoutStrategy**
   - 직전 60거래일 신고가 돌파
   - 거래량 및 거래대금 필터
   - -5% 손절 / MA20 이탈 시 시장가 전량매도

2. **PullbackTrendStrategy**
   - MA5 > MA20 > MA60 정배열
   - MA20 부근 눌림목 진입
   - -5% 손절
   - 거래량 +15% & 음봉 매도
   - 전고점 30% 부분익절
   - 다음 거래일 거래량 감소 시 잔량 매도

3. **ValueQualityStrategy**
   - TTM PER 0~15
   - PBR 0~1
   - 매출총이익률 30~95%
   - 총자산회전율 1~10
   - -10% 손절 / +30% 익절

## 핵심 구조

```text
64bit marketdata Python
    ↓
pykrx / 외부 과거 데이터
    ↓
market_history.db
    ↓
32bit Kiwoom 자동매매
    ↓
StrategyManager
    ├─ HighBreakout
    ├─ PullbackTrend
    └─ ValueQuality
    ↓
실시간 이벤트 기반 감시
    ↓
조건 충족 시 주문
```

## 실시간 FID

2026-08-15 기준 공통 실시간 FID를 8개에서 4개로 축소했습니다.

```text
현재가
시가
누적거래량
(최우선)매수호가
```

`StrategyManager`가 event-driven 전략별 필요 FID의 합집합을 계산하고,
`Kiwoom.py`도 해당 FID만 `GetCommRealData`로 읽습니다.

## 자금/리스크 정책

- 전략별 고정 예산 배분 없음
- 하나의 계좌 공용 현금 사용
- 종목당 총자산 최대 10%
- 계좌 전체 보유 + 신규매수 미체결 + 예약 종목 최대 10개
- 실제 매수 주문은 StrategyManager를 통해 중앙 관리

## Telegram

Telegram은 계속 사용합니다.

사용 용도:

- 매수/매도 주문 및 체결 알림
- 상태(`/status`) 확인
- ValueQualityStrategy 보유종목의 한경 컨센서스 기업 리포트

**기존 네이버 일반 경제뉴스 자동 전송 기능은 제거했습니다.**

따라서 `util/notifier.py`는 삭제하면 안 됩니다.

## 한경 기업 리포트

ValueQualityStrategy 실제 보유종목에 대해서만 확인합니다.

```text
30분마다 확인
    ↓
마지막 확인 report_id 이후만 검사
    ↓
보유종목과 일치하는 새 리포트
    ↓
Telegram 전송
```

리포트 상태/중복 전송 정보는 `value_quality.db`에 저장합니다.

## 주요 DB

### 유지

```text
market_history.db
strategy_position.db
monitoring.db
value_quality.db
```

`universe_price.db`는 기존 db_helper 호환 코드 정리가 끝날 때까지 유지합니다.

### 제거된 레거시 DB

코드 참조가 없는 경우 정리 스크립트가 아래 파일을 백업 후 삭제합니다.

```text
sent_news.db
RSIStrategy.db
BandTrendStrategy.db
BandReversionStrategy.db
HighBreakoutStrategy.db
ORBStrategy.db
```

## 실행 환경

### 자동매매

```text
Windows
Python 3.9 32-bit
PyQt5 QAxContainer
Kiwoom OpenAPI+
```

### 과거 데이터 수집

```text
64-bit conda env: marketdata
pykrx
pandas
numpy
```

## 현재 중요 파일

```text
main.py

api/
  Kiwoom.py

strategy/
  StrategyManager.py
  HighBreakoutStrategy.py
  PullbackTrendStrategy.py
  ValueQualityStrategy.py

util/
  market_history.py
  value_quality_data.py
  hankyung_report_helper.py
  db_helper.py
  notifier.py
  time_helper.py
  const.py
```

## 2026-08-15 정리

- 일반 경제뉴스 자동 전송 제거
- `sent_news.db` 관련 코드 제거
- 사용하지 않는 RSI/Band/ORB 전략 정리
- 과거 전략별 DB 정리
- 한경 컨센서스 리포트만 ValueQuality 보유종목에 전송
- 한경 리포트 검사 주기 30분
- 실시간 FID 8개 → 4개 축소
- HighBreakout MA20 이탈 매도 시장가 통일
- StrategyManager에서 전략별 FID 합집합 관리
- Kiwoom 실시간 콜백도 필요한 FID만 조회

## 다음 거래일 장중 확인

아래 로그를 확인합니다.

```text
[Kiwoom] 주식체결 실시간 FID 적용: 4개 / [...]
[StrategyManager] event-driven 공통 실시간 등록:
...종목 / 3전략 공유 / FID 4개 [...]
```

추가 확인:

- 현재가/시가/누적거래량/최우선매수호가 정상 수신
- HighBreakout 매수/시장가 매도 정상
- PullbackTrend 매수/매도 정상
- ValueQuality 매수/손절/익절 정상
- CPU/메모리 사용량
- 실시간 틱 처리 지연 여부

> `api/Kiwoom.before_fid_opt_*.py` 백업은 장중 FID 최적화가 정상임을 확인할 때까지 삭제하지 않습니다.
