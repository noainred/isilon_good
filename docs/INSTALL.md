# 설치 매뉴얼 — 처음 사용자를 위한 단계별 안내

이 문서는 **설치에만 집중**한 안내입니다. 설치 후 사용법은
**[처음 시작하기(초보자 가이드)](GETTING_STARTED.md)**, 전체 기능은
**[사용 설명서(USER_GUIDE.md)](USER_GUIDE.md)** 를 보세요.

> 한 줄 요약: **별도 설치가 필요 없습니다.** 파이썬 3.6 이상이 있는 리눅스 서버에 코드를
> 복사하고 `python3 -m isilon_usage serve ...` 로 바로 실행하면 됩니다(표준 라이브러리만 사용).

> ⚠️ **운영/대외 배포 전 필수.** 기본값은 **평문 HTTP·전 NIC(`0.0.0.0`) 바인드·무인증**입니다.
> 반드시 **신뢰망 한정(인터넷 비노출) + TLS 리버스 프록시 + 작업 보호 비밀번호 + `--mount-base`/
> `--lock-settings`** 를 적용하세요. 설정·자격증명 파일은 자동으로 `0600` 권한으로 저장됩니다.
> 전체 하드닝 절차는 **[보안 가이드(SECURITY.md)](../SECURITY.md)** 를 따르세요.

---

## 0. 5분 안에 끝내기 (요약)

**가장 빠른 길 — 한 줄 설치(권장).** 엣지(스캐너)를 `curl … | bash` **한 줄**로:
최신본 내려받기 → 검증 → `/opt/isilon_edge` 설치 → systemd 서비스(`isilon-edge`) 등록·기동까지
끝냅니다(같은 줄을 다시 실행하면 업그레이드). 비공개 저장소라 **GitHub 토큰(PAT)** 이 필요하고,
`--hq http://<HQ>:8800` 을 붙이면 **포탈에 자동 등록(enroll)** 까지 됩니다. 토큰·`wget`·공개·`--hq`·
**포탈 한 줄**·**오프라인 업그레이드** 등 정확한 명령은 한곳에 모아 두었습니다 →
**[download/README.md](../download/README.md)**.

직접 코드를 받아 실행해도 됩니다:
```bash
cd /opt
git clone https://github.com/noainred/isilon_good.git
cd isilon_good
python3 -m isilon_usage --version            # 설치 확인
mount -t nfs -o ro isilon:/ifs /mnt/isilon   # 조사할 NAS 마운트(예시)
python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage \
        --mount-base /mnt/isilon --port 8765
# 브라우저: http://<서버주소>:8765/
```

