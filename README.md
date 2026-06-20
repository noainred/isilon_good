# Isilon 디렉터리 사용량 스캐너

**[🚀 처음 시작하기](docs/GETTING_STARTED.md)** · [📦 설치 매뉴얼](docs/INSTALL.md) · [📄 솔루션 제품소개(A4)](docs/SOLUTION_BRIEF.html) · [📣 영업 자료](sales/) · [사용 설명서](docs/USER_GUIDE.md) · [변경 이력](CHANGELOG.md) · [보안 가이드](SECURITY.md) · [성능·최적화](docs/PERFORMANCE.md) · [서비스 실행(systemd)](docs/SERVICE.md) · [화면이 멈췄을 때 — 프로세스 확인/복구](docs/TROUBLESHOOTING-frozen-dashboard.md) · Python 3.6+ · [라이선스: 독점 · All Rights Reserved](LICENSE)

> ⚠️ **배포 전 필수 전제(보안).** 이 도구는 **신뢰망에서 운영자가 직접 운영**하는 내부 관리 도구입니다.
> 기본은 **평문 HTTP·다중 사용자 인증 없음**이므로, 대외/운영 배포 시 **반드시 ① 신뢰망 한정(인터넷
> 비노출), ② TLS 리버스 프록시 뒤에 배치, ③ 작업 보호 비밀번호 설정, ④ `--mount-base`/`--lock-settings`
> 적용**이 필요합니다. 자세한 내용은 **[보안 가이드(SECURITY.md)](SECURITY.md)** 를 먼저 읽으세요.

아이실론(Isilon)처럼 **한 디렉터리에 수천만 개의 파일**이 있는 초대용량 NAS
에서, 트리 전체에 `du` 를 한 번에 돌리면 메모리를 너무 많이 써서 프로세스가
죽는 경우가 있습니다. 이 도구는 그 문제를 피하기 위해 **디렉터리 단위로 쪼개서
메모리를 최소로 쓰며** 디렉터리별 파일 수·용량을 조사하고, **웹 대시보드**로
진행 상황과 서버 자원(특히 메모리/`du` 프로세스 메모리)을 실시간으로 보여줍니다.

표준 라이브러리만으로 동작하므로(웹서버·DB·자원수집 모두 내장), 패키지 설치가
제한된 폐쇄망 서버에도 그대로 올려서 쓸 수 있습니다. (Python 3.6+)

### 기능 한눈에

- **메모리 최소 스캔** — 2단계(탐색→상향식 집계) `native`/`du` 백엔드, **하드링크
  중복 제거**(du와 일치), **stat 동시 처리**(N 스레드, NFS 가속), disk/apparent 기준.
- **초대용량(수십억 파일) 안전장치** — **깊이 접기**(DB 크기 묶고 합계는 정확),
  최대 깊이, **DB 크기 가드**·**디스크 여유 자동 일시정지**, 주기적 WAL 체크포인트.
- **웹 대시보드** — 웹에서 디렉터리 지정 **스캔 시작/중지/재개**, 드릴다운 트리+검색,
  디스크 파이차트, **DB 생존 지표·하트비트(마지막 체크+카운트다운)·로그/DB 디스크
  여유**, 워커별 현재 디렉터리, 재개 누적/세션 시간, 자원(메모리/CPU/du) 모니터링.
- **📊 분석 리포트** — 파일 **나이(콜드 데이터)·소유자·확장자별** 사용량,
  **최대 파일 Top**, **용량 소진 예측**(추세→90%/가득 참 예상일), **변화 Top**(직전 대비).
- **운영** — 실행별 DB + 관리 DB, 보존 정책, **예약 스캔(시작+반복주기)**,
  완료/오류 **웹훅·메일** 알림, **CSV/JSON 내보내기**, 스캔 **비교(diff)**, 모든 설정 웹 편집.
- **통합·모니터링** — **글로벌 통합 포탈(HQ)**: 여러 DC를 DB 복제로 한 화면 조망 +
  Cross-DC 경로 비교 · **스토리지 어레이 상태**(Isilon/PowerStore/Unity/PowerMax/VMAX/
  XtremIO/VPLEX) · **Prometheus `/metrics`**.
