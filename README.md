# Python 서비스 관리자

Windows PC에서 여러 Python GUI 서비스와 정기 Telnet 작업을 하나의 tkinter 화면에서 관리하는 경량 프로그램입니다.

## 제공 기능

- 가상환경의 `python.exe`를 직접 사용한 Python 서비스 시작
- 서비스 PID·상태·마지막 시작 시각 표시
- `stdout`·`stderr` 실시간 표시, 서비스별 최근 2,000줄 메모리 유지
- `psutil`을 이용한 부모·자식 프로세스 트리 종료
- 수동 재시작, 분 단위 주기 재시작, 매일 고정 시각 재시작
- 서비스 자동 시작과 서비스 간 2초 시작 간격
- Telnet 로그인, 명령 순차 실행, 수동·예약 실행, 중복 실행 방지
- 창 닫기 시 시스템 트레이 최소화
- 잘못된 설정 항목만 비활성화하고 프로그램은 계속 실행

## 파일 구성

```text
service_manager/
├─ main.py
├─ process_manager.py
├─ telnet_worker.py
├─ simple_telnet.py
├─ config_loader.py
├─ schedule_utils.py
├─ config.json
├─ config.example.json
├─ requirements.txt
├─ icon.ico
├─ README.md
├─ TEST_RESULTS.md
└─ tests/
   └─ test_core.py
```

## 설치

Windows PowerShell 또는 명령 프롬프트에서 실행합니다.

```powershell
cd C:\path\to\service_manager
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Python 3.10 이상을 지원합니다. Python 3.10~3.12에서는 표준 라이브러리 `telnetlib`를 사용하고, Python 3.13 이상에서는 프로젝트에 포함된 최소 Telnet 호환 모듈 `simple_telnet.py`를 사용합니다.

## 설정

`config.example.json`을 참고하여 `config.json`을 실제 경로와 서버 정보에 맞게 수정합니다. 프로그램 실행 중 설정 변경은 반영되지 않으므로 수정 후 프로그램을 다시 실행해야 합니다.

### Python 서비스

```json
{
  "name": "Test Map Service",
  "working_directory": "C:\\PythonServices\\TestMap",
  "python_executable": "C:\\PythonServices\\TestMap\\.venv\\Scripts\\python.exe",
  "script": "run.py",
  "arguments": [],
  "auto_start": true,
  "schedule_type": "daily",
  "restart_time": "04:00",
  "restart_interval_minutes": null
}
```

실제 실행 명령은 다음과 같습니다.

```text
C:\PythonServices\TestMap\.venv\Scripts\python.exe -u run.py
```

`script`는 `working_directory` 기준 경로입니다. 인수는 `arguments` 배열에 문자열로 입력합니다.

### Telnet 작업

```json
{
  "name": "Linux Data Refresh",
  "host": "192.168.0.10",
  "port": 23,
  "username": "user01",
  "password": "password",
  "login_prompt": "login:",
  "password_prompt": "Password:",
  "shell_prompt": "$",
  "commands": [
    "cd /home/user01/job",
    "./refresh_data.sh"
  ],
  "connect_timeout_seconds": 10,
  "command_timeout_seconds": 60,
  "auto_run": true,
  "schedule_type": "interval",
  "run_time": null,
  "interval_minutes": 30
}
```

프롬프트 문자열은 서버가 실제로 보내는 값과 일치해야 합니다. 셸 프롬프트를 단순히 `$` 또는 `#`로 지정하면 명령 출력에 같은 문자가 포함될 때 조기에 완료된 것으로 인식할 수 있습니다. 가능하면 서버의 고유한 프롬프트 문자열을 사용하십시오.

## 예약 규칙

지원 값은 `none`, `interval`, `daily` 세 가지입니다.

- `none`: 예약 없음
- `interval`: 지정된 분 간격
- `daily`: 매일 `HH:MM`

프로그램이 꺼져 있던 동안 누락된 작업은 소급 실행하지 않습니다. Telnet 작업이 다음 예약 시각까지 끝나지 않았으면 해당 회차는 건너뜁니다. 수동 실행은 Telnet 주기 기준점을 변경하지 않습니다.

서비스의 `interval` 재시작 기준은 실제 서비스 시작 시각입니다. 서비스가 수동 또는 자동으로 다시 시작되면 다음 재시작 시각도 새 시작 시각을 기준으로 계산됩니다.

## 실행

```powershell
python main.py
```

창의 닫기 버튼은 프로그램을 종료하지 않고 트레이로 숨깁니다. 트레이 메뉴의 `프로그램 종료`를 선택하면 실행 중인 Python 서비스를 모두 종료할지 확인한 뒤 종료합니다.

## 비밀번호 주의사항

초기 버전은 Telnet 비밀번호를 평문 JSON으로 저장합니다.

- `config.json`을 Git에 커밋하지 마십시오.
- 공용 폴더나 공유 드라이브에 두지 마십시오.
- 파일 ACL을 해당 Windows 사용자만 읽을 수 있도록 제한하는 것이 좋습니다.
- 프로그램은 비밀번호와 로그인 직후 원문 응답을 로그에 기록하지 않습니다.

## 테스트

```powershell
python -m unittest discover -s tests -v
```

테스트 항목은 다음을 포함합니다.

- 일일·주기 예약 계산과 누락 회차 비소급
- 정상·오류 설정 검증
- Python 서비스 시작, 출력 수집, 종료
- 로컬 모의 Telnet 서버 로그인, 명령 실행, 성공 상태 전환

실제 Windows 트레이 동작, 실제 사내 Telnet 서버 프롬프트, 장시간 24시간 운전은 대상 PC 환경에서 별도 확인해야 합니다.

## PyInstaller 단일 실행 파일

Windows 환경에서만 빌드합니다.

```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --noconsole `
  --name PythonServiceManager `
  --icon icon.ico `
  main.py
```

생성 파일은 `dist\PythonServiceManager.exe`입니다. `config.json`과 `icon.ico`는 EXE와 같은 폴더에 복사하십시오. 비밀번호가 포함된 `config.json`을 EXE 내부에 번들하지 않는 것을 권장합니다.

## 운영상 제한

- GUI에서 설정을 추가·수정하지 않습니다.
- 로그를 파일에 저장하지 않습니다.
- 프로세스 비정상 종료 시 자동 재시도하지 않습니다.
- Telnet 명령의 성공 여부는 출력 문구가 아니라 프롬프트 복귀 여부로만 판단합니다.
- Telnet은 암호화되지 않은 프로토콜입니다. 신뢰할 수 있는 내부망에서만 사용하십시오.