자세한 단계와 폐쇄망/Docker/서비스 등록은 아래를 참고하세요.
**여러 서버(여러 DC)라면** 각 서버에 일일이 설치하지 말고 → **[7장. 포탈로 중앙 설치 + 자동
배포](#7-여러-서버에-한-번에--포탈로-중앙-설치--자동-배포)** (HQ에 포탈 한 번 설치 후 엣지 자동 배포).

---

## 1. 사전 요구사항

| 항목 | 요구 | 확인 |
|------|------|------|
| 운영체제 | 리눅스 (자원 수집에 `/proc` 사용) | `uname -a` |
| 파이썬 | **3.6 이상** (RHEL/CentOS 7 기본 3.6 OK) | `python3 --version` |
| 조사 대상 | NAS가 이 서버에 **마운트**되어 읽기 권한 있을 것 | `df -h`, `ls <마운트>` |
| (선택) psutil | 없어도 동작(`/proc` 폴백). 있으면 자원 지표 정밀 | `python3 -c "import psutil"` |

> **권장 구성**: 아이실론(`/ifs`)을 **다른 리눅스 서버에 NFS로 마운트**하고, 그 서버에서
> 이 도구를 띄웁니다. 아이실론 노드에 직접 설치하지 않습니다.

---

## 2. 설치 방법 A — 인터넷이 되는 서버 (git clone)

```bash
cd /opt
git clone https://github.com/noainred/isilon_good.git
cd isilon_good
python3 -m isilon_usage --version
```

끝입니다. 핵심은 **`isilon_usage/` 폴더 하나**에 모든 코드가 들어 있다는 점입니다.

---

## 3. 설치 방법 B — 폐쇄망 서버 (인터넷 불가)

인터넷이 되는 PC에서 받아 압축해 대상 서버로 옮깁니다.

```bash
# (인터넷 PC)
git clone https://github.com/noainred/isilon_good.git
cd isilon_good
tar czf isilon_usage.tgz isilon_usage tools tests docs README.md CHANGELOG.md requirements.txt

# (대상 폐쇄망 서버) — scp/USB 등으로 옮긴 뒤
tar xzf isilon_usage.tgz
python3 -m isilon_usage --version
```

> `pip install` 이 **필요 없습니다.** 사내 미러의 옛 setuptools로 굳이 설치하려면
> `pip install --no-build-isolation .` 을 쓰세요.

### (선택) psutil 을 폐쇄망에 설치
```bash
# (인터넷 PC)
python3 -m pip download psutil -d wheels
# (대상 서버) — wheels 폴더를 옮긴 뒤
python3 -m pip install --no-index --find-links wheels psutil
```

---

## 4. 설치 방법 C — 패키지 / Docker / Synology

### pip 패키지(콘솔 명령 `isilon-usage`)
```bash
pip install .            # 또는  pip install .[monitor]  (psutil 포함)
isilon-usage --version
```

### Docker (표준 라이브러리라 pip 불필요 · 폐쇄망/오프라인 빌드 가능)
```bash
docker build -t isilon-usage .
docker run -d -p 8765:8765 \
    -v /mnt/isilon:/mnt/isilon:ro -v isilon_data:/data \
    isilon-usage serve --data-dir /data --mount-base /mnt/isilon \
    --host 0.0.0.0 --port 8765
```
자세히(compose · 오프라인 `save`/`load` · 사내 베이스): **[docs/DOCKER.md](DOCKER.md)**.

### Synology (DSM 7.x · .spk)
```bash
python3 tools/build_synology_spk.py      # → download/synology/isilon_usage-<버전>.spk
```
Package Center → **수동 설치**로 올리면 포트 8765에서 뜹니다(noarch, run-as package 샌드박스).
공유폴더 읽기 권한 부여 등 자세히: **[docs/SYNOLOGY.md](SYNOLOGY.md)**.

> **폐쇄망 자동 설치(엣지/포탈)**: `tools/install_edge.sh`·`install_portal.sh` 의 다운로드 소스는
> 사내 미러가 기본입니다 — `IU_MIRROR_ROOT=<미러> sudo -E bash install_edge.sh …` 또는
> `--base-url <미러>`(토큰 불필요). 공개 GitHub 경로를 쓸 때만 PAT 이 필요합니다.

---

## 5. 설치 확인

```bash
python3 -m isilon_usage --version        # isilon_usage 1.99.23 (schema 9)
python3 -m isilon_usage version          # 상세 환경(파이썬·psutil 유무 등)
python3 tests/test_scanner.py            # "모든 테스트 통과 ✅"
python3 tests/test_autotune.py           # 오토튜닝 측정 코어 확인
```

NAS 없이 동작을 보려면 가짜 트리로 시험:
```bash
python3 -m isilon_usage gentest /tmp/demo --dirs 10 --subdirs 5 --files 10 --size 16K -y
python3 -m isilon_usage run /tmp/demo --mount-base /tmp --port 8765
```

---

## 6. 첫 실행 (대시보드 + 웹 스캔)

```bash
mkdir -p /var/lib/isilon_usage
python3 -m isilon_usage serve \
        --data-dir /var/lib/isilon_usage \   # 결과 DB 저장 폴더(계속 같은 걸 쓰세요)
        --mount-base /mnt/isilon \            # 웹에서 스캔 허용 경로(안전 제한)
        --host 0.0.0.0 --port 8765
```

브라우저 **http://<서버주소>:8765/** → 「새 스캔 시작」에서 폴더 선택 → 엔진을 고르고(또는 **🔬 오토튜닝**
체크 — 기본 꺼짐) **스캔 시작**. 오토튜닝을 켜면 프로그램이 가장 빠른 방법을 자동으로 찾아 스캔합니다.

> 원격에서 안전하게: `ssh -L 8765:127.0.0.1:8765 사용자@서버` 후 `--host 127.0.0.1` 로 실행.

---

## 7. 여러 서버에 한 번에 — 포탈로 중앙 설치 + 자동 배포

데이터센터가 여러 곳이면 **각 서버에 일일이 설치할 필요가 없습니다.** 본사(HQ)에 **글로벌
포탈을 한 번만 설치**하고, 포탈 웹에서 각 엣지(스캐너) 서버를 **자동으로 배포**하면 됩니다.

### 7-1. 포탈(HQ) 설치·실행
스캐너와 **같은 코드**입니다(따로 받을 것 없음). 가장 쉬운 건 **한 줄 설치**(install_portal.sh →
`/opt/isilon_portal` · 데이터 `/data/isilon_portal_data` · 서비스 `isilon-portal` · 포트 8800,
명령은 [download/README.md](../download/README.md)). 직접 띄우려면 HQ 서버에서:
```bash
python3 -m isilon_usage portal --data-dir /var/lib/isilon_portal --port 8800
# 브라우저: http://<HQ주소>:8800/
```

### 7-2. 포탈에서 엣지 자동 배포 (대상 서버에 수동 설치 불필요)
포탈 웹 **「노드 설정 → 🚀 설치·구성」** 에서 대상 서버 정보를 넣으면 두 방식으로 배포합니다:

- **A) 설치 스크립트 생성 (권장·무의존)** — 버튼으로 스크립트를 만들어 **엣지 서버에서 1회
  복붙 실행**. 스캐너를 설치·기동하고 **포탈에 자동 등록**까지 됩니다(아무 의존성도 필요 없음).