- **엔진·🔬 오토튜닝** — **2단 병렬 엔진(pscan)**: 프로세스(GIL 우회)×스레드(NFS 왕복 지연 은닉)
  + 적응형 깊이 분할. **오토튜닝**: 새 스캔 시 단일/멀티프로세스/2단 병렬을 **실측**해 가장 빠른
  방법으로 **자동 시작**(첫 화면 실시간 표시). 실 NAS 측정 도구 `tools/bench_walk.py`.
- **튜닝** — 서버 사양 기반 **권장 스레드 계산** + **실측 보정(시범 스캔)**.
- **보안·도구** — **작업 보호 비밀번호**(보기는 자유, 작업은 비밀번호), 설정 잠금,
  **테스트 데이터 생성기**.

> 처음 설치/운영은 **[사용 설명서(docs/USER_GUIDE.md)](docs/USER_GUIDE.md)** 를,
> 버전별 변경점은 **[CHANGELOG.md](CHANGELOG.md)** 를 보세요.
> 버전 확인: `python3 -m isilon_usage --version`

---

## 핵심 아이디어 — 왜 메모리를 적게 쓰는가

트리를 한 번에 처리하지 않고 두 단계로 나눕니다.

### 1단계: 디렉터리 탐색 (top-down, 비재귀)
- 루트부터 한 단계씩 내려가며 각 디렉터리를 `os.scandir` 로 **딱 한 번만** 훑습니다.
- 그 디렉터리에 **직접** 들어 있는 파일들의 용량 합(`own_bytes`)과 파일 수,
  하위 디렉터리 목록을 구해 **즉시 SQLite 에 기록**합니다.
- 다음에 방문할 디렉터리 목록(프론티어)도 **메모리가 아니라 DB**에 둡니다
  (`status='pending'`). 따라서 메모리에는 “지금 보고 있는 디렉터리 하나의 엔트리”만
  올라옵니다. → 파일이 수천만 개여도 메모리 사용량이 거의 일정합니다.

### 2단계: 상향식 집계 (bottom-up)
- 가장 깊은 레벨부터 0(루트)까지 **레벨을 거슬러 올라가며** 처리합니다.
  같은 레벨의 디렉터리를 모두 끝낸 뒤 그 위 레벨로 갑니다
  (요청하신 “최하위 → 같은 레벨 → 상위” 순서 그대로).
- 디렉터리의 재귀 용량 = `own_bytes + Σ(자식들의 재귀 용량)`.
  자식은 이미 더 깊은 레벨에서 계산됐으므로 **재귀 `du` 가 필요 없고, 파일을
  두 번 훑지 않습니다**(전체 디스크를 정확히 1회만 읽음).

> 결과적으로 메모리에는 디렉터리 1개 분량의 엔트리만 올라오고, 모든 결과는
> SQLite 에 흘려보내므로 트리 크기와 무관하게 메모리 사용량이 평탄합니다.
> (테스트: 파일 5만여 개를 스캔해도 스캐너 RSS ≈ 24MB 로 일정)

---

## 측정 백엔드 (`--backend`)

| 백엔드 | 설명 | 특징 |
|--------|------|------|
| `native` (기본) | 위 방식으로 파이썬이 직접 측정 | 파일을 1회만 읽어 **가장 빠르고 저메모리**. 대시보드의 “측정 프로세스 메모리”는 스캐너 프로세스 RSS. |
| `du` | 디렉터리마다 시스템 `du -s` 를 **별도 프로세스**로 실행 | 사용자의 본래 아이디어(“디렉터리별로 du”) 그대로. 그 **`du` 자식 프로세스의 메모리**를 대시보드에 표시. 상위에서 하위를 다시 훑어 더 느림(선택 사항). |

두 백엔드 모두 결과(바이트)는 시스템 `du` 와 일치합니다. 기본값은 `native` 입니다.

용량 기준은 `--size-mode` 로 선택합니다.
- `disk`(기본): 실제 디스크 점유 블록(`st_blocks×512`) — `du` 의 기본과 동일.
- `apparent`: 논리 파일 크기(`st_size`) — `du --apparent-size` 와 동일.

---

## 설치

별도 설치 없이 바로 실행할 수 있습니다(**파이썬 3.6 이상**, 표준 라이브러리만 사용).

