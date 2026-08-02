# 주요 기능 테스트 결과

실행 일자: 2026-07-29

## 자동 테스트

실행 명령:

```text
python -m unittest discover -s tests -v
```

결과:

```text
test_invalid_item_is_disabled_without_global_failure ... ok
test_missing_config_returns_error_without_exception ... ok
test_valid_config_is_enabled ... ok
test_start_log_and_stop ... ok
test_daily_schedule_uses_next_day_after_missed_time ... ok
test_interval_advance_skips_backfill ... ok
test_login_command_and_success ... ok

Ran 7 tests
OK
```

검증 범위:

- config.json 누락 시 예외로 종료되지 않고 오류 결과를 반환하는지 확인
- 잘못된 개별 설정이 전체 프로그램 설정 로드를 중단하지 않는지 확인
- 정상 서비스 및 Telnet 설정이 활성화되는지 확인
- Python 서비스 시작, PID 상태 이벤트, stdout 수집, 부모·자식 프로세스 트리 종료 확인
- 매일 고정 시각 계산 시 지난 시각을 소급 실행하지 않는지 확인
- interval 예약이 누락 회차를 반복 실행하지 않고 다음 미래 시각으로 이동하는지 확인
- 로컬 모의 Telnet 서버에서 로그인 프롬프트, 비밀번호 프롬프트, 셸 프롬프트, 명령 실행, 중복 실행 거부, 성공 상태 전환 확인

## 정적·구문 검사

```text
python -m compileall -q .
```

결과: 전체 Python 파일 컴파일 성공

## GUI 스모크 테스트

Linux 가상 디스플레이에서 tkinter 메인 창 생성, 서비스·Telnet 표 구성, 설정 오류 이벤트 처리, `after()` 이벤트 루프 진입 후 정상 종료를 확인했습니다.

## 대상 Windows PC에서 추가 확인할 항목

다음 항목은 실제 Windows 10/11 및 사내 네트워크 환경이 필요하므로 산출물 환경에서는 완전 검증하지 않았습니다.

- Windows 알림 영역 아이콘 생성·복원·메뉴 동작
- 실제 Python GUI 서비스의 자식 프로세스 트리 종료
- 실제 Telnet 서버의 프롬프트·문자 인코딩 적합성
- PyInstaller 단일 EXE 실행
- 24시간 이상 연속 운전
