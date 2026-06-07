# Isilon 디렉터리 사용량 스캐너 — 사용 설명서

버전 1.0.0 기준. 이 문서는 설치부터 운영까지 전체 사용법을 다룹니다.
간단 요약은 [README](../README.md), 변경 이력은 [CHANGELOG](../CHANGELOG.md) 참고.

## 목차
1. [소개](#1-소개)
2. [설치 (처음부터)](#2-설치-처음부터)
3. [빠른 시작](#3-빠른-시작)
4. [핵심 개념](#4-핵심-개념)
5. [웹 대시보드 사용법](#5-웹-대시보드-사용법)
6. [명령행(CLI) 레퍼런스](#6-명령행cli-레퍼런스)
7. [데이터·파일 구조](#7-데이터파일-구조)
8. [버전 관리](#8-버전-관리)
9. [운영(서비스 등록·보안·백업)](#9-운영서비스-등록보안백업)
10. [문제 해결(FAQ)](#10-문제-해결faq)
11. [용어집](#11-용어집)

---

## 1. 소개

아이실론처럼 **한 디렉터리에 수천만 개의 파일**이 있는 초대용량 NAS 에서 트리
전체에 `du` 를 한 번에 돌리면 메모리 부족으로 프로세스가 죽을 수 있습니다.
이 도구는 디렉터리 단위로 쪼개 **메모리를 최소로** 쓰며 디렉터리별 파일 수·용량을
조사하고, 진행 상황과 서버 자원(특히 메모리/`du` 프로세스 메모리)을 **웹
대시보드**로 보여줍니다.

권장 사용 형태는 **아이실론의 `/ifs` 를 다른 리눅스 서버에 마운트**한 뒤, 그
서버에서 이 도구를 띄우고 **웹페이지에서 조사할 디렉터리를 골라 스캔**하는 것입니다.

특징
- 표준 라이브러리만으로 동작(웹서버·DB·자원수집 내장) — 폐쇄망 서버에 바로 설치 가능.
- 실행마다 별도 DB + 전체 관리 DB 로 이력/용량 관리.
- `psutil` 없으면 `/proc` 폴백.

---

## 2. 설치 (처음부터)

### 2.1 사전 요구사항
- **Python 3.8 이상** (`python3 --version`)
- **리눅스** (자원 수집에 `/proc` 사용)
- 조사할 NAS 가 이 서버에 **마운트**되어 있고 읽기 권한이 있을 것

### 2.2 코드 받기 — GitHub 에서 클론
```bash
cd /opt
git clone https://github.com/noainred/isilon_good.git
cd isilon_good
# 현재 개발 브랜치를 사용(기본 브랜치이므로 자동 체크아웃되지만 명시해도 됨)
git checkout claude/upbeat-bell-cXX8f
```

폐쇄망이라 git 이 안 되면, 인터넷 되는 PC 에서 받아 압축해 옮깁니다.
```bash
# 외부 PC
git clone https://github.com/noainred/isilon_good.git
cd isilon_good && git checkout claude/upbeat-bell-cXX8f
tar czf isilon_usage.tgz isilon_usage tools tests docs README.md CHANGELOG.md requirements.txt
# 대상 서버로 scp 후
tar xzf isilon_usage.tgz
```
핵심은 **`isilon_usage/` 폴더 하나**입니다(그 안에 전부 들어 있음).

### 2.3 (선택) psutil 설치
없어도 동작합니다. 설치하면 메모리/CPU 지표가 더 정확합니다.
```bash
python3 -m pip install -r requirements.txt      # 온라인
# 폐쇄망: 외부 PC 에서 python3 -m pip download psutil -d wheels 후 옮겨서
#         python3 -m pip install --no-index --find-links wheels psutil
```

### 2.4 설치 확인
```bash
python3 -m isilon_usage --version       # isilon_usage 1.0.0 (schema 1)
python3 -m isilon_usage version         # 상세 환경 정보
python3 tests/test_scanner.py           # "모든 테스트 통과 ✅"
python3 tests/test_manager.py
```

---

## 3. 빠른 시작

### 3.1 마운트한 NAS 를 웹에서 골라 스캔(권장)
```bash
# 1) 마운트 (예시)
mount -t nfs isilon:/ifs /mnt/isilon

# 2) 대시보드 실행 (웹 스캔 활성, 스캔 허용 경로를 마운트로 제한)
python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage \
        --mount-base /mnt/isilon --port 8765

# 3) 브라우저로 http://<서버주소>:8765/ 접속
#    → "새 스캔 시작" 카드에서 디렉터리를 골라 "스캔 시작"
```

### 3.2 경로를 바로 지정해 시작
```bash
python3 -m isilon_usage run /mnt/isilon/ifs/home --port 8765
```

### 3.3 데모(실제 NAS 없이 시험)
```bash
python3 tools/make_tree.py /tmp/demo --depth 5 --breadth 4 --files 30
python3 -m isilon_usage run /tmp/demo --mount-base /tmp --port 8765
```

---

## 4. 핵심 개념

### 4.1 왜 메모리를 적게 쓰는가 (2단계 스캔)
- **1단계 탐색**: 루트부터 내려가며 각 디렉터리를 `os.scandir` 로 **한 번만** 훑어,
  그 디렉터리에 직접 들어 있는 파일들의 용량 합·개수와 하위 디렉터리를 즉시 DB 에
  기록합니다. 다음에 방문할 목록도 **메모리가 아니라 DB**(`status='pending'`)에 두므로,
  메모리에는 "지금 보는 디렉터리 하나의 엔트리"만 올라옵니다.
- **2단계 상향식 집계**: 가장 깊은 레벨부터 0(루트)까지 거슬러 올라가며,
  재귀 용량 = 자기 용량 + Σ(자식 재귀 용량) 으로 집계합니다. 자식이 먼저
  계산되므로 재귀 `du` 가 필요 없고 파일을 두 번 훑지 않습니다.

> 결과적으로 트리 크기와 무관하게 메모리 사용량이 거의 일정합니다.

### 4.2 측정 백엔드
| 백엔드 | 설명 |
|--------|------|
| `native`(기본) | 파이썬이 직접 측정. 가장 빠르고 저메모리. |
| `du` | 디렉터리마다 시스템 `du -s` 를 별도 프로세스로 실행. 그 `du` 프로세스의 메모리를 대시보드에 표시. 상위에서 하위를 다시 훑어 더 느림. |

용량 기준: `disk`(실제 점유 블록, `du` 기본) / `apparent`(논리 크기).

### 4.3 단계(상태)
`discovering`(탐색) → `sizing`(집계) → `done`(완료). 중간 중지는 `paused`,
오류는 `error`.

---

## 5. 웹 대시보드 사용법

브라우저에서 `http://<서버>:<포트>/` 접속. 화면은 위에서부터:

### 5.1 새 스캔 시작 (조사할 디렉터리 지정)
- **마운트 빠른 선택**: 서버에 붙은 마운트(NFS/SMB 우선)를 버튼으로 선택 → 폴더
  탐색이 그 위치에서 열립니다.
- **폴더 탐색**: 디렉터리를 클릭하면 그 안으로 들어갑니다. "⬆ 위로"로 상위 이동,
  상단 입력칸에 경로를 직접 입력하고 "탐색"도 가능. 원하는 위치에서 "이 디렉터리
  선택 ↓"을 누르면 아래 "스캔할 디렉터리"에 채워집니다.
- **옵션**: 백엔드(native/du), 용량 기준(디스크/논리), 한 파일시스템(-x).
- **▶ 이 디렉터리 스캔 시작**: 스캔이 시작되고 관리 개요에 바로 나타납니다.

> 서버를 `--mount-base` 없이 띄우면 어떤 경로든 시작할 수 있고, 지정하면 그 경로
> 밖은 거부됩니다. 폴더 탐색기는 디렉터리 이름만 나열하며 파일 내용은 읽지 않습니다.

### 5.2 전체 용량 관리 개요
- 총 조사 용량(루트별 최신 스캔 합계), 관리 중 루트 수, 전체/진행중 스캔 수.
- 루트별 최신 스캔 표: 조사 용량, 디스크 사용, 확인%, 상태. 진행 중이면 **중지** 버튼.
- 표의 `#번호` 를 누르면 그 스캔 상세로 이동합니다.

### 5.3 상세 보기 스캔 선택기
- 등록된 스캔 중 하나를 골라 아래 상세를 봅니다. 기본은 진행중/최신 스캔을 자동 추적.

### 5.4 현재 진행 상황 / 요약
- 지금 조사 중인 디렉터리, 디렉터리 진행률(처리/전체), **확인된 사용량 / 전체 디스크
  사용량 %**, 단계, 탐색·집계 디렉터리 수, 총 파일 수, 오류 디렉터리, 경과/ETA.

### 5.5 자원 모니터링
- **서버 메모리**: 사용률(%)·사용/전체·스왑·시계열 그래프.
- **측정 프로세스 메모리(du)**: `du` 백엔드면 실행 중 `du` 프로세스 RSS,
  `native` 면 스캐너 프로세스 RSS. 스캐너 RSS·peak 포함.
- **CPU·디스크**: CPU 사용률, load, 파일시스템 전체/사용/여유.

### 5.6 용량 상위 디렉터리
- 재귀 용량(하위 포함) 기준 상위 디렉터리 표(실시간).

---

## 6. 명령행(CLI) 레퍼런스

```
python3 -m isilon_usage <명령> [옵션]
```

### 공통(스캔) 옵션 — `run`, `scan`
| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--data-dir DIR` | `isilon_data` | manager.db + scans/ 상위 폴더 |
| `--db FILE` | 자동 | 이 실행의 per-run DB 경로 직접 지정 |
| `--backend native\|du` | `native` | 측정 방식 |
| `--size-mode disk\|apparent` | `disk` | 용량 기준 |
| `--one-file-system`, `-x` | 꺼짐 | 마운트 경계 넘지 않음 |
| `--batch-size N` | `500` | DB 커밋 배치 크기 |
| `--sample-interval S` | `2.0` | 자원 샘플링 주기(초) |

### `run <path>` — 초기 스캔 + 대시보드
추가 옵션: `--mount-base PATH`(반복), `--host`(기본 0.0.0.0), `--port`(기본 8765).
```bash
python3 -m isilon_usage run /mnt/isilon/ifs --mount-base /mnt/isilon --port 8765
```

### `serve` — 대시보드 + 웹 스캔(권장)
옵션: `--data-dir`, `--mount-base PATH`(반복), `--backend`, `--size-mode`,
`--one-file-system`, `--batch-size`, `--sample-interval`, `--host`, `--port`.
```bash
python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage --mount-base /mnt/isilon
```

### `scan <path>` — 스캔만(대시보드 없이)
```bash
python3 -m isilon_usage scan /mnt/isilon/ifs/home --data-dir /var/lib/isilon_usage
```

### `status` — 콘솔 출력
옵션: `--data-dir`, `--scan ID`(특정 스캔 상세).
```bash
python3 -m isilon_usage status --data-dir /var/lib/isilon_usage
python3 -m isilon_usage status --data-dir /var/lib/isilon_usage --scan 3
```

### `version` / `--version` — 버전·환경 정보
```bash
python3 -m isilon_usage version
python3 -m isilon_usage --version
```

### HTTP API (대시보드가 사용)
| 메서드/경로 | 설명 |
|-------------|------|
| `GET /api/scans` | 전체 관리 개요 + 스캔 목록 + 실행중/허용경로/버전 |
| `GET /api/status?scan=ID` | 특정(또는 최신) 스캔 진행/자원/상위 디렉터리 |
| `GET /api/children?scan=ID&parent=PID` | 하위 디렉터리 드릴다운 |
| `GET /api/browse?path=...` | 폴더 탐색(디렉터리 이름만) |
| `GET /api/mounts` | 마운트 목록 |
| `POST /api/scan/start` | `{path,backend,size_mode,one_file_system}` 로 스캔 시작 |
| `POST /api/scan/stop` | `{scan_id}` 스캔 중지 |

---

## 7. 데이터·파일 구조

```
<data-dir>/                  (기본 ./isilon_data, --data-dir 로 변경)
├── manager.db              관리 DB — 모든 스캔 요약 + 전체 용량 집계
└── scans/
    ├── scan_20260607-010259_mnt_isilon_ifs.db   실행 #1 상세
    └── ...
```
- **per-run DB**: 그 스캔 한 번의 상세(디렉터리별 집계, 자원 시계열). 실행마다 새로 생성.
- **manager.db**: 각 스캔의 요약 한 줄(루트·상태·조사 용량·디스크 용량·앱 버전 등).

소스 구조
```
isilon_usage/  __init__(버전) db manager monitor scanner server dashboard.html cli
tools/make_tree.py   tests/   docs/USER_GUIDE.md   README.md   CHANGELOG.md
```

---

## 8. 버전 관리

- **앱 버전**: `isilon_usage/__init__.py` 의 `__version__` (현재 `1.0.0`).
  `--version`/`version` 으로 확인, 대시보드 헤더에 `v1.0.0` 으로 표시.
- **데이터 버전**: 각 스캔에 그 스캔을 만든 앱 버전(`app_version`)이 DB 에 기록됩니다.
- **스키마 버전**: 각 DB 의 `PRAGMA user_version` 에 기록(현재 `1`). 구버전 DB 는
  열 때 누락 컬럼을 자동 보강합니다.
- **릴리즈**: 변경 이력은 [CHANGELOG.md](../CHANGELOG.md), 릴리즈 태그는 `v<버전>`
  (예: `v1.0.0`).
- **자동 릴리즈**: `v*` 태그를 push 하면 GitHub Actions(`.github/workflows/release.yml`)가
  테스트를 돌린 뒤 CHANGELOG 의 해당 버전 내용을 릴리즈 노트로 자동 발행합니다.
  ```bash
  # 새 버전 낼 때
  # 1) __init__.py 의 __version__ 변경, CHANGELOG.md 에 항목 추가
  # 2) 태그 push → 릴리즈 자동 생성
  git tag -a v1.0.1 -m "isilon_usage 1.0.1"
  git push origin v1.0.1
  ```

---

## 9. 운영(서비스 등록·보안·백업)

### 9.1 systemd 서비스
`/etc/systemd/system/isilon-usage.service`:
```ini
[Unit]
Description=Isilon 디렉터리 사용량 대시보드
After=network.target remote-fs.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/isilon_good
ExecStart=/usr/bin/python3 -m isilon_usage serve \
          --data-dir /var/lib/isilon_usage --mount-base /mnt/isilon \
          --host 0.0.0.0 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
mkdir -p /var/lib/isilon_usage
systemctl daemon-reload
systemctl enable --now isilon-usage
journalctl -u isilon-usage -f
```

### 9.2 보안
- 대시보드에는 인증이 없습니다. 신뢰망에서만 쓰거나 `--host 127.0.0.1` 후 SSH 터널:
  ```bash
  ssh -L 8765:127.0.0.1:8765 user@서버
  ```
- 웹 스캔은 **`--mount-base` 로 경로를 제한**하세요.
- 마운트를 읽을 수 있는 계정으로 실행하세요(NFS/SMB 권한).

### 9.3 백업/정리
- `<data-dir>` 전체를 복사하면 모든 스캔 이력이 보존됩니다.
- 오래된 per-run DB 는 `<data-dir>/scans/` 에서 파일 단위로 삭제하면 됩니다
  (manager.db 의 해당 행은 남지만 상세는 "초기화 중"으로 표시).

---

## 10. 문제 해결(FAQ)

| 증상 | 원인/해결 |
|------|-----------|
| `python3: command not found` | `python` 시도 또는 Python 3.8+ 설치 |
| 브라우저 접속 안 됨 | `--host 0.0.0.0` 확인, 방화벽/포트 확인 |
| `Address already in use` | 다른 `--port` 사용 |
| 대시보드에 `/proc 폴백` 표시 | 정상(psutil 미설치). 원하면 `pip install psutil` |
| "새 스캔 시작"에서 거부됨 | `--mount-base` 밖 경로이거나 디렉터리가 아님/접근 불가 |
| 일부 디렉터리 건너뜀 | 권한 부족. "오류 디렉터리" 수로 집계. 읽기 권한 있는 계정으로 실행 |
| 확인 용량이 `du` 와 약간 다름 | 하드링크/스파스/블록 정렬 차이. `--one-file-system` 으로 경계 제한 |
| 진행 중 자원 그래프가 듬성함 | 작은 트리에서만. 대규모 스캔에선 촘촘히 기록됨 |

### 동작이 의심되면
```bash
ISILON_DEBUG=1 python3 -m isilon_usage serve --data-dir ... # 모니터 쓰기 오류 표시
python3 -m isilon_usage status --data-dir ...               # 콘솔에서 상태 확인
```

---

## 11. 용어집

- **per-run DB**: 스캔 한 번의 상세 데이터를 담는 개별 DB 파일.
- **관리(매니저) DB**: 모든 스캔의 요약을 모아 전체 용량을 관리하는 DB(`manager.db`).
- **own_bytes(자기 용량)**: 디렉터리에 직접 들어 있는 파일들의 용량 합(+디렉터리 inode).
- **total_bytes(재귀 용량)**: 하위 디렉터리를 모두 포함한 용량.
- **확인된 사용량 %**: 지금까지 조사한 용량 ÷ 파일시스템 사용량.
- **백엔드**: 용량 측정 방식(`native`/`du`).
- **스키마 버전**: DB 구조의 버전(`PRAGMA user_version`).
