# 우량주·모멘텀만 운영하고 A/B/C 비교 유지

기준 소스: GitHub main 1df9b1174db81965d024021ee9d67dc5559cee99 (2026-10-08 확인).
이 패키지는 사용자 PC나 GitHub main에 직접 적용되지 않았습니다.

## 변경 결과

- main.py는 우량주·모멘텀만 Manager에 전달합니다.
- strategy/HighBreakoutStrategy.py와 strategy/PullbackTrendStrategy.py를 삭제합니다.
- 신고가의 순수 매수·ATR/MA 청산 규칙을 util/paper_breakout_baseline.py로 옮깁니다. 이 모듈에는 SendOrder, 실매수, 실매도, Qt 스레드 또는 실시간 리스너가 없습니다.
- PaperComparisonEngine이 A/B 기준 객체를 소유합니다. 동일 틱으로 기존 A/B/C를 계속 평가합니다.
- 기존 A/B/C 계좌 이름과 가상 DB를 그대로 사용하므로 현금·보유·거래·성과 이력을 이어갑니다. 과거 실험의 재시작 및 부분청산 검증도 유지합니다.
- Manager는 우량주·모멘텀 후보, A/B 신고가 기준 대상, 가상 보유의 합집합을 등록합니다. 체결시간/현재가/시가/고가/저가/누적거래량/개별 체결량/매수·매도호가를 공통 등록합니다.
- 과거 눌림목·신고가의 legacy 가상 포지션은 원본 DB에 남기지만 현재 가상 캐시에 로드하지 않습니다.
- 메인 대시보드에는 우량주·모멘텀만 표시합니다. 모멘텀 상세의 A/B/C 비교는 유지합니다.
- A/B MA 설정을 모멘텀 화면으로 이동했습니다. 동일 strategy_settings.db와 revision을 사용합니다. 내부의 과거 HighBreakoutStrategy 적용 키는 DB 호환 목적으로 유지합니다. 설정은 A/B에만 적용하며 C는 기존 MA10 확정 종가 규칙을 사용합니다.
- 웹 MA API는 /api/strategies/MomentumContractionStrategy/ma-settings입니다. 과거 신고가 상세/설정 경로는 404입니다.
- 기존 C-only CSV 리플레이는 enable_baseline=False로 그대로 C만 거래합니다.
- 사용하지 않는 눌림목 지표·런타임 함수를 제거했습니다. 공통 DB, ATR, 위험 수량, 일봉 수집, 우량주 함수는 유지합니다.

## ZIP 구성

- changed_files/: 추가/변경된 파일의 전체 코드. 부분 조각이 아닙니다.
- full_source/: 수정 후 전체 소스 스냅샷. DB, CSV, 가상환경, node_modules, 빌드 결과, .git은 포함하지 않습니다. 과거 docs 전체는 포함하지 않으므로 기존 프로젝트에 통째로 교체하지 말고 패치를 적용하세요.
- strategy_cleanup.patch: 추가·수정·삭제를 한 번에 적용하는 패치.
- DELETED_FILES.txt: 제거되는 파일 목록.
- CHANGE_MANIFEST.txt: 변경 목록.
- verification.txt: 테스트/빌드/패치 적용 검증 결과.

## 적용 전

1. 매매 프로그램과 웹 API를 종료합니다.
2. 백업 브랜치가 원격에 있는지 확인합니다.
3. DB와 CSV를 별도 백업합니다. 이 패치는 DB/CSV를 삭제하거나 초기화하지 않습니다.
4. 실제 신고가·눌림목 보유/미체결을 확인합니다. 이 패치는 주식을 매도하거나 주문을 취소하지 않습니다. 제거 후 해당 전략의 자동 매도 관리가 중단되므로 기존 포지션 관리 방법을 정한 뒤 실행합니다.
5. ZIP을 프로젝트 바깥에 압축 해제합니다. 아래 경로는 예시이므로 실제 압축 해제 위치로 바꿉니다.

## CMD에서 패치 적용 (프로젝트 최상위)

