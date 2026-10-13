# KRX 시가총액 KeyError — 수집 환경 및 로그인 조건 보완

## 확인 내용

사용자 Python은 3.9-64, pykrx는 1.0.51이다. 시가총액 조회의 반환 DataFrame에 필요한 컬럼이 없어
pykrx 내부 휴장 검사에서 KeyError가 발생했다. 사용자 환경에서 KRX 응답이 비어버린 직접 이유(인증/접속 상태)는 아직 확인되지 않았다.
공식 pykrx 이슈 #297에 같은 버전과 같은 오류가 보고됐고, 유지보수자는 업데이트 및 환경변수 설정 후 정상 조회를 확인했다고 안내한다.

2026-10-13 배포된 pykrx 1.2.9 wheel의 METADATA를 확인했다: Python>=3.10, numpy>=2.0, pandas>=2.2,<3.
공식 README는 인증 대상 조회에 KRX_ID/KRX_PW를 요구한다. DART_API_KEY와 별개다.
앞서 requirements-value-data.txt의 pykrx>=1.0은 오래된 1.0.51도 허용하므로 이 조회 조건에 충분하지 않았다.

## 패치 적용

ZIP의 krx_access_fix.patch를 프로젝트 루트로 복사하고 매매 프로그램을 종료한 뒤 실행한다.

```cmd
git apply --check --ignore-space-change .\krx_access_fix.patch
git apply --ignore-space-change .\krx_access_fix.patch
```

첫 명령이 실패하면 다음 명령을 실행하지 않는다. 앞선 재무 수집 패치에 추가하는 변경분이다.
이 패치는 Kiwoom 매매용 Python 3.9-32 환경의 패키지를 업그레이드하지 않는다.

## .env 설정

KRX 계정은 https://data.krx.co.kr/ 의 회원 계정이다. 조회가 필요한 계정 접근/약관 상태는 웹사이트에서 확인한다.
기존 DART_API_KEY를 유지하고 다음 항목을 추가한다. 실제 비밀번호는 대화나 Git에 올리지 않는다.

```dotenv
DART_API_KEY=발급받은40자리인증키
KRX_ID="한국거래소로그인아이디"
KRX_PW="한국거래소로그인비밀번호"
```

.env는 프로젝트 루트에 둔다. 비밀번호의 ${...} 표현 등이 의도치 않게 치환되지 않도록 dotenv interpolation을 끈다.
이미 같은 이름의 OS 환경변수가 설정돼 있으면 그것이 .env보다 우선한다.

## 수집용 환경 생성 — conda 없이 실행

현재 Python 3.9에서도 다음 설정 스크립트를 실행할 수 있다.

```cmd
python setup_value_data_env.py
```

이미 설치된 Python>=3.10 64bit를 확인하고 프로젝트의 .venv-data 가상환경을 만든다.
Windows py launcher의 3.12/3.11/3.13/3.10/3.14 64bit를 검사한다. 기존 .venv-data가 있으면 적합성을 확인하고 사용한다.
적합한 Python이 없으면 설치를 강행하지 않고 안내 후 종료한다. 이 경우 Python 3.11 또는 3.12 64bit를 설치한 뒤 다시 실행한다.
설치 주소: https://www.python.org/downloads/windows/
설정 스크립트는 수집 전용 가상환경에만 의존성을 설치한다. 시스템 Python이나 Kiwoom 32bit 환경을 변경하지 않는다.
기존 .venv-data가 부적합하면 자동 삭제/덮어쓰지 않는다.

## KRX만 먼저 확인

```cmd
.venv-data\Scripts\python.exe collect_value_fundamentals.py --check-market
```

이 명령은 .env를 로드하고 KRX 시가총액/상장주식수를 확인한다. DART 호출이나 재무 DB 입력은 하지 않는다.
정상 결과 예시는 [KRX check OK] date=... rows=... 및 시가총액/상장주식수 표다.

정상 조회 후:

```cmd
.venv-data\Scripts\python.exe collect_value_fundamentals.py --limit 20
py -3.9-32 collect_value_fundamentals.py --stats
```

20개 수집 결과가 확인되면 --limit 0으로 전체 수집할 수 있다. 완료 후 main.py를 재시작한다.

## 추가한 오류 구분

- PYKRX_UPGRADE_REQUIRED: 오래된 pykrx 버전, 조회 전 중단.
- KRX_CREDENTIALS_REQUIRED: KRX_ID/KRX_PW 누락, 조회 전 중단.
- KRX_RESPONSE_COLUMNS_MISSING: pykrx KeyError를 인증/조회 응답 점검 안내로 변환.
- KRX_MARKET_CAP_COLUMNS_CHANGED: 비어 있지 않은 응답의 필수 컬럼 누락. 휴장으로 처리하지 않는다.
- KRX_MARKET_CAP_REQUEST_FAILED: 예외 종류만 추가 출력한다. 예외 원문 URL/비밀번호는 출력하지 않는다.
- KRX_NO_RECENT_MARKET_CAP: 10달력일 범위에서 유효한 시가총액을 찾지 못함.

주말 조회를 건너뛰고 시가총액/주식수 양수·유한값·주식수 정수 조건을 검증한다.
종목코드 중복은 오류로 처리한다. 현재 DART 매핑이 지원하지 않는 영문 포함 코드는 전체 수집을 중단시키지 않고 제외한다.

## 검증과 한계

기존/신규 총 136개 테스트 통과. 신규 7개는 구버전, KRX 인증정보 누락, 컬럼 누락,
휴장/0 응답, 값 검증 및 명시적 함수 호출을 확인한다. Python 3.9 문법 파싱 및 LF/CRLF 패치 적용 확인.
테스트는 모의 pykrx 응답이다. 실제 KRX 계정 로그인과 사용자 PC의 환경 설치는 수행하지 않았다.
최신 패키지 및 로그인 설정만으로 모든 접속/서비스 오류의 해결을 보장하지 않는다. --check-market 결과로 실제 성공 여부를 확인한다.

## 근거

- https://github.com/sharebook-kr/pykrx/issues/297
- https://github.com/sharebook-kr/pykrx/blob/master/README.md
- pykrx 1.2.9 배포 wheel METADATA 및 pykrx/website/comm/auth.py (2026-10-13 확인)

이 문서의 환경 실행 명령이 이전 재무 수집 안내의 conda/Python 3.9 수집 명령을 대체한다.
