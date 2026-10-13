# Telegram 명령 수신 통신 오류 수정 — 2026-10-12

## 문제와 변경

기존 StrategyManager는 10초 QTimer 콜백에서 requests.get(timeout=1)을 직접 실행했다.
이 요청이 지연되면 Qt 메인 이벤트 루프도 기다리고, 예외 원문 출력에는 bot 토큰 URL이 포함될 수 있었다.

- util/telegram_poller.py: 단일 ThreadPoolExecutor로 HTTP 수신만 분리한다.
- 작업이 진행 중이면 추가 수신 요청을 생성하지 않는다.
- StrategyManager의 250ms QTimer는 Future 완료 여부만 확인한다. 네트워크 조회는 정상 완료 후 10초 간격이다.
- 기존 채팅방 ID 필터, /status, /help, update_id + 1 오프셋 갱신은 메인 스레드에서 유지한다.
- HTTP 연결/읽기 제한은 각각 3초/5초다. 전체 요청 시간의 엄격한 상한을 의미하지는 않는다.
- 연속 실패 후 재조회 간격: 10 → 20 → 40 → 최대 60초. 정상 수신 시 10초로 복원한다.
- 수신 오류 출력은 60초 간격으로 제한하고 억제 횟수를 기록한다. 정상 수신 복구는 한 번 출력한다.
- 수신/발신 통신 예외는 원문 URL 대신 예외 종류를 출력한다. 발신 API 실패는 HTTP 상태만 출력한다.
- 발신 메시지에 포함된 api.telegram.org/bot 토큰 URL은 마스킹한다.
- 앱 종료 시 작업 스레드에 신규 작업을 넣지 않고 executor를 종료한다. 이미 실행 중인 HTTP 요청은 즉시 강제 종료하지 않는다.

## 검증

외부 통신과 실제 주문 없이 기존 알림 개인정보/오류 제한 테스트 17개, 신규 수신 테스트 10개, 총 27개 통과.
실제 작업 스레드의 ID 분리, 요청 중복 방지, 완료 결과 수령, 오프셋, 재시도 경계, 반복 출력 제한, 복구,
타 채팅방 차단, /status 처리, API 실패 구분, 토큰 마스킹을 확인했다.
수정 Python 파일은 Python 3.9 문법 파싱을 통과했다.

Windows Python 3.9-32 + PyQt5 + Kiwoom ActiveX 실행과 Telegram 실제 응답 검증은 사용자 환경에서 필요하다.
프로젝트 전체 테스트는 이 환경에서 실행하지 않았다. 실제 인터넷 지연이나 접속 차단 자체를 해결하는 패치는 아니다.
send_message()의 기존 동기 발신 방식은 유지되므로 /status 답장 등 발신 요청의 대기는 별도로 남는다.

## 적용

적용 전 실행 중인 매매 프로그램을 정상 종료하고 현재 변경 사항을 보관한다.
ZIP의 telegram_receive_fix_v2.patch를 프로젝트 루트에 복사한 후 실행:

```powershell
git apply --check --ignore-space-change .\telegram_receive_fix_v2.patch
git apply --ignore-space-change .\telegram_receive_fix_v2.patch
py -3.9-32 -m unittest discover -s tests -p "test_notifier*.py" -v
py -3.9-32 -m unittest discover -s tests -p "test_telegram_poller.py" -v
git diff --stat
```

--check가 실패하면 두 번째 명령은 실행하지 않는다. 로컬 코드와 패치 기준 코드 차이를 확인해 병합한다.
ZIP의 files/는 검토 및 수동 병합용이며 수정 파일을 통째로 덮어쓰면 로컬 변경을 잃을 수 있다.

노출된 기존 bot 토큰은 BotFather에서 폐기/재발급하고 설정을 변경한 뒤 프로그램을 재시작한다.
Telegram에서 /status와 /help를 보내 수신/응답을 확인한다.

오류 예시:

```text
[Telegram receive error] ReadTimeout; retry in 10s; suppressed=0
[Telegram receive recovered] failures=3; suppressed=2
```

기준: 2026-10-12 GitHub main에서 읽은 util/notifier.py 및 strategy/StrategyManager.py.
저장소에 직접 커밋하거나 사용자의 Windows 파일을 직접 변경한 것은 아니다.

## v2 적용 호환성 수정

초기 패치 생성 중 기준 파일 끝에 빈 줄이 추가되어 마지막 변경 구간이 일반적인 파일에 적용되지 않는 문제를 수정했다.
--ignore-space-change는 줄바꿈/문맥 공백 차이를,는 끝 줄바꿈 차이를 허용한다.
코드 내용 차이까지 강제로 무시하는 옵션은 아니다. LF, CRLF, 끝 줄바꿈 없음, 추가 빈 줄 네 경우에 실제 적용 및 Python 문법 검증을 완료했다.
