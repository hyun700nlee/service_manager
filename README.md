# Python Service Manager

단일 Windows PC/서버에서 Python 서비스, 일반 실행 파일과 SSH/Telnet 원격 작업을 24시간 감시·예약·복구하는 로컬 서비스 관리자입니다.

## 제품 구조

- `ServiceManagerEngine`: 로그인 없이 동작하는 Windows 서비스. 프로세스, 예약, 자동복구, 원격 작업, 로그와 SQLite 설정을 소유합니다.
- `ServiceManagerGUI`: Named Pipe를 통해 엔진을 관리하는 데스크톱 GUI. 창을 닫아도 엔진과 관리 대상은 계속 실행됩니다.
- `servicemgr`: GUI와 동일한 로컬 API를 사용하는 자동화 CLI입니다.

데이터는 기본적으로 `%ProgramData%\PythonServiceManager`에 저장됩니다. SQLite 설정과 이벤트 이력, DPAPI 보호 IPC 키, 서비스별 회전 로그가 프로그램 설치 폴더와 분리됩니다.

## 주요 기능

- GUI 서비스·원격 작업 추가, 복제, 수정, 검증, 비활성화, 삭제
- 실패 시 지수 백오프 재시작, 종료 코드 제외, 반복 장애 회로 차단
- 프로세스·TCP·HTTP·사용자 명령 상태 확인
- Windows Job Object 기반 자식 프로세스 트리 격리와 정리
- 일일·주기·Cron·1회 예약, 시간대, 재부팅 후 예약 복원
- SSH 비밀번호·키·Agent 인증, 필수 호스트 키 지문, 종료 코드 판정
- 명시적 레거시 Telnet 모드, 성공·실패 패턴, 인코딩·개행 설정
- SQLite 이벤트, Windows Event Log, UTF-8 회전 로그와 SMTP/Webhook 알림
- 비밀값 DPAPI 보호, 중앙 로그 마스킹, 비밀값 없는 JSON 내보내기
- 데이터베이스 백업과 사용자가 확인해 전달하는 진단 ZIP

## 개발 환경

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

Windows 서비스 설치 전 개발 모드에서는 엔진과 GUI를 별도로 실행합니다.

```powershell
.venv\Scripts\python engine_host.py --legacy-config config.json
.venv\Scripts\python main.py
```

복구 또는 UI 개발에 한해 단일 프로세스 모드를 사용할 수 있습니다.

```powershell
.venv\Scripts\python main.py --standalone --database .data\service-manager.db --legacy-config config.json
```

## CLI

```powershell
.venv\Scripts\python service_manager_cli.py status
.venv\Scripts\python service_manager_cli.py start SERVICE_UUID
.venv\Scripts\python service_manager_cli.py events --level ERROR --limit 100
.venv\Scripts\python service_manager_cli.py export backup.json
.venv\Scripts\python service_manager_cli.py diagnostics diagnostics.zip
```

`create`, `update`, `test`, `get`, `delete`, `start`, `stop`, `restart`, `run`, `import`, `export`, `backup`, `diagnostics` 명령을 지원합니다.

## 기존 JSON 업그레이드

데이터베이스가 비어 있을 때 엔진에 `--legacy-config config.json`을 전달하면 기존 설정을 검증해 가져옵니다. 원본 옆에 `config.pre-commercial-날짜.json.bak` 백업을 만들고 Telnet 비밀번호는 즉시 DPAPI로 보호합니다. JSON 원본은 자동 삭제하지 않습니다.

## 테스트

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python -m compileall -q .
```

기존 핵심 회귀 테스트와 SQLite, DPAPI, JSON 마이그레이션, TCP 상태 확인, 장애 회로 차단, 인증 Named Pipe API 테스트를 포함합니다. 실제 서명 MSI, Windows 서비스 부팅·로그오프, 절전·DST, 실제 SSH/Telnet 장비와 7일 장기 운전은 릴리스 환경에서 별도 검증해야 합니다.

## 상용 빌드

WiX Toolset 3.x와 Windows SDK `signtool.exe`를 설치한 뒤 실행합니다.

```powershell
$env:SERVICE_MANAGER_SIGNING_PFX = "C:\secure\codesign.pfx"
$env:SERVICE_MANAGER_SIGNING_PASSWORD = "비밀번호"
.\build.ps1
```

빌드는 PyInstaller `onedir` 번들, CycloneDX SBOM과 x64 MSI를 생성합니다. 인증서가 없으면 개발용 EXE 번들은 만들 수 있지만 `build.ps1`이 경고하며 상용 배포본으로 간주하지 않습니다.

서비스 스크립트를 직접 설치할 경우 관리자 PowerShell에서 다음을 사용할 수 있습니다.

```powershell
ServiceManagerEngine.exe install
ServiceManagerEngine.exe start
```

직접 설치 경로는 지연 자동 시작과 5초·30초·60초 복구 작업을 설정합니다. MSI의 기본 복구 지연은 WiX 제약상 5초로 통일되므로 설치 후 조직 정책에서 단계별 지연이 필수이면 `sc.exe failure` 정책을 적용하십시오.

## 배포 전 필수 확인

- EXE와 MSI Authenticode 서명 및 타임스탬프 검증
- 공급자 정보와 관할법에 맞춘 `EULA.txt`, `PRIVACY.md`, 지원 연락처 법률 검토
- `release\sbom.json`과 제3자 라이선스 전문 포함
- Windows 10/11, Server 2019/2022/2025 설치·업그레이드·롤백 검증
- 7일 연속 운전과 1,000회 장애·복구 시험

보안 설계와 취약점 처리 원칙은 [SECURITY.md](SECURITY.md), 개인정보·진단정보 동작은 [PRIVACY.md](PRIVACY.md)를 참조하십시오.
