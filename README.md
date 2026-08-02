# Python Service Manager

단일 Windows PC에서 Python 프로그램과 일반 실행 파일, SSH/Telnet 원격 작업을 GUI로 관리하는 standalone 애플리케이션입니다. GUI와 관리 엔진은 하나의 프로세스에서 동작하며 별도 Windows 서비스나 데이터베이스가 필요하지 않습니다.

## 주요 기능

- GUI에서 서비스와 원격 작업 추가·복제·수정·비활성화·삭제
- 실패 시 지수 백오프 재시작, 종료 코드 예외, 반복 장애 회로 차단
- 프로세스·TCP·HTTP·사용자 명령 상태 확인과 의존 서비스 대기
- interval·daily·cron·once 예약과 시간대·중복 실행 정책
- SSH 비밀번호·키·Agent 인증과 필수 호스트 키 지문 확인
- 보안 위험을 명시적으로 확인한 작업에 한정한 레거시 Telnet
- Windows DPAPI 자격증명 보호와 로그 비밀값 마스킹
- JSON 설정·상태 저장, JSONL 이벤트 이력, 서비스별 회전 로그
- 트레이 최소화, JSON 가져오기·내보내기, 백업과 진단 ZIP

## 실행 방식

개발 환경은 Python 3.10 이상을 권장합니다.

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

기본 데이터 위치는 `%LOCALAPPDATA%\PythonServiceManager`입니다. 테스트나 휴대용 설정이 필요하면 별도 데이터 폴더를 지정할 수 있습니다.

```powershell
.venv\Scripts\python main.py --data-dir .data
```

공식 실행 옵션은 다음 하나입니다.

```text
PythonServiceManager.exe [--data-dir PATH]
```

창의 닫기 버튼을 누르면 애플리케이션은 시스템 트레이로 이동하며 감시·예약·자동복구를 계속합니다. 트레이 메뉴 또는 설정 화면의 **프로그램 종료**를 선택하면 관리 중인 프로세스를 정상 종료한 뒤 애플리케이션이 끝납니다. standalone 버전이므로 프로그램을 완전히 종료하거나 Windows에서 로그아웃하면 감시·예약·자동복구도 중단됩니다.

## JSON 데이터

데이터 폴더에는 다음 파일과 폴더가 생성됩니다.

| 경로 | 내용 |
|---|---|
| `config.json` | 서비스, 원격 작업, 알림 설정과 `schema_version` |
| `state.json` | 실행 의도, 장애 횟수, 회로 차단, 다음 예약 |
| `credentials.json` | UUID와 Windows DPAPI 암호문 |
| `events.jsonl` | 현재 이벤트 이력 |
| `events\events-YYYYMMDD-N.jsonl` | 날짜·크기 기준 회전 이벤트 |
| `logs\<UUID>\` | 서비스 및 원격 작업별 UTF-8 회전 로그 |
| `backups\` | GUI에서 생성한 JSON 데이터 ZIP 백업 |

설정과 상태는 임시 파일 작성 후 원자적으로 교체되며 직전 정상 파일은 `.bak`으로 보존됩니다. 시작 시 현재 JSON이 손상되었지만 정상 백업이 있으면 손상본을 `.corrupt-날짜`로 보존하고 백업에서 복구한 뒤 GUI에 알립니다. 정상 백업도 없으면 데이터를 덮어쓰지 않고 실행을 중단합니다.

기존 초기 버전의 `config.json`을 기본 데이터 폴더에 두고 처음 실행하면 신규 스키마로 변환합니다. 원본은 `config.pre-json-날짜.json.bak`으로 보존되며 기존 평문 Telnet 비밀번호는 즉시 DPAPI로 보호됩니다. SQLite 파일은 읽거나 변경하지 않습니다.

JSON 내보내기에는 설정만 포함되며 비밀번호, DPAPI 암호문, 내부 실행 상태와 검증용 임시 필드는 포함되지 않습니다.

## 단일 실행파일 빌드

Windows SDK, WiX, 코드 서명 인증서는 개발용 실행파일 생성에 필요하지 않습니다. 개발 의존성을 설치한 뒤 빌드 스크립트를 실행합니다.

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

테스트를 이미 수행했다면 다음과 같이 생략할 수 있습니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -SkipTests
```

결과물은 `dist\PythonServiceManager.exe` 한 개입니다. 관리자 권한을 요청하지 않으며 설정과 로그를 실행파일 옆이 아닌 `%LOCALAPPDATA%`에 저장합니다.

PyInstaller를 직접 호출하려면 다음 명령을 사용합니다.

```powershell
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name PythonServiceManager --icon icon.ico --add-data "icon.ico;." main.py
```

프로젝트의 `ServiceManager.spec`은 `tzdata`, Paramiko와 트레이 백엔드까지 포함하므로 공식 빌드에는 `build.ps1` 사용을 권장합니다.

## 품질 확인

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\ruff check .
.venv\Scripts\python -m compileall -q .
.venv\Scripts\python -m pip check
```

자동 테스트는 JSON CRUD·복구·동시 저장, DPAPI 비밀값 보호, JSONL 회전·손상 행 처리, 자동복구 회로 차단, 상태 복원, 예약, 단일 인스턴스와 임베디드 엔진 API를 포함합니다.

보안 문제 신고 절차는 [SECURITY.md](SECURITY.md), 개인정보·진단정보 동작은 [PRIVACY.md](PRIVACY.md)를 참고하십시오.
