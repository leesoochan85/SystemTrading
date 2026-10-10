# 텔레그램 알림의 로컬 경로 숨김

작성일: 2026-10-10

기준 소스: `leesoochan85/SystemTrading`의 `main`, 커밋 `68b3f2609ff5fcf869b95e520757e425012a31ca`.
사용자의 PC에만 존재하는 미반영 변경사항은 확인하지 않았습니다.

## 변경 내용

`StrategyManager` 등의 코드가 오류 문자열을 `print()`로 출력한 뒤
`util.notifier.send_message()`로 전달합니다. VS Code 화면 자체를 전송하는 방식은 아닙니다.

공통 전송 함수에서 HTTP 요청의 `text` 값만 정제합니다.

| 출력 위치 | 결과 |
| --- | --- |
| VS Code 터미널 / CMD | 원본 경로와 전체 오류 로그 유지 |
| 텔레그램 오류 추적의 `File` 항목 | 파일명, 행 번호, 함수명 유지; 상위 디렉터리 제거 |
| 텔레그램의 일반적인 파일 경로 | `[경로 숨김]`으로 대체 |
| 매매 알림, `/status` 명령, HTTP(S) 링크 | 기존 내용 유지 |

Windows 드라이브 경로, UNC 공유 경로, Linux 경로, `~/`, `./`, `../`,
`file://`, `sqlite://` 형태를 처리합니다. 따옴표로 감싼 공백 포함 경로도 처리합니다.
따옴표 없는 경로의 끝은 모호하므로 일부 형식에서는 같은 줄의 뒤쪽 문구도 함께 숨겨질 수 있습니다.

예시:

터미널:

```text
[전략 초기화 실패]
Traceback (most recent call last):
  File "C:\Users\sample\OneDrive\바탕 화면\SystemTrading\strategy\StrategyManager.py", line 791, in initialize_strategies
    strategy.check_and_get_price_data()
RuntimeError: 일봉 데이터가 부족합니다.
```

텔레그램:

```text
[전략 초기화 실패]
Traceback (most recent call last):
  File "StrategyManager.py", line 791, in initialize_strategies
    strategy.check_and_get_price_data()
RuntimeError: 일봉 데이터가 부족합니다.
```

## 적용 방법

압축파일의 `telegram_path_privacy.patch`를 프로젝트 최상위 폴더에 놓고,
그 폴더에서 다음 명령을 실행합니다.

```bat
git apply --check telegram_path_privacy.patch
git apply telegram_path_privacy.patch
py -3.9-32 -m unittest discover -s tests -p test_notifier_privacy.py -v
```

첫 번째 명령이 성공한 경우에 두 번째 명령을 실행하세요.
패치가 충돌하면 강제로 적용하지 말고 현재 `util/notifier.py`와 비교하세요.
신규 테스트 또는 문서가 이미 존재하는 경우에도 패치 점검이 실패할 수 있습니다.

파일 교체 방식으로 적용할 경우, 기존 `util/notifier.py`를 백업한 뒤
압축파일의 같은 파일로 교체할 수 있습니다. 개인적으로 수정한 내용이 있다면
파일 전체 교체보다 패치 적용을 권장합니다.

적용 후 기존 프로그램을 종료하고 다시 실행해야 새 함수가 사용됩니다.
예전 텔레그램 메시지는 변경되지 않습니다. GitHub에 자동 반영된 파일은 아닙니다.

## 검증

`tests/test_notifier_privacy.py`는 가짜 자격증명과 가짜 HTTP 모듈로 실행됩니다.
실제 텔레그램 메시지나 매매 주문을 보내지 않습니다.

- Windows / Linux 오류 추적에서 경로 제거, 파일명·행 번호·함수명 유지
- 일반 경로, 공유 경로, 공백과 한글이 포함된 경로 숨김
- 매매 알림, 명령, 비율, 웹 링크 보존
- 전송 payload만 변경되고 입력 문자열 및 터미널 원본 출력은 유지
- 텔레그램 설정 미등록 시 기존 터미널 출력 유지

전용 테스트: 7개 통과.
기존 테스트를 포함한 전체 테스트: 75개 통과(Python 3.12, Qt 및 외부 통신은 테스트용 대체 모듈 사용).
실제 사용 환경인 Windows / Python 3.9-32 / 키움 ActiveX에서의 실행은 확인하지 않았습니다.

## 범위와 한계

이 수정은 경로 노출을 줄이는 변경입니다. 계좌번호, 토큰, 사용자 이름이 일반 문장에
따로 포함된 경우까지 숨기는 기능은 아닙니다. 임의의 모든 경로 표현을 보장하지는 않습니다.
위 표에 포함되지 않는 로그 형식이 있다면 해당 형식에 대한 검증을 추가해야 합니다.
서버 환경의 실제 실행과 텔레그램 수신 화면은 검증하지 않았습니다.
여러 사용자에게 서비스할 때는 사용자별 알림 대상과 권한 분리도 별도로 구현해야 합니다.
