# 서비스로 실행하기 (systemd)

대시보드(`serve`)를 **systemd 서비스**로 돌리면: 부팅 시 자동 시작, 비정상 종료 시
자동 재시작, SSH 세션이 끊겨도 계속 실행, `journalctl` 로 로그 확인이 됩니다.
(Rocky/RHEL/CentOS 등 systemd 환경 기준.)

> 유닛 템플릿: [`packaging/isilon_usage.service`](../packaging/isilon_usage.service)

---

## 1. 코드 배치

폐쇄망이면 압축본을 풀어 **패키지 디렉터리만** 고정 위치에 둡니다.

```bash
sudo mkdir -p /opt/isilon_usage
unzip isilon_usage-latest.zip                      # isilon_usage-<버전>/ 생성
sudo cp -r isilon_usage-*/isilon_usage /opt/isilon_usage/
# 결과: /opt/isilon_usage/isilon_usage/  (패키지)  → python3 -m isilon_usage 가능
python3 -m isilon_usage --version                  # 동작 확인(해당 폴더에서)
```

> (대안) `pip install .` 로 설치했다면 콘솔 명령 `isilon-usage` 가 생기므로
> `ExecStart=/usr/bin/isilon-usage serve …` 로 쓰고 `WorkingDirectory` 는 임의 폴더면 됩니다.

## 2. (선택) 전용 계정

루트로 돌려도 되지만, 보안상 **읽기 권한만 가진 전용 계정**을 권장합니다.
단, 그 계정이 **대상 NFS 마운트를 읽을 수 있어야** 합니다(NFS 권한/uid 매핑 확인).

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin isilon
# 유닛에서 User=isilon / Group=isilon 주석 해제
```

## 3. 유닛 설치 + 수정

```bash
sudo cp packaging/isilon_usage.service /etc/systemd/system/isilon_usage.service
sudo vi /etc/systemd/system/isilon_usage.service
#  - WorkingDirectory = /opt/isilon_usage
#  - --data-dir /var/lib/isilon_usage   (StateDirectory 와 일치)
#  - --mount-base /mnt/hadoop           (스캔 허용 경로)
#  - --host / --port                    (LAN 접근이면 0.0.0.0)
```

## 4. 시작 + 자동시작 등록

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now isilon_usage      # 지금 시작 + 부팅 시 자동 시작
sudo systemctl status isilon_usage            # 상태 확인
journalctl -u isilon_usage -f                 # 실시간 로그
```

브라우저로 `http://<서버IP>:8765/` 접속 → 대시보드. 스캔은 웹의 **'새 스캔 시작'** 에서.

---

## 관리 명령

| 동작 | 명령 |
|---|---|
| 상태 | `systemctl status isilon_usage` |
| 시작/중지/재시작 | `systemctl {start,stop,restart} isilon_usage` |
| 로그(실시간/최근) | `journalctl -u isilon_usage -f` / `journalctl -u isilon_usage -n 200` |
| 자동시작 끄기 | `systemctl disable isilon_usage` |

## 코드 업데이트

```bash
# 새 버전 패키지로 교체 후 재시작(스캔은 프론티어가 DB에 있어 '재개'로 이어감)
sudo cp -r isilon_usage-<새버전>/isilon_usage /opt/isilon_usage/
sudo systemctl restart isilon_usage
```

> 대시보드 화면만 바꾸는 경우(프론트 전용 변경)는 `isilon_usage/dashboard.html` 만
> 덮어쓰고 브라우저 하드 리프레시(Ctrl+Shift+R)로도 적용됩니다.

## 안전장치 설정 (min-free-gb / db-max-gb / fold-depth)

`serve` 명령줄이 아니라 **대시보드 '설정'(→ `<data-dir>/settings.json`)** 에서 적용됩니다.
서비스 기동 후 설정 화면에서 한 번 지정하면 이후 웹 스캔에 반영됩니다.

## 부팅 시 특정 경로를 자동 스캔하고 싶다면

대시보드 없이 스캔만 자동 시작하려면 `serve` 대신 `run` 을 쓰는 별도 유닛을 둡니다.

