# 2026-10-08 Breakout 구현과 데이터 검증

이번 개발은 기존 180일 신고가의 연결 오류 수정과 PDF·영상 기반 모멘텀 수축 돌파에 집중한다. Parabolic과 EP는 이번 브랜치에 포함하지 않는다.
기본 전략·A/B/C 계좌·비용·웹 화면은 [10월 5일 운영 안내](2026-10-05_README.md)에 설명되어 있다.
사용자 PC에 패치가 적용되었는지는 확인하지 못했으며, 이번 전달본은 GitHub 기준 코드부터 적용하는 전체 패치와 이전 구현 뒤에 적용하는 증분 패치를 제공한다.

## 수정·추가한 코드

| 파일 | 역할 |
| --- | --- |
| `strategy/HighBreakoutStrategy.py` | 기존 실매수에 ATR 수량 계산 인자 전달, 누락된 실·가상 매도 평가와 저장된 MA 설정 연결; 공통 FID15 추가 |
| `strategy/MomentumContractionStrategy.py` | 새 전략의 일봉 후보, 첫 구간/횡보 돌파, 고정 당일 저가 손절과 확정 종가 MA 문맥; 종목 마스터 읽기 전용 |
| `util/momentum_breakout.py` | ADR·순위·선행 상승·TR 수축·저점 계산, 첫 구간 무결성 검사, 부분 매도·MA 청산 판정 |
| `util/paper_trading.py` | A/B/C 독립 현금 계좌, 정수 수량·비용·손절 위험, 원자적 가상 체결·상태 복구 |
| `util/paper_strategy_engine.py` | 같은 틱으로 A/B/C 비교; 잘못된 첫 구간 틱의 진입 보류를 웹까지 전달 |
| `util/paper_tick_recorder.py` | 선택적으로 C 후보·보유 종목의 체결 틱 CSV 기록 |
| `replay_momentum_breakout.py` | CSV 틱을 실제 C 가상 엔진에 넣는 재현 CLI; 로그인·실주문 없음 |
| `web_frontend/src/pages/MomentumPage.jsx` | 계좌 비교·후보·탈락/대기 사유·보유·부분 매도·체결 표시 |

기존 신고가의 매수·매도 연결 수정은 **기존 실계좌 동작에도 영향을 준다**. 새 C와 A/B 비교 계좌는 가상 전용이며 Manager가 실매수 요청을 차단한다.
`main.py` 전체가 가상 전용으로 바뀌는 것은 아니다. 기존 전략 실행 정책을 확인한 뒤 사용한다.

## 첫 구간 무결성 검사

기존 버전은 등록 시점이 개장 전이고 틱이 하나라도 있으면 관측 최고가로 진입할 수 있었다. 일부 체결이 빠졌을 때 실제 첫 구간 고가와 달라질 수 있었다.
새 버전은 첫 구간이 끝난 뒤 처음 관측한 체결까지 다음을 확인한다.

1. 첫 관측 체결의 `누적거래량 == abs(거래량)`을 요구한다. 시작 전 누적 수량을 설명할 수 없으면 `OPENING_START_UNVERIFIED`로 당일 진입을 보류한다.
2. 이후 `현재 누적거래량 − 직전 누적거래량 == abs(현재 체결량)`을 요구한다. 불일치하면 `OPENING_VOLUME_GAP`으로 보류한다.
3. 같은 초·같은 가격·같은 누적 수량의 중복 이벤트는 제외한다. 같은 초의 다른 체결은 누적 수량 증가로 구별한다.
4. 첫 구간 확정 전의 시간 누락·가격 오류·역순·지연도 당일 진입 보류로 남긴다. 정상 틱이 나중에 왔다고 사유를 지우지 않는다.
5. 실시간 재등록 시 관측 구간을 초기화한다. 개장 후 재등록은 당일 신규 진입을 보류한다. 다음 거래일에는 새 구간을 수집한다.

예: 직전 누적 1,000주, 현재 누적 1,300주, 현재 체결 100주라면 200주의 증가를 설명할 수 없어 진입하지 않는다.
첫 구간이 이미 확정되면 뒤의 체결량 차이로 고정한 구간 고가를 바꾸지 않는다. 저장된 보유 손절은 첫 구간·후보 필터와 독립적으로 관리한다.

**이 검사는 구현을 위한 보수적 정책이며 PDF의 매매 규칙이 아니다.** 키움의 공식 OpenAPI+ 개발가이드 v1.1, 8.2절(31페이지)은 FID20=체결시간, FID15=체결량, FID13=누적거래량을 설명한다. 하지만 모든 환경에서 두 수량의 차이가 항상 일치한다고 보장하지는 않는다. 장전 거래량·다른 거래 세션·정정·집계 차이로 정상 종목도 보류될 수 있다. 현재 PC의 KOA Studio와 실제 스트림에서 판정 사유를 확인해야 한다. 자동 복원·정정 처리나 첫 구간 분봉 조회는 이번 구현에 없다.
공식 문서: https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.1.pdf

## 실제 틱 기록

기본값은 기록 안 함이다. 매매 프로그램을 실행하는 CMD에서 다음을 설정하면 C 후보·보유 종목만 기록한다. A/B 전체 틱 재현용 자료는 아니다.

