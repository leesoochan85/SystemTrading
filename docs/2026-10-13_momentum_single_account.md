# 2026-10-13 모멘텀 가상계좌 단일화

기준 소스: GitHub `leesoochan85/SystemTrading` main,
커밋 `9d8699585da46ed3e12302c983b89097dc5298ed`.

## 변경 목적

A는 기존 신고가 돌파, B는 신고가 돌파에 모멘텀 후보 필터를 추가한 비교계좌였다.
모멘텀 수축 돌파 운영과 상태 확인에 집중하기 위해 기존 C에 해당하는
`MomentumContractionStrategy` 가상계좌 하나만 운영한다.
이는 관리 구조를 단순화하는 변경이며 투자 수익 개선을 입증하는 변경은 아니다.

## 동작 변화

| 항목 | 변경 후 |
|---|---|
| 신규 DB | 모멘텀 가상계좌 하나만 생성 |
| 기존 DB | C의 현금·보유·손절·부분매도·체결·수익률 이력 유지 |
| 기존 A/B | DB 행 보존, 실행 캐시·매매·평가 갱신·API 조회에서 제외 |
| 실시간 등록 | 현재 운영 전략 후보와 활성 가상/실제 보유만 포함; A/B 전용 후보·보유 제외 |
| 화면 | 단일 모멘텀 성과 그래프·평가액·현금·MDD·후보·보유·체결 |
| A/B MA 설정 | 전용 UI와 GET/PUT API 제거; 과거 설정 DB는 보존 |
| 모멘텀 MA | 기존 `config/momentum_contraction.json`의 `strategy.trailing_ma_period` 사용 |
| 실제 매매 | 기존 실주문 어댑터와 환경변수 활성화 방식 유지 |

A/B 잔고를 C에 합산하거나 가상 청산하지 않는다. 과거 수익률을 오염시키지 않도록
기존 C의 식별자와 계좌 설정 검증 정책을 유지한다. 같은 DB와 설정으로 재시작하면
초기자금을 새로 지급하지 않고 기존 상태를 복원한다.

## 주요 코드

| 파일 | 변경 |
|---|---|
| `util/paper_trading.py` | 활성 계좌를 모멘텀 하나로 제한; 계좌·보유·당일 매수 기록을 해당 전략만 로드; 비활성 계좌 매수 차단 |
| `util/paper_strategy_engine.py` | `MomentumPaperEngine`으로 변경; A/B 초기화·후보·매수·청산 처리 제거 |
| `util/virtual_strategy_engine.py` | 새 단일 계좌 엔진 연결 |
| `web_api/database.py` | 계좌·보유·체결·성과·이력을 모멘텀으로 한정 |
| `web_api/main.py`, `web_api/strategies.py` | `/paper-account` 제공; A/B MA API와 상세 설정 필드 제거 |
| `web_frontend/src/pages/MomentumPage.jsx` | 비교표·A/B 그래프·C 명칭 제거; 단일 계좌 화면과 단일 조회 호출 |
| `web_frontend/src/api/tradingApi.js` | 새 계좌 조회 API 사용; 폐지된 MA 설정 호출 제거 |
| `web_frontend/src/components/MaSettingsPanel.jsx` | 미사용 A/B 설정 컴포넌트 삭제 |
| `web_frontend/src/components/StrategyCard.jsx`, `web_frontend/src/styles.css` | 비교 문구·미사용 설정 스타일 정리 |
| `strategy/StrategyManager.py`, 두 모멘텀 전략 파일 | 현재 구성에 맞게 설명 수정 |
| `replay_momentum_breakout.py`, `demo/expected_report.json` | 단일 엔진 사용, 리포트 scope를 `MOMENTUM_ONLY_TICK_REPLAY`로 변경 |
| `tests/` | 단일 계좌 기대값 반영 및 기존 DB 보존·상태 복원 회귀검증 추가 |
| `README.md` | 계좌 구성·DB·실행·API·문제 해결 설명 갱신 |

새 API: `GET /api/strategies/MomentumContractionStrategy/paper-account`.
기존 `/paper-comparison`은 호환용으로 같은 단일 계좌 결과를 반환한다.
과거 A/B 유틸리티는 테스트와 기록 호환을 위해 남아 있으나 운영 엔진에서 호출하지 않는다.

## 적용

1. 매매 프로그램과 웹 API를 종료한다.
2. 프로젝트 루트에서 패치 검사를 진행하고 적용한다.

```cmd
git apply --check momentum_single_account.patch
git apply momentum_single_account.patch
```

3. 사용하던 `SYSTEMTRADING_PAPER_DB` 경로와 DB를 유지한다.
4. `run_tests.bat` 또는 아래 명령으로 검증한다.

```cmd
python -m unittest discover -s tests -v
cd web_frontend
npm run build
npm test
```

5. 기존 실행 BAT로 매매 프로그램·웹 API·프론트엔드를 다시 시작하고 브라우저를 새로고침한다.

`config/momentum_contraction.json`과 계좌 비용 설정은 이 변경에서 수정하지 않는다.
DB 삭제·초기화·별도 변환 작업은 필요하지 않다.

## 검증 결과와 한계

- Python unittest 164개 통과.
- 프론트엔드 프로덕션 빌드 성공, 폴링 테스트 4개 통과.
- 기존 C의 부분매도·손절·잔고·당일 재진입 제한 유지 확인.
- 기존 A/B 계좌·보유·체결·평가 행이 모멘텀 갱신 뒤에도 동일함을 확인.
- 신규 DB의 단일 계좌 생성, 기존 API 호환과 A/B MA API 404 확인.
- Kiwoom ActiveX 실접속 및 실제 장중 체결은 이 환경에서 검증하지 않았다.
- 사용자 PC의 미푸시 수정은 포함하지 않으며 패치 검사 실패 시 강제 적용하지 않는다.