**가장 빠른 길 — 한 줄 설치(권장).** 엣지(스캐너)·포탈(HQ)을 각각 `curl … | bash` **한 줄**로
내려받기·검증·설치·systemd 서비스(`isilon-edge`/`isilon-portal`) 등록·기동까지 끝냅니다(같은 줄을
다시 실행하면 업그레이드). 비공개 저장소라 GitHub 토큰(PAT)이 필요하고, 엣지에
`--hq http://<HQ>:8800` 을 붙이면 **포탈에 자동 등록(enroll)** 까지 됩니다. 명령 전체(토큰·`wget`·
공개·`--hq`·오프라인 업그레이드)는 **[download/README.md](download/README.md)** 에 모아 두었습니다.

직접 코드를 받아 실행하려면:
```bash
git clone <repo>
cd isilon_good
python3 -m isilon_usage --help
```

> **폐쇄망 + Python 3.6(RHEL/CentOS 7 등)**: `pip install` 이 필요 없습니다. 압축본을
> 풀어 `python3 -m isilon_usage ...` 로 바로 실행하세요. (사내 미러의 옛 setuptools로
> 굳이 설치하려면 `pip install --no-build-isolation .` 을 사용)

(선택) 더 정확한 자원 지표를 원하면 `psutil` 을 설치합니다. 없으면 자동으로
`/proc` 폴백을 사용합니다.

```bash
pip install -r requirements.txt   # psutil (선택)
```

### 패키지로 설치(콘솔 명령 `isilon-usage`)

```bash
pip install .            # 또는  pip install .[monitor]  (psutil 포함)
isilon-usage --version
isilon-usage serve --data-dir /var/lib/isilon_usage --mount-base /mnt/isilon
```

### Docker

```bash
docker build -t isilon-usage .
docker run -d -p 8765:8765 -v /mnt/isilon:/mnt/isilon:ro -v isilon_data:/data \
    isilon-usage serve --data-dir /data --mount-base /mnt/isilon --host 0.0.0.0 --port 8765
```

---

## 사용법

> **권장 시나리오 — 아이실론을 다른 서버에 마운트해서 사용**
> 아이실론에서 직접 돌리지 않고, `/ifs` 를 다른 리눅스 서버에 NFS/SMB 로 마운트한
> 뒤 그 서버에서 이 도구를 띄웁니다. **조사할 디렉터리는 웹페이지에서 직접 골라
> 시작**할 수 있습니다(아래 “웹에서 디렉터리 지정해 스캔하기” 참고).
>
> ```bash
> # 마운트 예: mount -t nfs isilon:/ifs /mnt/isilon
> python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage \
>         --mount-base /mnt/isilon --port 8765
> # 브라우저 http://<서버>:8765/ → 폴더 탐색으로 디렉터리 선택 → “스캔 시작”
> ```

### 0) 웹에서 디렉터리 지정해 스캔하기 (마운트 사용 시 권장)

```bash
python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage --mount-base /mnt/isilon
```

- 대시보드의 **「새 스캔 시작」** 카드에서:
  - **마운트 빠른 선택**: 서버에 붙은 마운트(NFS/SMB 등)를 버튼으로 바로 선택
  - **폴더 탐색**: 디렉터리를 클릭해 들어가며 원하는 위치를 찾고 “이 디렉터리 선택”
  - 백엔드(native/du)·용량 기준·한 파일시스템 옵션을 고른 뒤 **“스캔 시작”**
- 시작한 스캔은 관리 개요에 바로 나타나고, **진행 중 스캔은 “중지” 버튼**으로 멈출 수 있습니다.
- `--mount-base` 로 **웹에서 스캔 가능한 경로를 제한**합니다(여러 번 지정 가능).
  지정하지 않으면 어떤 경로든 허용되므로, 신뢰망이 아니면 반드시 제한하세요.

### 1) 초기 경로를 바로 주고 시작 (CLI + 대시보드)

```bash
python3 -m isilon_usage run /mnt/isilon/ifs/data --port 8765
```

- 지정한 경로 스캔을 즉시 시작하고 대시보드가 뜹니다. 이후 웹에서 다른 디렉터리도 추가로 스캔할 수 있습니다.
- 중지: `Ctrl+C` (진행 상황은 DB 에 남아 있어 나중에 `serve` 로 다시 볼 수 있음).

