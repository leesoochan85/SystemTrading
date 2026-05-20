# SystemTrading 작업 환경 세팅 가이드

Kiwoom OpenAPI+ 기반 자동매매 프로그램입니다.  
RSI, 신고가 돌파, 볼린저 밴드 추세/반전 전략을 사용하며, Telegram 알림과 네이버 경제 뉴스 전송 기능을 포함합니다.

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

## 8. 조회 제한 -209 대응

아래 팝업이 뜨면 Kiwoom TR 조회 제한에 걸린 것입니다.

```text
조회횟수 제한 : -209
현재 고객님 프로그램에서 과도한 조회요청이 발생되고 있습니다.
```

원인:

```text
새 PC에서는 DB가 비어 있어서 각 전략이 200개 종목 가격 데이터를 대량 조회함.
전략 4개 기준 최대 800회 이상 TR 조회 가능.
```

대응 방법:

```text
1. main.py 종료
2. 10~30분 대기
3. 기존 PC의 .db 파일을 새 PC로 복사
4. 또는 get_price_data 호출 사이에 time.sleep(4) 추가
```

테스트용으로 유니버스 수를 줄일 수도 있습니다.

`util/make_up_universe.py`:

```python
df = df.loc[:199, ['종목코드', '종목명', '현재가', '거래량', 'PER', 'ROE']]
```

테스트 시:

```python
df = df.loc[:9, ['종목코드', '종목명', '현재가', '거래량', 'PER', 'ROE']]
```

---

## 9. 기존 PC에서 복사하면 좋은 DB 파일

새 PC에서 대량 TR 조회를 피하려면 기존 PC의 DB 파일을 복사합니다.

```text
RSIStrategy.db
HighBreakoutStrategy.db
BandTrendStrategy.db
BandReversionStrategy.db
strategy_position.db
sent_news.db
universe_price.db
```

복사 위치:

```text
C:\Users\사용자명\Downloads\SystemTrading
```

---

## 10. 실행 방법

모든 설정 완료 후:

```cmd
cd C:\Users\사용자명\Downloads\SystemTrading
kiwoom\Scripts\activate
python main.py
```

정상 흐름:

```text
Kiwoom 로그인
미체결 조회
잔고 조회
예수금 조회
전략 초기화
통합 유니버스 생성
타이머 시작
장 시간이 아니면 대기
장중이면 전략 검사 시작
```

장외에는 매수/매도 주문이 나가지 않습니다.  
장중에 전략 조건이 충족되면 주문이 발생합니다.

---

## 11. 자동매매 동작 조건

프로그램이 정상적으로 초기화되어 계속 실행 중이면, 장 시작 후 전략 검사가 진행됩니다.

```text
프로그램 실행
→ Kiwoom 로그인 성공
→ 전략 초기화 완료
→ 타이머 시작
→ 장외에는 대기
→ 장중에는 유니버스 종목 순차 검사
→ 매수 조건 충족 시 매수 주문
→ 보유 종목의 매도 조건 충족 시 매도 주문
```

단, “무조건 매수/매도”가 아니라 조건이 충족되어야 주문이 발생합니다.

---

## 12. Git 작업 흐름

수정 상태 확인:

```cmd
git status
```

변경 파일 추가:

```cmd
git add .
```

커밋:

```cmd
git commit -m "수정 내용"
```

GitHub에 업로드:

```cmd
git push
```

원격 최신 코드 받기:

```cmd
git pull
```

---

## 13. 자주 발생한 오류 정리

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

## 14. 주의사항

```text
1. main.py 실행 전 Kiwoom OpenAPI 버전처리 완료
2. Kiwoom OpenAPI는 Python 3.9 32-bit 환경 사용
3. Telegram 토큰은 GitHub에 올리지 말고 환경변수로 관리
4. 새 PC에서는 .db 파일이 없으면 조회 제한에 걸릴 수 있음
5. 초기화 완료 로그를 확인한 뒤 장중 자동매매를 맡기는 것이 안전함
6. BotFather 토큰이 노출되면 즉시 재발급
7. 처음 세팅하는 PC에서는 유니버스 수를 줄이거나 TR 요청 간격을 늘려 테스트 권장
```