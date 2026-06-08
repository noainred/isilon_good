# 변경 이력 (릴리즈 노트)

이 프로젝트의 모든 주요 변경 사항을 이 파일에 기록합니다.
형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따르고,
버전은 [유의적 버전(SemVer)](https://semver.org/lang/ko/) 을 따릅니다.

버전 표기: `MAJOR.MINOR.PATCH`
- MAJOR — 호환되지 않는 변경
- MINOR — 호환되는 기능 추가
- PATCH — 호환되는 버그 수정

DB 스키마 버전은 각 DB 의 `PRAGMA user_version` 에 기록되며, 현재 스키마 버전은 **2** 입니다.

---

## [1.1.5] - 2026-06-08

### 추가됨 (Added)
- **설정을 별도 페이지로 분리** + 상단 네비게이션(대시보드 / ⚙ 설정 / 📖 버전 기록).
- **버전 기록(version history)** 페이지: 현재 버전·스키마 + CHANGELOG 표시
  (`GET /api/changelog`).
- 설정 페이지에 **DB 저장 경로(읽기 전용)**, **로그 저장 경로**, **완료/오류 메일
  알림(SMTP)** 항목 추가. 스캔 종료 시 메일 발송(smtplib, 465=SSL/587=STARTTLS),
  log_path 지정 시 스캔 시작·종료를 파일에 기록. SMTP 비밀번호는 화면에 노출하지
  않고(마스킹) 빈값 저장 시 기존 값 유지.

### 변경됨 (Changed)
- 스캔 진행 중에는 「새 스캔 시작」 카드 전체를 숨긴다.

---

## [1.1.4] - 2026-06-08

### 추가됨 (Added)
- **프로세스별 CPU·메모리 표시**: 디스크 용량 계산에 쓰이는 스캐너 프로세스와
  du 자식 프로세스의 **CPU%** 를 대시보드에 표시(메모리 RSS 와 함께).
  스레드 동시 처리로 100%를 넘을 수 있다. (스키마 2: resource_samples 에
  scanner_cpu/du_cpu 추가, 구버전 DB 자동 마이그레이션)
- **관리 개요 표에 "소요 시간" 열** 추가(루트별 최신 스캔 경과 시간).

### 변경됨 (Changed)
- 스캔 진행 중에는 「새 스캔 시작」의 입력/탐색 영역(경로·옵션·마운트 선택·폴더
  탐색)을 **숨기고** 빨간 "● 스캔 중…" 버튼만 남긴다.

---

## [1.1.3] - 2026-06-08

### 변경됨 (Changed)
- 대시보드 레이아웃: **자원 모니터링(서버 메모리 / du 프로세스 메모리 / CPU·디스크)
  카드를 화면 맨 위로** 이동.

---

## [1.1.2] - 2026-06-08

### 수정됨 (Fixed)
- **탐색 중 진행 숫자가 멈춰 보이던 문제**: 파일이 매우 많은 한 디렉터리(NFS 등)를
  훑는 동안 총 파일 수/확인 용량이 그 디렉터리를 끝낼 때까지 갱신되지 않았다.
  이제 청크(약 2000개)마다 **실시간으로** 전역 카운터와 진행 상태를 갱신한다.

### 변경됨 (Changed)
- 기본 `scan_workers` 1 → **4** (NAS/NFS 에서 파일 stat 을 동시 처리해 가속).
  설정에서 더 높일수록 빨라진다.
- 대시보드: 스캔이 진행 중이면 「이 디렉터리 스캔 시작」 버튼이 **빨간 "● 스캔 중…"**
  으로 바뀐다(진행 중 표시).

---

## [1.1.1] - 2026-06-08

폐쇄망의 Python 3.6 환경(RHEL/CentOS 7 등)에서도 동작하도록 호환성 수정.

### 수정됨 (Fixed)
- **Python 3.6 호환**: 3.7+ 전용 문법/모듈을 제거 — `from __future__ import
  annotations`, PEP 604 `X | None`(→ `Optional`), PEP 585 `dict[...]`(→ `Dict`),
  `dataclasses`(→ 일반 클래스), `http.server.ThreadingHTTPServer`(→ 3.6 폴백),
  `subprocess(text=)`(→ `universal_newlines=`), `add_subparsers(required=)`
  (→ 속성 설정). 이제 **Python 3.6 이상**에서 실행된다(vermin 확인).
- **설치 호환**: 옛 setuptools(`<61`)와 Python 3.6 에서도 `pip install` 이 되도록
  PEP 621 `[project]` 대신 classic `setup.cfg`/`setup.py` 로 전환
  (`setuptools>=61` 강제 의존 제거). `python_requires>=3.6`.

> 폐쇄망에서는 설치 없이 압축을 풀어 `python3 -m isilon_usage ...` 로 바로
> 실행할 수 있습니다(표준 라이브러리만 사용).

---

## [1.1.0] - 2026-06-07

정확도·성능·관리 기능을 대폭 보강. DB 스키마 변경 없음(스키마 버전 1 유지).

### 추가됨 (Added)
- **하드링크 중복 제거**: native 백엔드가 `(st_dev, st_ino)` 로 하드링크를 1회만
  계산해 `du` 와 용량이 일치(스냅샷/하드링크 많은 NAS 정확도 향상).
- **stat 동시 처리(`scan_workers`)**: 디렉터리 내 파일 stat 을 스레드풀로 묶어
  처리해 NFS 대용량 스캔을 가속(청크 스트리밍으로 메모리는 그대로 최소).
- **디렉터리 드릴다운 + 검색**: 대시보드에서 디렉터리를 클릭해 하위로 파고드는
  트리 탐색과 경로 검색(`/api/children`, `/api/search`).
- **용량 추세 · 스캔 비교(diff)**: 루트별 조사 용량 추이 그래프와 두 스캔의
  디렉터리별 증감 비교(`/api/diff`).
- **오류 디렉터리 목록**: 접근 불가 디렉터리 목록/사유 표시(`/api/errors`).
- **보존 정책 / 정리**: 루트별 보관 수 또는 기간으로 오래된 스캔 자동/수동 정리
  (`retention_per_root` 설정, `/api/prune`, `prune` CLI, 스캔 삭제 `/api/scan/delete`).
- **완료 알림(웹훅)**: 스캔 완료/오류 시 웹훅(Slack 등) POST(`notify_webhook`).
- **예약 스캔(스케줄러)**: 경로별 주기 반복 스캔(`schedules` 설정 + 대시보드 편집기).
- **결과 내보내기**: 디렉터리 집계를 CSV/JSON 으로 스트리밍 다운로드(`/api/export`).
- **재개(resume)**: 중단된 스캔을 이어서 진행(`/api/scan/resume`, `resume` CLI).
- **패키징**: `pyproject.toml`(콘솔 스크립트 `isilon-usage`, 동적 버전, dashboard.html
  포함) + `Dockerfile`. `pip install .[monitor]` 로 설치 가능.
- **테스트/CI 강화**: 서버 API 통합 테스트(`tests/test_server.py`), 하드링크·권한
  거부 테스트, CI 에 ruff 린트 + 다중 Python 버전 + 서버 테스트 추가.

### 수정됨 (Fixed)
- 같은 루트를 같은 초에 두 번 스캔하면 per-run DB 파일명이 충돌해 두 스캔이 같은
  DB 를 공유하던 문제 수정(파일명에 고유 토큰 추가).

---

## [1.0.0] - 2026-06-07

초대용량 NAS(아이실론 등)의 디렉터리별 사용량을 **메모리 최소로** 조사하고,
진행 상황과 서버 자원을 **웹 대시보드**로 보여주는 도구의 첫 정식 릴리즈.

### 추가됨 (Added)

**스캔 엔진 (메모리 최소화)**
- 트리를 한 번에 처리하지 않는 2단계 스캔:
  - 1단계 탐색 — `os.scandir` 로 디렉터리를 한 번만 훑어 직접 파일의 용량/개수를
    기록하고, 방문 대기 목록을 메모리가 아닌 DB(`status='pending'`)에 둠.
  - 2단계 상향식 집계 — 최하위 레벨 → 같은 레벨 → 상위 레벨 순으로 재귀 용량을
    집계(파일을 두 번 훑지 않음).
- 두 가지 측정 백엔드:
  - `native` (기본) — 파이썬이 직접 측정(가장 빠르고 저메모리).
  - `du` — 디렉터리마다 시스템 `du` 를 별도 프로세스로 실행하고 그 프로세스의
    메모리를 대시보드에 표시.
- 용량 기준 선택(`--size-mode disk|apparent`), 마운트 경계 제한(`--one-file-system`).
- 심볼릭 링크 미추적, 접근 불가 항목은 "오류 디렉터리"로 집계.

**저장/관리 (버전·이력 관리)**
- 실행마다 별도의 per-run DB(`<data-dir>/scans/scan_<시각>_<경로>.db`)를 생성하여
  과거 스캔이 덮어써지지 않음.
- 관리(매니저) DB(`<data-dir>/manager.db`)에 모든 스캔의 요약을 모아 전체 용량을 관리.
  같은 루트는 "최신 스캔"만 합산하여 중복 합산을 방지.
- 각 스캔에 **생성한 앱 버전(app_version)** 을 기록하고, DB 스키마 버전을
  `PRAGMA user_version` 으로 관리(구버전 DB 컬럼 자동 보강).

**웹 대시보드 (표준 라이브러리, 외부 CDN 없음)**
- 현재 조사 중인 디렉터리, `처리/전체 디렉터리 수`, **확인된 사용량의 전체 디스크
  대비 %**, 단계, ETA 등 진행 상황 표시.
- 서버 자원 모니터링: **시스템 메모리 사용률**, **du/스캐너 프로세스 메모리**,
  CPU·load, 파일시스템 용량, 메모리 시계열 그래프, 용량 상위 디렉터리.
- 전체 용량 관리 개요(루트별 최신 합계) + 스캔 선택기.
- **웹에서 조사할 디렉터리 지정 후 스캔 시작/중지**:
  마운트 빠른 선택, 폴더 탐색기, 백엔드/옵션 선택, 진행 중 스캔 중지.
- `--mount-base` 로 웹에서 스캔 가능한 경로를 제한.
- **웹에서 모든 설정 보기/수정(설정 카드)**: 기본 백엔드/용량기준/한파일시스템,
  커밋 배치 크기, 자원 샘플링 주기, 허용 마운트 경로(mount_bases), 상위 N,
  대시보드 새로고침 주기를 편집해 `<data-dir>/settings.json` 에 저장(재시작에도 유지).
  `--lock-settings`(읽기 전용), `--reset-settings`(CLI 값으로 덮어쓰기) 제공.

**자원 모니터링**
- `psutil` 이 있으면 사용하고, 없으면 리눅스 `/proc` 폴백으로 동작(폐쇄망 대응).

**CLI**
- `run` — 초기 경로 스캔 + 대시보드(웹에서 추가 스캔 가능).
- `serve` — 대시보드 + 웹에서 디렉터리 지정 스캔(마운트 사용 시 권장).
- `scan` — 대시보드 없이 스캔만(배치용).
- `status` — 전체 관리 개요 + 스캔 상세를 콘솔에 출력.
- `version` / `--version` — 버전·환경 정보.

**API**
- `GET /api/scans`, `GET /api/status`, `GET /api/children`,
  `GET /api/browse`, `GET /api/mounts`,
  `POST /api/scan/start`, `POST /api/scan/stop`.

**릴리즈 자동화 (CI)**
- `.github/workflows/ci.yml`: 브랜치 push/PR 마다 Python 3.8/3.11/3.13 에서
  컴파일·CLI·테스트와 대시보드 JS 문법을 검증.
- `.github/workflows/release.yml`: `v*` 태그를 push 하면 테스트를 실행한 뒤
  CHANGELOG 의 해당 버전 섹션을 릴리즈 노트로 추출해 GitHub 릴리즈를 자동 생성.

**문서/테스트**
- 사용 설명서(`docs/USER_GUIDE.md`), README, 설치 가이드.
- 정확성 테스트(`tests/test_scanner.py`) — native/du 결과가 시스템 `du` 와 일치.
- 관리 DB 통합 테스트(`tests/test_manager.py`) — per-run 분리, 전체 집계, 중복 합산 방지.

### 알려진 제약 (Notes)
- 대시보드에는 인증이 없습니다. 신뢰망에서 쓰거나 `127.0.0.1` 바인딩 + SSH 터널을
  권장하며, 웹 스캔은 `--mount-base` 로 경로를 제한하세요.
- "확인된 사용량"은 하드링크/스파스 파일/블록 정렬 등으로 디스크 사용량과 정확히
  일치하지 않을 수 있습니다.

[1.1.1]: https://github.com/noainred/isilon_good/releases/tag/v1.1.1
[1.1.0]: https://github.com/noainred/isilon_good/releases/tag/v1.1.0
[1.0.0]: https://github.com/noainred/isilon_good/releases/tag/v1.0.0