- **B) SSH 자동 푸시 (옵션)** — 포탈이 SSH로 접속해 설치·구성을 **대신 실행**합니다
  (신뢰망·키 인증 권장). 여러 대를 한 번에 올릴 때 편리합니다.
- **C) 엣지에서 한 줄 자기등록 (`--hq`)** — 엣지 서버에서 설치 한 줄에 `--hq http://<HQ>:8800`
  을 붙이면, 설치 직후 엣지가 자기 주소·`api_token` 을 포탈에 보내 **스스로 등록**됩니다(포탈에
  로그인 비밀번호가 있으면 '보안·감사' 탭의 enroll 토큰을 `--enroll` 로 함께). 명령은
  [download/README.md](../download/README.md).
- **여러 대를 CSV로 한 번에 등록** — 포탈 **「노드 설정 → 🗂 노드 관리 → 📋 CSV로 여러 서버
  가져오기」** 에 `id,url,region,token,…` 형식 CSV 를 붙여넣거나 `.csv` 파일을 올리면 **한 번에
  등록**됩니다(첫 줄 헤더, `id`·`url` 필수, 같은 id 는 수정, 토큰 빈값은 기존 유지). 등록 후 위
  자동 배포/업그레이드로 일괄 운영하세요.

> 등록이 끝나면 포탈 **글로벌 대시보드**에 각 DC의 용량·스캔 상태가 모입니다.

### 7-3. 이후 버전도 자동 배포 (업그레이드)
포탈 설정의 **감시 폴더**에 새 버전 패키지(`isilon_usage-*.tar.gz`)를 두면, 포탈이 자기 자신을
업그레이드하면서 **등록된 모든 엣지에도 새 코드를 푸시**합니다(엣지 토큰 인증, SSH 불필요).
포탈 **「노드 설정 → 🔁 자동 업그레이드」** 에서 감시 폴더 지정과 **전 노드 지금 업그레이드**도
수동으로 실행할 수 있습니다.

> **요약:** 포탈 한 번 설치 → 엣지 자동 배포 → 이후 버전도 자동 전파. 자세한 운영(노드 등록
> 옵션·경로 별칭·복제 주기·Cross-DC 비교)은 [사용 설명서 12장 — 글로벌 통합 포탈](USER_GUIDE.md#12-글로벌-통합-포탈-hq).

---

## 8. 서비스로 상시 운영 (systemd)

실제 배포 유닛은 저장소의 **`packaging/isilon-edge.service`**(설치 스크립트가 그대로 사용)를 기준으로 하세요.
아래는 그와 같은 값의 개념 예시입니다 — `/etc/systemd/system/isilon-edge.service`:
```ini
[Unit]
Description=Isilon 디렉터리 사용량 스캐너 대시보드
After=network.target remote-fs.target local-fs.target
RequiresMountsFor=/data/isilon_usage

[Service]
Type=simple
User=root
WorkingDirectory=/opt/isilon_edge
ExecStart=/usr/bin/python3 -m isilon_usage serve \
          --data-dir /data/isilon_usage --mount-base /mnt/isilon \
          --host 0.0.0.0 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now isilon-edge
journalctl -u isilon-edge -f          # 로그 보기
```

자세한 서비스 운영(포탈 포함)은 [docs/SERVICE.md](SERVICE.md).

---

## 9. 설치 단계 문제 해결

| 증상 | 해결 |
|------|------|
| `python3: command not found` | 파이썬 3.6+ 설치, 또는 `python` 로 시도 |
| `pip install` 시 `setuptools>=61` 못 찾음(폐쇄망) | pip 설치 불필요 — 압축 풀어 `python3 -m isilon_usage` 로 실행 |
| 브라우저 접속 안 됨 | `--host 0.0.0.0` 확인, 방화벽/포트(`firewall-cmd`, `iptables`) 확인 |
| `Address already in use` | 다른 `--port` 사용 |
| 대시보드에 `/proc 폴백` 표시 | 정상(psutil 미설치). 원하면 `pip install psutil` |
| 실행 후 `df` 에 마운트가 잔뜩 | **정상**(아이실론 자동 서브마운트). ⚠ `한 파일시스템(-x)` 은 켜지 마세요 |
| "새 스캔 시작" 거부 | `--mount-base` 안쪽 경로를 고르세요 |

---

## 10. 보안 체크리스트 (외부 노출 시)

- `--mount-base` 로 **스캔 가능 경로 제한**(필수).
- `--host 127.0.0.1` + SSH 터널, 또는 신뢰망에서만 사용.
- 설정의 **🔒 작업 보호 비밀번호**로 보기는 자유, 작업(버튼·설정 변경)에 비밀번호 요구.
- 조사 대상은 **읽기 전용(`-o ro`)** 으로 마운트(사고 방지).
- 자세히: [SECURITY.md](../SECURITY.md).

---

설치가 끝났으면 **[처음 시작하기 가이드](GETTING_STARTED.md)** 로 첫 스캔을 돌려 보세요. 🚀