```cmd
set SYSTEMTRADING_PAPER_TICK_CSV=C:\경로\SystemTrading\ticks\breakout_20261008.csv
python main.py
```

파일에는 수신 시각·실시간 등록 시각·종목·거래소 체결시각·현재가·당일 저가·누적 수량·개별 수량이 저장된다. 등록이 완료되기 전의 이벤트는 기록하지 않는다.
기존 CSV의 헤더가 다르면 덮어쓰지 않고 기록 실패 로그를 출력한다. 기록 오류는 매매 엔진을 중지하지 않는다. 파일 증가량과 디스크 여유를 관리하고, 서로 다른 실행 프로세스가 같은 CSV에 쓰지 않도록 한다.
데이터는 버퍼를 주기적으로 비우므로 비정상 종료 때 마지막 일부 행이 빠질 수 있다. 비정상 종료로 생긴 CSV 불완전 행은 재현 도구가 오류로 보고한다.

## C 전략 틱 재현

키움 ActiveX 없이 pandas가 설치된 64bit 환경에서 실행할 수 있다. 원본 일봉 DB를 읽기 전용으로 사용하며, 결과 DB는 **새 파일**이어야 한다. 기존 파일은 덮어쓰지 않는다.

```cmd
conda activate marketdata
python replay_momentum_breakout.py --ticks ticks\breakout_20261008.csv --market-db market_history.db --paper-db replay_breakout_20261008.db > replay_breakout_20261008.json
```

CSV 헤더:

```csv
received_at,registered_at,code,trade_time,price,day_low,cumulative_volume,trade_volume
20261008090000,20261008085900,000001,090000,10000,10000,100,100
```

시각은 한국시간 `YYYYMMDDHHMMSS`, `trade_time`은 `HHMMSS`이다. 입력은 수신 순서대로 처리하며 임의로 정렬하지 않는다. 등록이 바뀌면 해당 행의 `registered_at`을 바꾼다. 잘못된 수신 순서 등 입력 오류는 결과 DB를 만들기 전에 확인한다.
로그는 stderr, JSON 결과는 stdout으로 나간다. 결과는 C의 현금·평가액·비용·손익·체결·잔량·마지막 판정 사유를 담는다. 공통 ledger가 빈 A/B 계좌도 만들지만, 이 CLI는 C만 거래하므로 **A/B/C 성과 비교용 백테스트가 아니다**.
각 재현 시점에는 해당 날짜 이전 일봉만 읽는다. 당일·미래 일봉으로 과거 후보/ATR/MA를 계산하지 않는다.

전달 ZIP의 `demo`에는 합성 일봉 DB·틱 CSV·기대 결과가 있다. 프로젝트 루트에 demo 폴더를 복사한 뒤:

```cmd
python replay_momentum_breakout.py --ticks demo\ticks_demo.csv --market-db demo\market_history_demo.db --paper-db demo\my_replay.db > demo\my_report.json
```

기대 순서: 9월 30일 돌파 매수 → 10월 7일 D+4 부분 매도 → 10월 8일 전일 확정 종가 MA 이탈 청산. 10월 5일 휴장은 기존 캘린더를 따른다. 합성 종목/가격이며 실제 실적이 아니다.

## 검증과 확인 순서

```cmd
python -m unittest discover -s tests -v
python -m compileall -q api strategy util web_api tests replay_momentum_breakout.py
cd web_frontend
npm ci
npm run build
```

이번 브랜치 자동 테스트 53개 통과. 누락·중복·동일 초의 복수 체결·재등록·지연 틱 보류, 실제 기록→재현 동일 진입, D+4 부분 청산→MA 청산, 원본 DB 바이트 보존, 기존 결과 보호, 기록 환경변수로 재현 입력 파일이 바뀌지 않는지 등을 확인한다. Python 컴파일과 React 빌드도 통과했다.

적용 후에는 합성 예제 재현 → 개장 전 등록 → 후보별 탈락/대기 사유 관측 → 비용 포함 A/B/C 결과 누적 순서로 확인한다.
일봉만으로 첫 구간/손절/진입의 발생 순서를 증명할 수 없으며, 분봉 OHLC를 가짜 틱으로 바꾸지 않는다. 재현 MDD는 매 이벤트마다, 실제 엔진 MDD는 약 5초마다 계산하므로 값이 다를 수 있다.
DB의 현재 종목 마스터로 과거 유니버스를 읽으면 생존 편향이 남는다. 과거 상장/폐지 구성과 수정주가 일관성은 별도 자료가 필요하다.
Windows 32bit Kiwoom 로그인·실제 체결 수신·실주문 및 브라우저 시각 검증은 이번 Linux 환경에서 확인하지 못했다. 테스트 통과는 수익성 검증이 아니다.

원본 전략 자료는 2021년 설명이며 2026년 국내 시장 성과는 확인하지 않았다. 20봉·TR 0.7·D+4·수익 중 1/3 부분 매도·거래비용은 실험 설정이다. 캘린더·특별 개장 시각·현재 계좌 비용은 운영 조건에 맞춰 관리한다.
최신 검색 키워드: `키움 OpenAPI 주식체결 FID 13 15 장전 누적거래량`, `KOA Studio 실시간 체결량 누적거래량`, `KRX 2026 휴장일 특별 개장 시간`, `돌파 전략 워크포워드 생존편향`.