자주 쓰는 옵션:

```bash
# du 백엔드로(=디렉터리마다 시스템 du 실행, du 프로세스 메모리 표시)
python3 -m isilon_usage run /ifs/data --backend du

# 논리 크기 기준, 다른 파일시스템으로 안 넘어가게(du -x)
python3 -m isilon_usage run /ifs/data --size-mode apparent --one-file-system

# 데이터 폴더(관리 DB + per-run DB)를 둘 위치 지정
python3 -m isilon_usage run /ifs/data --data-dir /var/lib/isilon_usage
```

### 2) 스캔만 (대시보드 없이, CLI 배치용)

```bash
python3 -m isilon_usage scan /ifs/data --data-dir /var/lib/isilon_usage
```

### 3) 콘솔에서 전체 관리 개요 + 상태 확인

```bash
python3 -m isilon_usage status --data-dir /var/lib/isilon_usage
python3 -m isilon_usage status --data-dir /var/lib/isilon_usage --scan 3   # 특정 스캔 상세
```

### 4) 그 밖의 명령

```bash
python3 -m isilon_usage tune --benchmark /mnt/isilon   # 권장 스레드(사양+실측)
python3 -m isilon_usage stats --data-dir DIR --top 20  # 분석 리포트(나이/소유자/확장자/최대 파일)
python3 -m isilon_usage gentest /data/iutest --dirs 10 --subdirs 5 --files 10 --size 4K -y  # 테스트 트리 생성
python3 -m isilon_usage resume <scan_id> --data-dir DIR   # 중단된 스캔 이어하기
python3 -m isilon_usage prune --data-dir DIR --keep-per-root 5  # 오래된 스캔 정리
python3 -m isilon_usage portal   # 글로벌 통합 포탈(HQ) — 기본 data-dir /data/isilon_usage
```

전체 서브커맨드: `run · scan · serve · status · resume · prune · tune · pscan · autotune · gentest · stats · portal · version`
(자세한 옵션은 [USER_GUIDE](docs/USER_GUIDE.md) 6장 CLI 레퍼런스).

```bash
python3 -m isilon_usage autotune /mnt/isilon/data   # 최적 프로세스×스레드 자동 측정(읽기 전용)
python3 -m isilon_usage pscan    /mnt/isilon/data -P 8 -T 8   # 2단 병렬 빠른 용량
```

---

## 데이터 구조 — 실행마다 별도 DB + 관리 DB

스캔을 **실행할 때마다 별도의 per-run DB** 파일을 만들고, 그 위에 모든 스캔을
모아 보는 **관리(매니저) DB** 를 둡니다.

```
<data-dir>/                     (기본: ./isilon_data, --data-dir 로 변경)
├── manager.db                  관리 DB — 모든 스캔의 요약 카탈로그 + 전체 용량 집계
└── scans/
    ├── scan_20260607-010259_ifs_data.db    실행 #1 의 상세(디렉터리별 집계·자원 시계열)
    ├── scan_20260607-143012_ifs_home.db    실행 #2
    └── ...
```

- **per-run DB**: 그 스캔 한 번의 상세 데이터(디렉터리별 파일 수·용량, 자원 시계열).
  실행마다 `시각 + 경로` 로 이름이 붙어 새로 생기므로 과거 스캔이 덮어써지지 않습니다.
- **관리 DB(manager.db)**: 각 스캔의 요약(루트, 상태, 조사 용량, 파일 수, 디스크
  용량 등)을 한 줄씩 보관합니다. 스캐너가 진행하면서 이 행을 주기적으로 갱신합니다.
- **전체 용량 관리**: 같은 루트를 여러 번 스캔했으면 **루트별 ‘최신 스캔’만** 골라
  합산해(과거 중복 합산 방지) 총 조사 용량을 보여줍니다. 대시보드 상단의
  “전체 용량 관리 개요”와 `status` 명령에서 확인할 수 있습니다.

> 직접 per-run DB 경로를 지정하려면 `--db <파일>` 을 추가할 수 있습니다(그래도
> 관리 DB 에는 함께 등록됩니다).

---

## 대시보드에 표시되는 것

