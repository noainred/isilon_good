# Synology 패키지(.spk) — 번외 버전

The Davinci NAS Management 를 시놀로지 DSM 7.x 에 설치하는 **noarch SPK** 입니다.
이 프로그램은 순수 Python 표준 라이브러리라 컴파일·의존성이 없어, 모든 시놀로지 CPU 공용 패키지 하나면 됩니다.

## 빌드
```bash
python3 tools/build_synology_spk.py
# → download/synology/isilon_usage-<version>.spk
```
결정적 빌드입니다(같은 소스 → 같은 결과). 아이콘은 PIL 있으면 파란 라운드+N, 없으면 단색으로 폴백합니다.

## 요구사항
- **DSM 7.0 이상**
- **Python 3** — DSM 7.2 는 시스템 `python3` 가 없을 수 있습니다. 그럴 땐 **Package Center → 'Python 3.9'**
  패키지를 먼저 설치하세요. 서비스 스크립트가 여러 경로(`/usr/local/bin/python3`,
  `/var/packages/Python3.9/target/...` 등)를 자동 탐색하고, 못 찾으면 설치 로그에 안내를 남깁니다.

## 설치
1. **Package Center → 우측 상단 '수동 설치(Manual Install)'** → `.spk` 파일 업로드.
2. 미서명 패키지라 DSM 이 **신뢰 확인**을 요구합니다 → 진행(본인 소유 NAS 전제).
3. 설치가 끝나면 자동 시작하고, **포트 8765** 에서 웹 UI 가 뜹니다.
4. **Package Center 의 '열기(Open)'** 버튼 또는 브라우저에서 `http://<NAS-IP>:8765` 로 접속.

## 데이터·업그레이드
- 데이터(설정·DB)는 `/var/packages/isilon_usage/var/data` 에 저장되며 **업그레이드에도 보존**됩니다.
- 새 버전은 빌드한 `.spk` 를 다시 수동 설치하면 됩니다(설정 유지).
- 로그: `/var/packages/isilon_usage/var/isilon_usage.log`

## 권한(중요)
- NAS 공유폴더(`/volume1/...`)를 스캔하려면 읽기 권한이 필요해 **run-as root** 로 동작합니다
  (`conf/privilege`). 본인 소유 NAS 의 개인/사내 용도 전제입니다. 신뢰하지 않는 패키지에 root 를 주는 건
  일반적으로 권장되지 않으니, 소스를 확인하고 설치하세요.
- **방화벽**을 켜두셨다면 DSM 제어판 → 보안 → 방화벽에서 **8765/TCP** 를 허용하세요.

## 실기기 확인 포인트 (정직하게 — DSM 장비 없이 만든 빌드)
SPK 구조/INFO/checksum/스크립트 문법은 검증했지만, 실제 DSM 에 설치해 다음을 확인하시길 권합니다:
1. 설치·시작 후 `http://<NAS>:8765` 접속 및 '열기' 버튼 동작.
2. `python3` 미탑재 DSM 7.2 라면 'Python 3.9' 설치 후 재시작으로 서비스가 뜨는지.
3. 스캔 대상 공유폴더가 실제로 읽히는지(권한).
문제가 있으면 설치 로그(`Package Center → 세부 정보`)와 위 로그 파일을 확인해 주세요.
