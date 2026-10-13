# 우량주 재무 데이터 자동 수집 및 신규매수 데이터 검증

## 해결하는 문제

기존 main.py는 value_fundamental_snapshot 테이블을 생성/조회하지만 외부 재무 데이터를 입력하지 않는다.
기존 입력 도구는 사용자가 준비한 CSV만 읽는다. 이번 수정은 64bit 별도 수집기를 추가한다.
수집 후 value_quality.db를 읽는 기존 우량주 전략을 그대로 사용한다. 필터 값과 매도 수익률 규칙은 변경하지 않는다.

## 적용 순서

실행 중인 매매 프로그램을 정상 종료한다. ZIP의 value_fundamentals_fix.patch를 프로젝트 루트에 복사한다.

```cmd
git apply --check --ignore-space-change .\value_fundamentals_fix.patch
git apply --ignore-space-change .\value_fundamentals_fix.patch
py -3.9-32 -m unittest discover -s tests -p "test_value_fundamentals.py" -v
```

--check가 실패하면 다음 명령을 실행하지 않는다. 기존 Telegram/일봉/계좌 복구 패치를 다시 적용할 필요는 없다.
검토용 files/ 파일 전체를 덮어쓰는 대신 변경분 패치를 우선한다.

## OpenDART 인증키 설정

인증키 신청: https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do
발급받은 40자리 인증키를 현재 CMD에서 설정한다. 실제 키는 대화나 Git에 올리지 않는다.

```cmd
set "DART_API_KEY=발급받은40자리인증키"
```

PowerShell에서는 위 set 대신 다음을 사용한다.

```powershell
$env:DART_API_KEY = "발급받은40자리인증키"
```

또는 프로젝트의 .env에 DART_API_KEY 항목을 설정할 수 있다. 명령줄에 --api-key 옵션은 제공하지 않는다.
환경변수가 없거나 형식이 잘못되면 외부 수집 및 DB 입력 전에 중단한다.

## 의존성 및 첫 수집

```cmd
conda run -n marketdata python -m pip install -r requirements-value-data.txt
conda run -n marketdata python collect_value_fundamentals.py --limit 20
```

수집은 64bit marketdata 환경이다. --limit 20은 처음 확인할 기업 수를 제한한다. 기본값도 20이다.
코드로 지정할 수도 있다: --codes 000020,000100 (실제 통과 여부는 재무 데이터 및 주식 종류에 따라 달라진다).
종목 전체는 다음 명령이다.

```cmd
conda run -n marketdata python collect_value_fundamentals.py --limit 0
```

전체 수집에는 다수 API 요청이 필요하다. 기본 요청 간격은 0.3초이며 성공 응답을 24시간 캐시한다.
인증/네트워크/호출 제한 오류가 발생하면 기업별 반복 호출을 중단하고 종료 결과에 이유를 기록한다.
실제 API 요청 한도는 OpenDART 계정 조건에 따른다. pykrx는 스크래핑 기반이며 외부 서비스 변경으로 실패할 수 있다.

## 결과 확인

```cmd
py -3.9-32 collect_value_fundamentals.py --stats
```

total=저장 지표 행 수, validated=검증 플래그 수, fresh=기준일 유효 데이터 수,
qualified=현재 전략의 네 필터를 모두 통과한 종목 수이다. total이 0보다 커도 qualified가 0일 수 있다.
필터를 낮추거나 데이터를 임의로 채우지 않는다.

기업별 결과 파일은 data/value_reports/collection_YYYYMMDD_HHMMSS.json이다.
DB의 value_collection_status에도 마지막 수집/검증 사유를 저장한다. 제외/실패 기업은 이력 대신 가짜 수치를 저장하지 않는다.
기존 지표가 있던 기업의 재수집이 실패하면 이전 값은 남기고 validated=0으로 신규매수에서 제외한다.

수집 완료 후 main.py를 재시작한다. 기존 실행 중 메모리에 있던 후보에 DB 갱신을 즉시 전파하는 기능은 이번 범위에 포함하지 않았다.
조회일이 7일을 넘기면 신규매수 후보에서 제외되므로 이 수집 명령을 정기적으로 실행해야 한다.
이번 패치는 예약 작업을 생성하지 않는다.

## 계산 기준

소스는 pykrx의 보통주 시가총액/상장주식수와 OpenDART 공시 재무제표/기업개황/주식총수이다.
재무제표는 연결(CFS)을 우선하고 해당 연간 연결 자료가 없는 경우 별도(OFS)를 사용한다.
TTM 계산에 사용하는 연간/당기 누적/전년 동기 누적은 모두 같은 CFS/OFS 기준을 사용한다.