- **새 스캔 시작(상단)** — 마운트 빠른 선택 + 폴더 탐색으로 조사할 디렉터리를
  웹에서 고르고, 백엔드/용량기준/옵션을 선택해 바로 스캔을 시작·중지합니다.
- **설정(⚙ 카드)** — 기본 백엔드/용량기준, 배치 크기, 샘플링 주기, 허용 경로
  (mount_bases), 상위 N, 새로고침 주기 등 **모든 런타임 설정을 웹에서 보고 수정**
  (`<data-dir>/settings.json` 에 저장되어 재시작에도 유지). `--lock-settings` 로 잠금 가능.
- **전체 용량 관리 개요** — 총 조사 용량(루트별 최신 합계), 관리 중 루트 수,
  전체/진행중 스캔 수, 루트별 최신 스캔 표(조사 용량·디스크 사용·확인%·상태,
  진행 중이면 “중지” 버튼).
- **스캔 선택기** — 등록된 스캔 중 하나를 골라 아래 상세를 봅니다(기본은 진행중/
  최신 스캔을 자동 추적).
- **현재 진행 상황**
  - 지금 조사 중인 디렉터리 경로
  - 디렉터리 진행률: `처리한 디렉터리 수 / 전체 디렉터리 수` (집계 단계)
  - **확인된 사용량 / 전체 디스크 사용량 (%)** — 지금까지 조사한 용량이 전체
    디스크 사용량의 몇 %인지 (전체 용량 대비 %도 함께 표시)
- **요약**: 단계, 탐색·집계 디렉터리 수, 총 파일 수, 최대 깊이, 오류(접근불가)
  디렉터리 수, 경과 시간, 예상 잔여(ETA)
- **서버 자원 모니터링**
  - **시스템 메모리 사용률(%)** + 사용/전체 + 스왑, 시계열 그래프
  - **측정 프로세스 메모리(du)** — `du` 백엔드면 실행 중인 `du` 자식 프로세스의
    RSS, `native` 면 스캐너 프로세스 RSS. 스캐너 RSS·peak 도 함께 표시.
  - CPU 사용률, load average, 파일시스템 전체/사용/여유 용량
- **용량 상위 디렉터리(실시간)**: 재귀 용량 기준 상위 디렉터리 표
- **스토리지 어레이 상태** — 설정 시 Isilon/PowerStore/Unity/PowerMax/VMAX/
  XtremIO/VPLEX 의 용량·노드·이벤트 상태(미설정이면 카드 숨김).
- **🔒 작업 보호** — 비밀번호를 걸면 보기는 자유, 작업(버튼·설정 변경)에는 비밀번호.

상단 탭으로 화면을 전환합니다:
**Summary**(진행/요약/자원) · **디렉터리**(드릴다운·검색·상위 디렉터리) ·
**추세·비교**(용량 추세·diff) · **📊 분석 리포트**(나이/소유자/확장자/최대 파일/예측/변화) ·
**⚙ 설정** · **🧪 테스트 데이터** · **📖 버전 기록**.

> 대시보드 서버가 스캐너와 **같은 호스트**에서 돌고 스캔이 진행 중이면, 헤드라인
> 게이지(시스템 메모리/CPU/스캐너 RSS)는 매 폴링마다 실시간으로 갱신됩니다.
> 다른 호스트에서 `serve` 만 띄운 경우엔 스캐너가 DB 에 적재한 샘플을 보여줍니다.

---

## 아키텍처

