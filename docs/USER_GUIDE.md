# Isilon 디렉터리 사용량 스캐너 — 사용 설명서

버전 1.7.1 기준. 이 문서는 설치부터 운영까지 전체 사용법을 다룹니다.
간단 요약은 [README](../README.md), 변경 이력은 [CHANGELOG](../CHANGELOG.md) 참고.

> **1.1~1.7 주요 추가:** 화면 분리(Summary/디렉터리/추세·비교), 예약 스캔 반복주기
> (분/시간/일/주/개월), 마운트 읽기전용 확인 토글, 병렬 처리 표시, **글로벌 통합 포탈**
> (여러 DC를 한 화면에서 — 12장), 고아 스캔 자동 정리. 자세한 건 13장 요약 참고.

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
12. [글로벌 통합 포탈 (HQ)](#12-글로벌-통합-포탈-hq)
13. [최근 추가 기능 요약 (1.1~1.7)](#13-최근-추가-기능-요약-1117)

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
- **Python 3.6 이상** (`python3 --version`) — RHEL/CentOS 7 의 기본 3.6 에서도 동작
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

### 5.6.1 디렉터리 탐색(드릴다운) · 검색 · 추세 · 비교 · 내보내기
- **드릴다운**: 디렉터리를 클릭해 하위로 파고들며 비중(%)을 확인. “🏠 루트”로 복귀.
- **검색**: 경로 일부로 디렉터리 검색 후 그 위치로 이동.
- **추세**: 선택 스캔의 루트에 대한 과거 스캔별 조사 용량 그래프.
- **비교(diff)**: 같은 루트의 두 스캔을 골라 어느 디렉터리가 늘고/줄었는지 확인.
- **내보내기**: 상세 스캔을 CSV/JSON 으로 다운로드(대용량도 스트리밍).
- **오류 디렉터리**: 접근 불가 디렉터리 목록/사유.
- **재개/삭제**: 관리 표에서 일시정지된 스캔 “재개”, 스캔 “✕” 삭제(per-run DB 포함).

### 5.7 설정 (웹에서 모든 설정 수정)
화면의 **「⚙ 설정」** 카드를 펼치면 모든 런타임 설정을 보고 수정할 수 있습니다.
저장하면 `<data-dir>/settings.json` 에 기록되어 **재시작해도 유지**됩니다.

| 설정 | 설명 |
|------|------|
| 기본 백엔드 | 새 스캔의 기본 백엔드(native/du) |
| 기본 용량 기준 | disk/apparent |
| 기본 한 파일시스템 | 새 스캔의 기본 `-x` 여부 |
| 커밋 배치 크기 | DB 커밋 묶음 크기 |
| 자원 샘플링 주기(초) | 메모리/CPU 샘플 간격 |
| 상위 디렉터리 표시 개수 | 상위 표/그래프 항목 수 |
| 대시보드 새로고침(ms) | 자동 새로고침 주기 |
| stat 동시 처리 스레드 | 파일 stat 을 동시에 처리(NFS 대용량 가속, 1=순차) |
| 루트별 보관 스캔 수 | 0=무제한. 완료 시 루트별로 최신 N개만 남기고 자동 정리 |
| 완료/오류 알림 웹훅 URL | 스캔 종료 시 POST(Slack 등). 비우면 사용 안 함 |
| 예약 스캔 | 경로별 주기(분) 반복 스캔. 추가/삭제 후 저장 |
| 허용 경로(mount_bases) | 웹에서 스캔 허용 경로(한 줄에 하나, 비우면 전체 허용) |

- **즉시 반영**: 저장하면 다음 스캔/조회부터 바로 적용됩니다(허용 경로·상위 개수
  등). 잘못된 값은 자동으로 허용 범위로 보정됩니다.
- host·port·data-dir 는 실행 시 고정이라 **읽기 전용**으로만 표시됩니다.
- 서버를 `--lock-settings` 로 띄우면 웹에서 설정 편집이 **잠깁니다**(읽기 전용).
- `--reset-settings` 로 띄우면 기존 `settings.json` 을 현재 CLI 옵션 값으로 덮어씁니다.

> 보안: 허용 경로(mount_bases)도 웹에서 바꿀 수 있으므로(신뢰망 전제), 외부에
> 노출되는 환경이라면 `--lock-settings` 로 잠그고 SSH 터널 등으로만 접근하세요.

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
| `--workers N` | `1` | 파일 stat 동시 처리 스레드(NFS 가속) |
| `--sample-interval S` | `2.0` | 자원 샘플링 주기(초) |

### `run <path>` — 초기 스캔 + 대시보드
추가 옵션: `--mount-base PATH`(반복), `--host`(기본 0.0.0.0), `--port`(기본 8765).
```bash
python3 -m isilon_usage run /mnt/isilon/ifs --mount-base /mnt/isilon --port 8765
```

### `serve` — 대시보드 + 웹 스캔 + 설정 편집(권장)
옵션: `--data-dir`, `--mount-base PATH`(반복), `--backend`, `--size-mode`,
`--one-file-system`, `--batch-size`, `--sample-interval`,
`--lock-settings`(웹 설정 잠금), `--reset-settings`(settings.json 을 CLI 값으로 덮어씀),
`--host`, `--port`.
CLI 옵션은 `settings.json` 이 없을 때 초기값으로만 쓰이고, 이후에는 웹에서 편집한
설정이 우선합니다.
```bash
python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage --mount-base /mnt/isilon
```

### `portal` — 글로벌 통합 포탈(HQ, 여러 DC 조망)
옵션: `--data-dir`(portal_nodes.json + replicas/ 상위), `--host`, `--port`(기본 8800).
노드(엣지) 추가/관리는 포탈 웹의 "노드 설정"에서 합니다. 자세한 건 12장 참고.
```bash
python3 -m isilon_usage portal --data-dir /var/lib/isilon_portal --port 8800
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

### `prune` — 오래된 스캔 정리
```bash
python3 -m isilon_usage prune --data-dir DIR --keep-per-root 5
python3 -m isilon_usage prune --data-dir DIR --older-than-days 30
```

### `resume` — 중단된 스캔 이어하기
```bash
python3 -m isilon_usage resume <scan_id> --data-dir DIR
```

### `tune` — 서버 사양 기반 권장 동시 스캔 스레드 수
```bash
python3 -m isilon_usage tune                 # native(기본)
python3 -m isilon_usage tune --backend du     # du 백엔드 기준
```
서버의 논리 CPU 수·가용 메모리를 보고 권장 스레드 수와 근거를 출력합니다. native
스캔은 NFS I/O 대기가 대부분이라 코어 수보다 많은 스레드(≈코어×4)가 유리하고,
du 백엔드는 코어 수 근처가 적당합니다. 대시보드 설정 화면의 **'서버 사양 기반 권장
계산'** 버튼으로도 같은 값을 계산해 바로 적용할 수 있습니다.

### `version` / `--version` — 버전·환경 정보
```bash
python3 -m isilon_usage version
python3 -m isilon_usage --version
```

> 패키지로 설치(`pip install .`)하면 `python3 -m isilon_usage` 대신 `isilon-usage`
> 명령을 쓸 수 있습니다.

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
| `GET /api/settings` | 현재 설정 + 서버 정보(읽기 전용 포함) |
| `POST /api/settings` | `{settings:{...}}` 로 설정 저장(잠금 시 403) |
| `GET /api/search?scan=&q=` | 경로 검색 |
| `GET /api/errors?scan=` | 접근 불가 디렉터리 목록 |
| `GET /api/diff?base=&target=` | 두 스캔 디렉터리별 증감 비교 |
| `GET /api/export?scan=&format=csv\|json` | 결과 내보내기(스트리밍) |
| `POST /api/scan/resume` | `{scan_id}` 중단 스캔 재개 |
| `POST /api/scan/delete` | `{scan_id}` 스캔 삭제(per-run DB 포함) |
| `POST /api/prune` | `{keep_per_root}` 또는 `{older_than_days}` 정리 |

---

## 7. 데이터·파일 구조

```
<data-dir>/                  (기본 ./isilon_data, --data-dir 로 변경)
├── manager.db              관리 DB — 모든 스캔 요약 + 전체 용량 집계
├── settings.json           웹에서 편집하는 런타임 설정(재시작에도 유지)
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

> **화면이 멈춘 것 같을 때**(상세가 `—`/`0`, "마지막 업데이트: —")는 별도 문서
> **[화면이 멈췄을 때 — 프로세스 확인/복구](TROUBLESHOOTING-frozen-dashboard.md)** 를 보세요.

| 증상 | 원인/해결 |
|------|-----------|
| 대시보드 상세가 `—`/`0`, "마지막 업데이트: —" | 거대 스캔에서 DB가 커져 `/api/status`가 느려진 것(보통 스캔은 살아있음). → [프로세스 확인/복구 문서](TROUBLESHOOTING-frozen-dashboard.md). v1.13.0+ 권장 |
| `python3: command not found` | `python` 시도 또는 Python 3.6+ 설치 |
| `pip install` 시 `setuptools>=61` 못 찾음(폐쇄망) | pip 설치 불필요 — 압축 풀어 `python3 -m isilon_usage` 로 실행. 굳이 설치하려면 `pip install --no-build-isolation .` |
| 브라우저 접속 안 됨 | `--host 0.0.0.0` 확인, 방화벽/포트 확인 |
| `Address already in use` | 다른 `--port` 사용 |
| 대시보드에 `/proc 폴백` 표시 | 정상(psutil 미설치). 원하면 `pip install psutil` |
| "새 스캔 시작"에서 거부됨 | `--mount-base` 밖 경로이거나 디렉터리가 아님/접근 불가 |
| 일부 디렉터리 건너뜀 | 권한 부족. "오류 디렉터리" 수로 집계. 읽기 권한 있는 계정으로 실행 |
| 확인 용량이 `du` 와 약간 다름 | 하드링크/스파스/블록 정렬 차이. `--one-file-system` 으로 경계 제한 |
| 진행 중 자원 그래프가 듬성함 | 작은 트리에서만. 대규모 스캔에선 촘촘히 기록됨 |
| 프로그램 실행 후 `df` 에 마운트가 잔뜩 생김 | **정상** — Isilon(OneFS)의 NFS 자동 서브마운트(crossmnt). 스캔이 중첩 익스포트로 들어가면 리눅스 NFS 클라이언트가 자동 마운트. 유휴 시 자동 해제됨. ⚠️ `--one-file-system`(-x)을 켜면 이 경계에서 멈춰 **대량 누락**되니 끄세요. 자세히는 아래 **10.1** |

### 10.1 프로그램을 실행하면 `df` 에 마운트가 많아져요 (Isilon crossmnt)

**프로그램이 마운트를 만드는 게 아닙니다.** 스캐너는 오직 읽기(`os.scandir`/`os.stat`)만
하고 `mount` 를 호출하지 않습니다. `du` 나 `find` 로 같은 트리를 훑어도 동일하게 생깁니다.

원인은 **Isilon(OneFS) 의 NFS 자동 서브마운트(`crossmnt`/`nohide`)** 입니다. 루트
익스포트(예: `/ifs/data/hadoop`) 아래에 **중첩 익스포트**(예: `eswr-prd/.../eswr-dfs`,
`NFS/VeeamBackup` 등)가 있으면, 스캔이 그 하위로 **들어가는 순간** 리눅스 NFS
클라이언트가 각 서브 익스포트를 **자동으로 마운트**합니다. 그래서 `df` 에 같은 클러스터
(같은 크기/사용률)가 여러 줄로 보입니다.

- **해롭지 않습니다.** 모두 같은 백엔드이고, **유휴 시 자동 해제**됩니다(기본 약 500초,
  `cat /proc/sys/fs/nfs/nfs_mountpoint_timeout`).
- ⚠️ **중요 — `--one-file-system`(-x)을 켜지 마세요.** 이 서브마운트들은 각각 **다른
  파일시스템(st_dev)** 이라, `-x` 를 켜면 스캐너가 경계에서 멈춰 **중첩 익스포트 용량을
  통째로 누락**합니다. 전체를 정확히 조사하려면 **꺼두세요(기본값 OFF)**.
- `df` 가 지저분한 게 싫다면 루트를 `nocrossmnt` 로 마운트할 수 있지만, 그러면 **중첩
  익스포트 내용이 스캔에서 빠집니다**(빈 디렉터리처럼 보임) — 권장하지 않습니다.
- 조사 대상은 **읽기전용으로 마운트**(`mount -o ro …`)하는 것을 권합니다(사고 방지).

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

---

## 12. 글로벌 통합 포탈 (HQ)

여러 데이터센터(DC)에 스캐너 서버(엣지)를 한 대씩 두고, **한국 HQ에서 1개의 포탈로
전체 스토리지 사용량을 통합 조회**합니다. 무거운 NAS 스캔은 각 DC 내부에서만
수행되고, HQ는 **요약 + 완료된 DB 스냅샷만** 받아 가볍게 동작합니다.

### 12.1 구성
```
[각 DC]  isilon_usage serve  (로컬 스토리지 스캔, /api/dbexport 노출)
                 │  HTTP(완료 per-run DB tar.gz 증분 + meta.json, 토큰)
[한국 HQ] isilon_usage portal (replicas/ 로 복제 + 글로벌 대시보드 + 경로 비교)
```

### 12.2 엣지(DC) 준비
1. 평소처럼 `serve` 로 실행하고 해당 DC의 스토리지를 스캔합니다.
2. **API 토큰**을 설정합니다(설정 → "글로벌 통합 포탈 — API 토큰"). 토큰을 설정하면
   `/api/dbexport`(복제) 호출에 `X-Auth-Token`(또는 `?token=`)이 필요합니다.

### 12.3 포탈(HQ) 실행
```bash
python3 -m isilon_usage portal --data-dir /var/lib/isilon_portal --port 8800
# 브라우저: http://<HQ주소>:8800/
```

### 12.4 노드 설정(웹)
"노드 설정"에서 각 DC를 추가합니다:
- **이름/지역/주소(URL)**: 예 `dc-tokyo` / `아시아/도쿄` / `http://10.20.1.5:8765`
- **API 토큰**: 엣지의 `api_token` 과 동일
- **경로 별칭(선택)**: 마운트 접두어가 DC마다 다를 때 `로컬 접두어 ↔ 논리 접두어`
  (예 `/mnt/isilon/data ↔ /data`)를 지정하면 같은 논리 경로로 비교됩니다.
- **복제 주기**: 분/시간/일/주/개월(예약 스캔과 같은 "시작 + 반복주기" 모델)
- **수집 방식**: 복제+폴링(권장)/복제만/폴링만 · **연결 테스트** 버튼으로 토큰·응답 확인

### 12.5 글로벌 대시보드
- 전체 사용 용량 / 스토리지 수 / 노드 온라인 / 진행 중 스캔 KPI
- **지역별 롤업**(카드 클릭 = 해당 지역 필터)
- **노드 표**: 온라인·오프라인·스캔 중 상태, 머리글 클릭 정렬, 행 클릭 시 스토리지 펼침,
  "열기 ↗" 로 해당 DC 대시보드 새 탭

### 12.6 경로 비교 (Cross-DC)
"경로 비교" 탭에서 **논리 경로**를 입력하면:
- **단일 경로 비교**: 노드별 사용 용량·파일 수 + 최대 대비% + **드리프트/누락 판정**
- **통합 디렉터리 매트릭스**: 행=디렉터리, 열=노드. 중앙값 대비 편차·누락 강조,
  디렉터리 클릭으로 더 깊이 드릴다운
- 마운트 접두어가 다른 DC는 12.4의 **경로 별칭**으로 매핑합니다.

### 12.7 보안(WAN 구간)
`api_token` 으로 복제를 인증합니다. WAN 노출 시 **TLS(리버스프록시/VPN)** 와
**HQ IP만 허용하는 방화벽**을 권장합니다.

---

## 13. 최근 추가 기능 요약 (1.1~1.7)

- **화면 구성**: 상단 네비게이션 **Summary / 디렉터리 / 추세·비교 / ⚙ 설정 / 📖 버전 기록**.
  - Summary: 자원(메모리·측정 프로세스·CPU/디스크) + 전체 용량 관리 개요 + 진행 + 요약
  - 디렉터리: 디스크 사용량 **파이**(클릭 드릴다운) + **용량 상위 디렉터리**(깊이 선택·정렬·드릴) + 드릴다운 트리
  - 추세·비교: 용량 추세 + 스캔 비교(diff) + 오류 디렉터리
- **예약 스캔 반복주기**: "시작 + 반복주기" 모델 — 분/시간/일/주(시작 요일)/개월(x월 x일부터).
- **마운트 읽기전용(ro) 확인**: 스캔 시작 시 검사·표시(설정에서 on/off).
- **병렬 처리 표시**: `⚡ Multi Scan N Thread` + 현재 동작 중 워커 수.
- **고아 스캔 정리**: 서버 재시작 시 죽은 스캔(상태가 '탐색중'으로 남은 것)을 자동 '일시정지'.
- **진행 중 안내**: 탐색 단계에서는 집계 전이라 재귀 용량 등이 임시값임을 배너로 안내.