- PER(TTM): 보통주 시가총액 ÷ TTM 이익. 연결은 지배기업 소유주 귀속 순이익, 별도는 순이익이다.
- TTM 이익: 직전 연간 이익 + 당기 누적 이익 - 전년 동기 누적 이익. 연간 자료가 최신이면 그 연간 이익을 사용한다.
- 반기/3분기 손익은 thstrm_add_amount를 사용한다. thstrm_amount의 3개월 값을 누적값으로 대체하지 않는다.
- 1분기는 누적값이 누락돼도 3개월 값과 누적 기간이 같으므로 thstrm_amount 사용을 허용한다.
- PBR: 보통주 시가총액 ÷ 최신 자본. 연결은 지배기업 소유주 귀속 자본이다.
- 매출총이익률: 최근 연간 매출총이익 ÷ 연간 매출액 × 100.
- 총자산회전율: 최근 연간 매출액 ÷ (연초 총자산 + 연말 총자산)/2.
- 매출총이익 계정이 없는 경우, 명시적인 매출액·매출원가 계정이 있을 때만 차액으로 계산한다.

지표를 제공하는 다른 사이트와 산식, 재무제표 범위, 자기주식 취급 등이 달라 값이 다를 수 있다.
source_note에 재무제표 기준, 보고서 코드, 접수번호, 산정 방식을 기록한다.

## 지원 범위 및 검증 조건

이번 버전은 12월 결산, 원화(KRW), 보통주 단일 종류 기업을 대상으로 한다.
보통주 시가총액을 기업 전체 이익/자본과 잘못 비교하지 않도록 우선주 등 다른 종류의 발행 주식이 있는 기업은 제외한다.
TTM에 사용한 보고서별 주식총수 자료를 확인하고, 최신 보고서 주식 수와 KRX 상장주식 수가 다르면 제외한다.
원화 미확인, 비12월 결산, 비표준/모호한 계정, 필수 계정 누락, 비교기간 누락, 적자/비양수 분모는 제외 사유를 남긴다.
공시가 가격 기준일 이후에 제출된 경우도 제외한다. 실제 정보가 제공되지 않는 기업까지 채워지는 것은 아니다.
정정공시 및 특수 회계처리는 사람의 재무제표 확인이 필요할 수 있으며 이 검증은 회계감사가 아니다.

가격 기준일 7일, 최신 재무기간 말 240일, 연간 기간 말 550일 이내인 데이터만 신규매수에 사용한다.
기간 날짜가 미래이거나 순서가 맞지 않으면 제외한다. 기존 전략 포지션의 매도 관리 규칙은 그대로다.

## 기존 DB/CSV 호환성

value_fundamental_snapshot에 validated, financial_period_end, annual_period_end, validation_note 컬럼을 추가한다.
기존 행/포지션 상태/리포트 기록은 삭제하지 않는다. 기존 행의 validated 기본값은 0이다.
수집기가 검증한 데이터를 재입력하면 기존 종목 행이 갱신된다.

기존 CSV 입력 도구도 유지한다. 검증된 CSV 입력에는 기존 컬럼 외에 validated=1,
financial_period_end, annual_period_end, 출처가 필요하다. 기간/유효값 검증을 통과하지 않으면 검증된 입력을 거부한다.
검증 메타데이터 없는 CSV는 저장하되 신규매수 후보에서는 제외한다. 임의 수치는 넣지 않는다.

## 검증

기존 108개 + 신규 21개, 총 129개 테스트 통과. Python 3.9 문법 확인.
신규 테스트는 TTM 누적값, 연간 평균자산, 연결 귀속이익, 주식 종류, 기간 불일치,
NULL/NaN/모호 계정, 구 스키마 보존, 만료 후보 차단, 재수집 실패 제외, 캐시 및 키 비노출을 검증한다.
테스트는 합성 재무제표/임시 DB/모의 HTTP를 사용하며 실제 OpenDART 인증 요청과 실거래는 실행하지 않았다.
실제 사용자 DB 입력은 인증키 설정 후 사용자 PC에서 수행해야 한다.

## 공식 자료

- OpenDART 전체 재무제표: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020
- 기업개황: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019002
- 주식의 총수: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020002
- 기업 고유번호: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018
- pykrx: https://github.com/sharebook-kr/pykrx

2026-10-13 확인한 문서/프로젝트 코드 기준. 외부 제공 형식 및 데이터 가용성은 실행 시 다시 확인한다.