```
isilon_usage/
├── db.py            per-run DB 스키마/헬퍼 (WAL: 스캔 쓰기 + 대시보드 읽기 동시)
├── manager.py       관리 DB (모든 스캔 카탈로그 + 전체 용량 집계)
├── monitor.py       자원 모니터 + 시스템 사양/권장 스레드 계산
├── scanner.py       스캐너 (탐색 + 상향식 집계, native/du, 안전장치, 집계 리포트)
├── tuning.py        실측 보정(시범 스캔으로 스레드 처리량 비교)
├── gentest.py       테스트용 샘플 디렉터리/파일 생성기
├── settings.py      런타임 설정(웹 편집) + 예약(반복주기)
├── notify.py        완료/오류 웹훅·메일 알림
├── isilon_api.py    Isilon(OneFS) PAPI 상태
├── powerstore_api.py Dell PowerStore REST 상태
├── storage_status.py 어레이 상태 디스패처(Unity/PowerMax/VMAX/XtremIO/VPLEX)
├── portal.py        글로벌 통합 포탈(HQ) — DB 복제 + Cross-DC 조망
├── server.py        대시보드 HTTP 서버 + JSON API + /metrics(Prometheus)
├── dashboard.html   단일 페이지 대시보드(외부 CDN 없음, vanilla JS)
└── cli.py           명령행 인터페이스 (run/scan/serve/status/resume/prune/tune/gentest/stats/portal)
tests/                스캐너·관리·서버·예약·포탈·아이실론 테스트(스크립트 실행)
tools/make_release.py 결정적 릴리스 아카이브 빌드(download/)
```

데이터 모델(요약):
- 관리 DB `scans` — 스캔별 요약 한 줄(루트, 상태, 조사 용량, 디스크 용량 등)
- per-run `scan_runs` — 그 스캔의 메타데이터 + 실시간 진행 상태(워커·하트비트·누적시간 등)
- per-run `directories` — 디렉터리별 집계(파일 수, `own_bytes`, 재귀 `total_bytes` 등)
- per-run `resource_samples` — 자원 사용 시계열(메모리/CPU/RSS 등)
- per-run `scan_stats` — 파일 나이/소유자/확장자별 집계(분석 리포트)
- per-run `top_files` — 최대 파일 Top-N

> 현재 DB 스키마 버전 **9** (`PRAGMA user_version`). 구버전 DB 는 자동 마이그레이션.

### 재시작/이어하기
모든 진행 상태가 SQLite 에 있으므로 중간에 멈춰도 데이터가 남습니다. 같은
`--data-dir` 로 `serve` 하면 모든 과거 스캔과 마지막 상태를 그대로 볼 수 있습니다.

---

## 동작 확인 (데모)

실제 NAS 없이도 합성 트리로 시험할 수 있습니다.

```bash
# 테스트 트리 생성(내장 명령): 디렉터리 60개 / 파일 600개
python3 -m isilon_usage gentest /tmp/isilon_demo --dirs 10 --subdirs 5 --files 10 --size 16K -y
#   (대시보드 '🧪 테스트 데이터' 탭에서도 진행 막대로 생성 가능)

# 스캔 + 대시보드
python3 -m isilon_usage run /tmp/isilon_demo --port 8765
# 브라우저로 http://localhost:8765/  → '📊 분석 리포트' 탭도 확인

# 테스트(스크립트 실행)
for t in tests/test_*.py; do python3 "$t"; done
```

---

## 주의/팁

- **심볼릭 링크**는 따라가지 않습니다(`du` 기본과 유사). 링크 자체의 작은 크기만 셉니다.
- 접근 불가 디렉터리/파일은 건너뛰고 “오류 디렉터리” 수로 집계합니다.
- “확인된 사용량”이 디스크 사용량과 정확히 안 맞을 수 있습니다(하드링크, 스파스
  파일, 다른 마운트, 블록 정렬 등). `--one-file-system` 으로 마운트 경계를
  제한할 수 있습니다.
- 매우 큰 트리에서 `du` 백엔드는 상위 디렉터리에서 하위를 다시 훑어 느립니다.
  특별히 시스템 `du` 의 메모리를 관찰하려는 목적이 아니면 `native` 를 권장합니다.
- 대시보드는 인증이 없습니다. 신뢰된 내부망에서 쓰거나 `--host 127.0.0.1` 로
  바인딩한 뒤 SSH 터널 등으로 접근하세요.
- **웹에서 스캔을 시작**할 수 있으므로(읽기 전용이지만 자원을 쓰는 작업),
  반드시 `--mount-base` 로 스캔 가능한 경로를 마운트 지점으로 제한하세요.
  `--mount-base` 를 주지 않으면 서버 계정이 읽을 수 있는 어떤 경로든 스캔할 수
  있습니다. 폴더 탐색기는 디렉터리 이름만 나열하며 파일 내용은 읽지 않습니다.
- 스캔 프로세스는 마운트를 읽을 수 있는 계정 권한으로 실행하세요(NFS/SMB 권한).