```cmd
cd /d "C:\Users\leesoochan\OneDrive\바탕 화면\포트폴리오\SystemTrading"
git status --short
git log -1 --oneline
git switch -c refactor/value-momentum-abc
set "CLEANUP_PACKAGE=C:\Users\leesoochan\Downloads\SystemTrading_strategy_cleanup"
git apply --check "%CLEANUP_PACKAGE%\strategy_cleanup.patch"
```

status에 변경사항이 있으면 먼저 보존합니다. --check가 실패하면 적용하지 말고 출력과 변경 파일을 확인합니다.
기준 커밋과 달라도 --check가 성공할 수 있지만 별도 변경 내용의 호환성을 확인해야 합니다.

검사 성공 후:

```cmd
git apply "%CLEANUP_PACKAGE%\strategy_cleanup.patch"
git status --short
python -m unittest discover -s tests -v
python -m compileall -q api strategy util web_api tests replay_momentum_breakout.py
cd web_frontend
npm run build
cd ..
```

Python 테스트 환경에는 pandas, fastapi, httpx, requests, beautifulsoup4 등 해당 소스의 테스트 의존성이 필요합니다.
Kiwoom 실행은 Windows Python 3.9 32bit를 사용합니다. 테스트용 환경과 실행 환경은 다를 수 있습니다.
원래 .env/계좌 정보/의존성 lockfile/DB를 패키지 파일로 덮어쓰지 않습니다.

검증 후 커밋·새 브랜치 푸시:

```cmd
git add main.py strategy util web_api web_frontend/src tests replay_momentum_breakout.py README.md docs/2026-10-08_strategy_cleanup.md
git diff --cached --stat
git commit -m "Keep value and momentum strategies with paper ABC baselines"
git push -u origin refactor/value-momentum-abc
```

main 통합은 이 브랜치 검증 후 진행합니다. 이전 백업 브랜치는 수정하지 않습니다.

## 실행 확인

```cmd
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
set "SYSTEMTRADING_PAPER_TICK_CSV=%CD%\ticks\breakout.csv"
py -3.9-32 main.py
```

기존 실험이 다른 DB를 사용했다면 그 기존 경로를 지정합니다. 위 예시 때문에 새 DB로 바꾸지 않습니다.
웹 API도 같은 SYSTEMTRADING_PAPER_DB로 실행합니다.

- 대시보드 카드 2개 (우량주/모멘텀)
- 모멘텀 화면의 A/B/C 평가액·성과·기존 이력
- A/B MA 설정 저장 및 다음 거래일 적용 확인
- 공통 FID 등록 로그 및 C 첫 구간 수신 상태
- 재시작 후 A/B/C 보유 수량·손절선·현금 복원
- 실제 우량주 주문/잔고 안전장치

신규 C 진입 검증은 개장 전에 등록해야 합니다. 장중 시작하면 첫 5분 무결성 정책에 따라 당일 신규 진입이 보류될 수 있습니다.
기존 A/B 후보가 데이터 부족으로 0개라면 180거래일 일봉을 먼저 확보합니다.

## 검증 범위와 한계

Linux에서 Python 57개 테스트, 문법 검사, Vite 프로덕션 빌드 통과.
새 검증은 두 운영 전략만으로 A/B/C 동시 가상 진입, 실주문 미호출, 공통 FID/후보/보유 합집합,
과거 비활성 가상 포지션 제외, 웹 전략 목록 2개, MA 설정 revision/경로를 확인합니다.
삭제된 실전 신고가 주문 테스트 1개를 제외하고 기존 비교/ATR/리플레이 검증은 유지했습니다.
Windows ActiveX 로그인, 실제 실시간 수신, 실제 계좌 주문, 사용자 브라우저 화면은 이 환경에서 확인하지 않았습니다.
테스트는 수익성 검증이 아닙니다. C는 가상 전용이며 A/B도 비교 계좌입니다.

## 복원

적용 직후 추가 수정 전에는 git apply -R --check 및 git apply -R로 되돌릴 수 있습니다.
추가 작업 또는 커밋이 있다면 백업 브랜치에서 새 복원 브랜치를 만드는 방법을 사용합니다.
과거 코드 복원은 DB를 자동 복원하지 않으므로 별도 DB 백업도 보관합니다.
