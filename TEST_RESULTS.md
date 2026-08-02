# 주요 기능 테스트 결과

실행 일자: 2026-08-02

## 자동 테스트

```text
.venv\Scripts\python -m unittest discover -s tests -v
```

결과: 총 22개 테스트 통과

검증 범위:

- 기존 설정 검증, 예약 계산, 프로세스 트리 종료와 모의 Telnet 회귀 테스트 7개
- JSON 설정·상태·자격증명 재시작 복원과 비밀값 없는 내보내기
- 기존 `config.json` 변환, 원본 백업과 평문 비밀번호의 DPAPI 전환
- 손상된 설정의 직전 정상 백업 복구 및 지원하지 않는 스키마의 안전한 실행 차단
- 여러 스레드의 상태 저장 후에도 유효한 JSON 유지
- JSONL 이벤트 회전, 검색, 손상 행 건너뛰기와 보존 정리
- TCP 상태 확인, 반복 장애 회로 차단과 desired-running 상태 복원
- 데이터 경로별 단일 인스턴스 잠금과 standalone 임베디드 엔진 CRUD

## 정적·의존성 검사

다음 검사가 모두 성공했습니다.

```text
.venv\Scripts\ruff check .
.venv\Scripts\python -m compileall -q .
.venv\Scripts\python -m pip check
git diff --check
```

## PyInstaller onefile 검증

`build.ps1 -SkipTests`로 `dist\PythonServiceManager.exe` 단일 파일 생성을 확인했습니다.

- 빌드 환경: Python 3.13.14, PyInstaller 6.21.0, Windows 10
- EXE 크기: 26,009,887 bytes
- 격리된 `--data-dir`로 EXE를 실제 시작하고 GUI 프로세스 유지 확인
- `config.json`, `state.json`, `credentials.json` 자동 생성 확인
- 테스트가 시작한 EXE 프로세스만 종료해 다른 실행 중인 프로그램에 영향을 주지 않음

## 별도 환경에서 확인할 항목

- 실제 SSH/Telnet 장비의 인증·인코딩·호스트 키 변경 시나리오
- Windows 10/11의 트레이 메뉴와 DPI 조합별 시각 확인
- 절전·복귀, 시각 변경·DST와 24시간 이상 연속 운전