```ini
ExecStart=/usr/bin/python3 -m isilon_usage run /mnt/hadoop \
    --data-dir /var/lib/isilon_usage --fold-depth 5 --min-free-gb 5 --db-max-gb 30
Restart=no
```
(`run` 은 scan 옵션을 직접 받습니다. 끝나면 종료되므로 `Restart=no`.)

## HQ에서 포탈과 스캐너 분리 운영 (업그레이드 격리)

HQ 서버가 **로컬 디스크 스캔(`serve`)** 과 **글로벌 집계 포탈(`portal`)** 을 둘 다
돌릴 때, **포탈을 업그레이드/재시작해도 스캔이 중단되지 않게** 하려면 둘을 완전히
분리합니다. 둘은 원래 별도 프로세스이지만, **코드 디렉터리·data-dir·포트·서비스**를
나눠 두면 한쪽을 건드려도 다른 쪽에 절대 영향이 없습니다.

```
HQ 서버
├── /opt/isilon_edge/isilon_usage/     ← 스캐너 코드(serve)        :8765
│   └─ 서비스 isilon_usage         data-dir /var/lib/isilon_usage
└── /opt/isilon_portal/isilon_usage/   ← 포탈 코드(portal)         :8800
    └─ 서비스 isilon_usage_portal  data-dir /var/lib/isilon_portal
```

**설치 (두 디렉터리에 각각 패키지 배치)**
```bash
sudo mkdir -p /opt/isilon_edge /opt/isilon_portal
sudo cp -r isilon_usage-*/isilon_usage /opt/isilon_edge/      # 스캐너용
sudo cp -r isilon_usage-*/isilon_usage /opt/isilon_portal/    # 포탈용(별도 사본)

# 스캐너 유닛: WorkingDirectory=/opt/isilon_edge
sudo cp isilon_usage-*/packaging/isilon_usage.service        /etc/systemd/system/
# 포탈 유닛: WorkingDirectory=/opt/isilon_portal, 포트 8800
sudo cp isilon_usage-*/packaging/isilon_usage_portal.service /etc/systemd/system/
sudo vi /etc/systemd/system/isilon_usage.service        # WorkingDirectory=/opt/isilon_edge
sudo systemctl daemon-reload
sudo systemctl enable --now isilon_usage isilon_usage_portal
```

**포탈만 업그레이드 (스캔 무중단)**
```bash
sudo cp -r isilon_usage-<새버전>/isilon_usage /opt/isilon_portal/   # 포탈 코드만 교체
sudo systemctl restart isilon_usage_portal                          # 포탈만 재시작
# → /opt/isilon_edge 와 isilon_usage(스캐너)는 손대지 않음 = HQ 디스크 스캔 계속 진행
```

> 참고: 같은 디렉터리를 공유해도, **실행 중 프로세스는 시작 시점의 코드를 메모리에**
> 들고 돌기 때문에 파일을 바꿔도 그 프로세스는 영향받지 않습니다(재시작 전까지).
> 그래도 위처럼 **코드 사본·서비스를 분리**해 두면 실수로라도 스캐너를 건드릴 일이
> 없어 가장 안전합니다.

## 보안 (외부 노출 시 필수)

`--host 0.0.0.0` 은 모든 NIC에 노출됩니다. 신뢰망(사내 LAN)이 아니면:
- `--host 127.0.0.1` + nginx/Caddy 리버스 프록시(HTTPS + 인증), 또는 SSH 터널
- 대시보드 **작업 보호 비밀번호**(설정), `--lock-settings`(설정 편집 잠금)

자세한 내용: [SECURITY.md](../SECURITY.md)

## systemd 가 없는 환경

```bash
# nohup (가장 간단) — 세션 끊겨도 유지
cd /opt/isilon_usage
nohup python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage \
      --mount-base /mnt/hadoop --host 0.0.0.0 --port 8765 \
      > /var/log/isilon_usage.log 2>&1 &
# 또는 tmux/screen 세션 안에서 실행
```
