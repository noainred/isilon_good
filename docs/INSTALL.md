# 설치 매뉴얼 — 처음 사용자를 위한 단계별 안내

이 문서는 **설치에만 집중**한 안내입니다. 설치 후 사용법은
**[처음 시작하기(초보자 가이드)](GETTING_STARTED.md)**, 전체 기능은
**[사용 설명서(USER_GUIDE.md)](USER_GUIDE.md)** 를 보세요.

> 한 줄 요약: **별도 설치가 필요 없습니다.** 파이썬 3.6 이상이 있는 리눅스 서버에 코드를
> 복사하고 `python3 -m isilon_usage serve ...` 로 바로 실행하면 됩니다(표준 라이브러리만 사용).

---

## 0. 5분 안에 끝내기 (요약)

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

## 4. 설치 방법 C — 패키지 설치 / Docker

### pip 패키지(콘솔 명령 `isilon-usage`)
```bash
pip install .            # 또는  pip install .[monitor]  (psutil 포함)
isilon-usage --version
```

### Docker
```bash
docker build -t isilon-usage .
docker run -d -p 8765:8765 \
    -v /mnt/isilon:/mnt/isilon:ro -v isilon_data:/data \
    isilon-usage serve --data-dir /data --mount-base /mnt/isilon \
    --host 0.0.0.0 --port 8765
```

---

## 5. 설치 확인

```bash
python3 -m isilon_usage --version        # isilon_usage 1.x.x (schema 9)
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

브라우저 **http://<서버주소>:8765/** → 「새 스캔 시작」에서 폴더 선택 → **🔬 오토튜닝**(기본 켜짐)
그대로 **스캔 시작**. 프로그램이 가장 빠른 방법을 자동으로 찾아 스캔합니다.

> 원격에서 안전하게: `ssh -L 8765:127.0.0.1:8765 사용자@서버` 후 `--host 127.0.0.1` 로 실행.

---

## 7. 서비스로 상시 운영 (systemd)

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
systemctl daemon-reload
systemctl enable --now isilon-usage
journalctl -u isilon-usage -f          # 로그 보기
```

자세한 서비스 운영(포탈 포함)은 [docs/SERVICE.md](SERVICE.md).

---

## 8. 설치 단계 문제 해결

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

## 9. 보안 체크리스트 (외부 노출 시)

- `--mount-base` 로 **스캔 가능 경로 제한**(필수).
- `--host 127.0.0.1` + SSH 터널, 또는 신뢰망에서만 사용.
- 설정의 **🔒 작업 보호 비밀번호**로 보기는 자유, 작업(버튼·설정 변경)에 비밀번호 요구.
- 조사 대상은 **읽기 전용(`-o ro`)** 으로 마운트(사고 방지).
- 자세히: [SECURITY.md](../SECURITY.md).

---

설치가 끝났으면 **[처음 시작하기 가이드](GETTING_STARTED.md)** 로 첫 스캔을 돌려 보세요. 🚀
