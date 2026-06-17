# 오프라인(폐쇄망) 다운로드 — 버전별 보관소

사설 Nexus 가 불안정할 때를 대비해, **각 버전을 GitHub 에 그대로 보관**합니다.
git 없이 브라우저/`wget` 으로 원하는 버전을 받아 폐쇄망 서버로 옮겨 바로 실행하세요.
(`tar.gz` 또는 `zip` **한 개가 프로그램 전체**입니다.)

## 🚀 엣지 신규 설치 — 한 줄 (curl | bash)

새 엣지(스캐너) 서버에서 **한 줄**이면: 최신본 다운로드 → 검증 → `/opt/isilon_edge` 설치 →
systemd 서비스(`isilon-edge`) 등록·기동까지 끝납니다. 같은 줄을 다시 실행하면 업그레이드입니다.

> ⚠ **`<GitHub_PAT>`·`<HQ-IP>` 같은 꺾쇠 표기는 자리표시자입니다 — 꺾쇠 `< >` 는 빼고 실제 값만**
> 넣으세요. `< >` 를 그대로 두면 셸이 리다이렉션으로 해석해 `parse error near ';'` 가 납니다.
> 예: `TOKEN=ghp_AbC123...`. (root 로 실행 중이면 `sudo` 는 빼도 됩니다.)

**비공개(private) 저장소 — GitHub 토큰(PAT) 필요** (대개 이 경우):
```bash
TOKEN=<GitHub_PAT>; S=$(curl -fsSL -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/noainred/isilon_good/contents/tools/install_edge.sh?ref=claude/upbeat-bell-cXX8f") && printf '%s\n' "$S" | sudo GITHUB_TOKEN="$TOKEN" bash -s -- --mount-base /mnt/isilon
```
- `<GitHub_PAT>` = 이 repo **읽기** 권한이 있는 개인 액세스 토큰. 토큰을 한 번만 넣으면
  스크립트 내려받기와 패키지 다운로드 양쪽에 쓰입니다.
- `--mount-base /mnt/isilon` 뒤에 옵션을 더 붙일 수 있습니다:
  `--port 8765` · `--api-token <포탈연동토큰>` · `--data-dir DIR` · `--branch <브랜치>`.
- **포탈에 자동 등록**: `--hq http://<HQ-IP>:8800` 을 추가하면 설치 직후 이 엣지가 포탈에
  스스로 등록됩니다(자기 IP·api_token 전송 → 포탈 폴링이 바로 인증, 401 없음). 포탈에 로그인
  비밀번호가 걸려 있으면 `--enroll <포탈 enroll 토큰>` 도 함께(포탈 '보안·감사' 탭에서 발급). 예:
  ```bash
  TOKEN=<GitHub_PAT>; S=$(curl -fsSL -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/noainred/isilon_good/contents/tools/install_edge.sh?ref=claude/upbeat-bell-cXX8f") && printf '%s\n' "$S" | sudo GITHUB_TOKEN="$TOKEN" bash -s -- --mount-base /mnt/isilon --hq http://<HQ-IP>:8800 --region 서울
  ```

`curl` 이 없으면 `wget`:
```bash
TOKEN=<GitHub_PAT>; S=$(wget -qO- --header="Authorization: Bearer $TOKEN" --header="Accept: application/vnd.github.raw" "https://api.github.com/repos/noainred/isilon_good/contents/tools/install_edge.sh?ref=claude/upbeat-bell-cXX8f") && printf '%s\n' "$S" | sudo GITHUB_TOKEN="$TOKEN" bash -s -- --mount-base /mnt/isilon
```

**공개(public) 저장소라면** 토큰 없이:
```bash
curl -fsSL "https://raw.githubusercontent.com/noainred/isilon_good/claude/upbeat-bell-cXX8f/tools/install_edge.sh" | sudo bash -s -- --mount-base /mnt/isilon
```

> 설치 후 접속: `http://<서버IP>:8765/` · 로그: `journalctl -u isilon-edge -f`
> 여러 노드를 포탈에 한꺼번에 붙이려면 HQ 포탈의 **노드 설정 → 원격 자동 구성**(IP만 입력)
> 또는 **SSH 원격 자동 설치**가 더 편합니다.

## 🏛 포탈(HQ) 신규 설치 — 한 줄 (curl | bash)

여러 DC 를 집계하는 **HQ 통합 포탈**을 한 줄로: `/opt/isilon_portal` 설치 ·
`/data/isilon_portal_data` 데이터 · 포트 8800 · systemd 서비스 `isilon-portal` 등록·기동.
다시 실행하면 업그레이드. (포탈은 스캔을 안 하므로 `--mount-base` 가 없습니다.)

**비공개(private) 저장소 — 토큰 필요:**
```bash
TOKEN=<GitHub_PAT>; S=$(curl -fsSL -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github.raw" "https://api.github.com/repos/noainred/isilon_good/contents/tools/install_portal.sh?ref=claude/upbeat-bell-cXX8f") && printf '%s\n' "$S" | sudo GITHUB_TOKEN="$TOKEN" bash -s -- --port 8800
```
옵션: `--port 8800` · `--data-dir DIR` · `--install-dir DIR` · `--branch <브랜치>`.

**공개(public) 저장소라면** 토큰 없이:
```bash
curl -fsSL "https://raw.githubusercontent.com/noainred/isilon_good/claude/upbeat-bell-cXX8f/tools/install_portal.sh" | sudo bash -s -- --port 8800
```

> 설치 후 접속: `http://<HQ-IP>:8800/` · 로그: `journalctl -u isilon-portal -f`
> 엣지(스캐너)는 위 '엣지 신규 설치' 한 줄로 따로(보통 다른 서버) 설치하세요.

## ♻️ 엣지 오프라인 업그레이드 — 포탈 릴리스 폴더에서 한 줄 (인터넷·토큰 불필요)

포탈 서버의 **릴리스 폴더**(기본 `/opt/isilon_release`)에 `isilon_usage-*.tar.gz` 하나만 두면,
각 엣지가 **인터넷·GitHub·토큰 없이** 포탈에서 받아 업그레이드합니다. 포탈 **노드 설정 →
업그레이드** 탭에서 폴더를 지정·확인할 수 있고(거기 한 줄 명령도 자동 표시), **엣지 콘솔**에서:
```bash
curl -fsSL "http://<HQ-IP>:8800/api/portal/release" | sudo tar -xz -C /opt/isilon_edge --strip-components=1 && sudo find /opt/isilon_edge -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; (cd /opt/isilon_edge && python3 -m isilon_usage --version) && sudo systemctl restart isilon-edge
```
- 포탈이 그 폴더의 **가장 최신 버전** tarball 을 내려줍니다(파일명 버전 기준).
- 푸시(포탈→엣지)와 달리 **엣지가 당겨가므로** 노드 등록·인바운드 접근이 필요 없습니다.
- root 로 실행 중이면 `sudo` 는 빼도 됩니다. (`<HQ-IP>` 는 꺾쇠 빼고 실제 포탈 주소)

---

## 📦 버전 목록 (버전별 다운로드)
표의 링크를 `wget` 하거나 브라우저로 받으세요. 기계 판독용 목록은 `versions.json`.

<!-- VERSIONS:START -->
| 버전 | 호환 | tar.gz | zip |
|------|------|--------|-----|
| **1.80.2** (latest) | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.80.2.tar.gz) (401 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.80.2.zip) |
| **1.80.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.80.1.tar.gz) (400 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.80.1.zip) |
| **1.80.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.80.0.tar.gz) (399 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.80.0.zip) |
| **1.79.7** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.7.tar.gz) (398 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.7.zip) |
| **1.79.6** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.6.tar.gz) (398 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.6.zip) |
| **1.79.5** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.5.tar.gz) (397 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.5.zip) |
| **1.79.4** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.4.tar.gz) (397 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.4.zip) |
| **1.79.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.3.tar.gz) (395 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.3.zip) |
| **1.79.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.2.tar.gz) (394 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.2.zip) |
| **1.79.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.1.tar.gz) (391 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.1.zip) |
| **1.79.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.0.tar.gz) (389 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.79.0.zip) |
| **1.78.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.78.0.tar.gz) (387 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.78.0.zip) |
| **1.77.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.77.0.tar.gz) (385 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.77.0.zip) |
| **1.76.8** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.8.tar.gz) (383 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.8.zip) |
| **1.76.7** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.7.tar.gz) (382 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.7.zip) |
| **1.76.6** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.6.tar.gz) (381 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.6.zip) |
| **1.76.5** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.5.tar.gz) (380 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.5.zip) |
| **1.76.4** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.4.tar.gz) (380 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.4.zip) |
| **1.76.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.3.tar.gz) (379 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.3.zip) |
| **1.76.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.2.tar.gz) (379 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.2.zip) |
| **1.76.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.1.tar.gz) (378 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.1.zip) |
| **1.76.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.0.tar.gz) (378 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.76.0.zip) |
| **1.75.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.75.1.tar.gz) (376 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.75.1.zip) |
| **1.75.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.75.0.tar.gz) (376 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.75.0.zip) |
| **1.74.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.74.1.tar.gz) (374 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.74.1.zip) |
| **1.74.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.74.0.tar.gz) (373 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.74.0.zip) |
| **1.73.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.73.1.tar.gz) (371 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.73.1.zip) |
| **1.73.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.73.0.tar.gz) (371 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.73.0.zip) |
| **1.72.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.3.tar.gz) (368 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.3.zip) |
| **1.72.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.2.tar.gz) (367 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.2.zip) |
| **1.72.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.1.tar.gz) (367 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.1.zip) |
| **1.72.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.0.tar.gz) (366 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.72.0.zip) |
| **1.71.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.3.tar.gz) (365 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.3.zip) |
| **1.71.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.2.tar.gz) (364 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.2.zip) |
| **1.71.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.1.tar.gz) (363 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.1.zip) |
| **1.71.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.0.tar.gz) (362 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.71.0.zip) |
| **1.70.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.70.1.tar.gz) (358 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.70.1.zip) |
| **1.70.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.70.0.tar.gz) (359 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.70.0.zip) |
| **1.69.8** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.8.tar.gz) (354 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.8.zip) |
| **1.69.7** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.7.tar.gz) (352 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.7.zip) |
| **1.69.6** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.6.tar.gz) (350 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.6.zip) |
| **1.69.5** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.5.tar.gz) (349 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.5.zip) |
| **1.69.4** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.4.tar.gz) (348 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.4.zip) |
| **1.69.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.3.tar.gz) (347 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.3.zip) |
| **1.69.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.2.tar.gz) (346 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.2.zip) |
| **1.69.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.1.tar.gz) (345 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.1.zip) |
| **1.69.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.0.tar.gz) (345 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.69.0.zip) |
| **1.68.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.68.2.tar.gz) (344 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.68.2.zip) |
| **1.68.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.68.1.tar.gz) (342 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.68.1.zip) |
| **1.68.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.68.0.tar.gz) (340 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.68.0.zip) |
| **1.67.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.67.2.tar.gz) (338 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.67.2.zip) |
| **1.67.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.67.1.tar.gz) (337 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.67.1.zip) |
| **1.67.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.67.0.tar.gz) (336 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.67.0.zip) |
| **1.66.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.66.0.tar.gz) (334 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.66.0.zip) |
| **1.65.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.65.2.tar.gz) (330 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.65.2.zip) |
| **1.65.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.65.1.tar.gz) (329 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.65.1.zip) |
| **1.65.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.65.0.tar.gz) (328 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.65.0.zip) |
| **1.64.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.3.tar.gz) (323 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.3.zip) |
| **1.64.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.2.tar.gz) (323 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.2.zip) |
| **1.64.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.1.tar.gz) (322 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.1.zip) |
| **1.64.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.0.tar.gz) (320 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.64.0.zip) |
| **1.63.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.63.0.tar.gz) (318 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.63.0.zip) |
| **1.62.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.62.0.tar.gz) (317 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.62.0.zip) |
| **1.61.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.61.0.tar.gz) (313 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.61.0.zip) |
| **1.60.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.60.0.tar.gz) (312 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.60.0.zip) |
| **1.59.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.59.0.tar.gz) (310 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.59.0.zip) |
| **1.58.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.58.1.tar.gz) (308 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.58.1.zip) |
| **1.58.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.58.0.tar.gz) (308 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.58.0.zip) |
| **1.57.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.57.1.tar.gz) (305 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.57.1.zip) |
| **1.57.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.57.0.tar.gz) (304 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.57.0.zip) |
| **1.56.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.56.0.tar.gz) (296 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.56.0.zip) |
| **1.55.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.55.0.tar.gz) (292 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.55.0.zip) |
| **1.54.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.54.0.tar.gz) (290 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.54.0.zip) |
| **1.53.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.53.0.tar.gz) (289 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.53.0.zip) |
| **1.52.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.52.0.tar.gz) (285 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.52.0.zip) |
| **1.51.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.51.0.tar.gz) (284 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.51.0.zip) |
| **1.50.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.50.0.tar.gz) (283 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.50.0.zip) |
| **1.49.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.49.0.tar.gz) (283 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.49.0.zip) |
| **1.48.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.48.0.tar.gz) (281 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.48.0.zip) |
| **1.47.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.47.0.tar.gz) (280 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.47.0.zip) |
| **1.46.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.46.0.tar.gz) (278 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.46.0.zip) |
| **1.45.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.45.0.tar.gz) (274 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.45.0.zip) |
| **1.44.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.44.0.tar.gz) (267 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.44.0.zip) |
| **1.43.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.43.2.tar.gz) (265 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.43.2.zip) |
| **1.43.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.43.1.tar.gz) (265 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.43.1.zip) |
| **1.43.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.43.0.tar.gz) (264 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.43.0.zip) |
| **1.42.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.42.2.tar.gz) (263 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.42.2.zip) |
| **1.42.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.42.1.tar.gz) (262 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.42.1.zip) |
| **1.42.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.42.0.tar.gz) (262 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.42.0.zip) |
| **1.41.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.41.1.tar.gz) (260 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.41.1.zip) |
| **1.41.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.41.0.tar.gz) (258 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.41.0.zip) |
| **1.40.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.40.0.tar.gz) (253 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.40.0.zip) |
| **1.39.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.39.0.tar.gz) (251 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.39.0.zip) |
| **1.38.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.38.0.tar.gz) (248 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.38.0.zip) |
| **1.37.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.37.0.tar.gz) (245 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.37.0.zip) |
| **1.36.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.36.0.tar.gz) (240 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.36.0.zip) |
| **1.35.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.35.0.tar.gz) (237 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.35.0.zip) |
| **1.34.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.34.1.tar.gz) (235 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.34.1.zip) |
| **1.34.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.34.0.tar.gz) (234 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.34.0.zip) |
| **1.33.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.33.0.tar.gz) (230 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.33.0.zip) |
| **1.32.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.32.1.tar.gz) (226 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.32.1.zip) |
| **1.32.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.32.0.tar.gz) (225 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.32.0.zip) |
| **1.31.5** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.5.tar.gz) (224 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.5.zip) |
| **1.31.4** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.4.tar.gz) (222 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.4.zip) |
| **1.31.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.3.tar.gz) (220 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.3.zip) |
| **1.31.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.2.tar.gz) (220 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.2.zip) |
| **1.31.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.1.tar.gz) (220 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.1.zip) |
| **1.31.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.0.tar.gz) (218 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.31.0.zip) |
| **1.30.4** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.4.tar.gz) (215 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.4.zip) |
| **1.30.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.3.tar.gz) (215 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.3.zip) |
| **1.30.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.2.tar.gz) (214 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.2.zip) |
| **1.30.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.1.tar.gz) (213 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.1.zip) |
| **1.30.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.0.tar.gz) (213 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.30.0.zip) |
| **1.29.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.29.1.tar.gz) (210 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.29.1.zip) |
| **1.29.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.29.0.tar.gz) (209 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.29.0.zip) |
| **1.28.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.28.0.tar.gz) (203 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.28.0.zip) |
| **1.27.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.27.1.tar.gz) (201 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.27.1.zip) |
| **1.27.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.27.0.tar.gz) (200 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.27.0.zip) |
| **1.26.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.26.0.tar.gz) (197 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.26.0.zip) |
| **1.25.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.25.0.tar.gz) (191 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.25.0.zip) |
| **1.24.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.24.0.tar.gz) (182 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.24.0.zip) |
| **1.23.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.23.2.tar.gz) (176 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.23.2.zip) |
| **1.23.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.23.1.tar.gz) (173 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.23.1.zip) |
| **1.23.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.23.0.tar.gz) (173 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.23.0.zip) |
| **1.22.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.22.0.tar.gz) (166 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.22.0.zip) |
| **1.21.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.21.1.tar.gz) (162 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.21.1.zip) |
| **1.21.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.21.0.tar.gz) (161 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.21.0.zip) |
| **1.20.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.20.0.tar.gz) (157 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.20.0.zip) |
| **1.19.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.19.2.tar.gz) (152 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.19.2.zip) |
| **1.19.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.19.1.tar.gz) (152 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.19.1.zip) |
| **1.19.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.19.0.tar.gz) (151 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.19.0.zip) |
| **1.18.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.18.1.tar.gz) (149 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.18.1.zip) |
| **1.18.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.18.0.tar.gz) (148 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.18.0.zip) |
| **1.17.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.17.0.tar.gz) (143 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.17.0.zip) |
| **1.16.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.16.1.tar.gz) (139 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.16.1.zip) |
| **1.16.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.16.0.tar.gz) (137 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.16.0.zip) |
| **1.15.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.15.0.tar.gz) (133 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.15.0.zip) |
| **1.14.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.14.0.tar.gz) (131 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.14.0.zip) |
| **1.13.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.13.0.tar.gz) (127 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.13.0.zip) |
| **1.12.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.12.0.tar.gz) (123 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.12.0.zip) |
| **1.11.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.11.0.tar.gz) (119 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.11.0.zip) |
| **1.10.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.10.0.tar.gz) (116 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.10.0.zip) |
| **1.9.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.9.0.tar.gz) (112 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.9.0.zip) |
| **1.8.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.8.1.tar.gz) (111 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.8.1.zip) |
| **1.8.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.8.0.tar.gz) (111 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.8.0.zip) |
| **1.7.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.7.1.tar.gz) (108 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.7.1.zip) |
| **1.7.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.7.0.tar.gz) (108 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.7.0.zip) |
| **1.6.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.6.0.tar.gz) (105 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.6.0.zip) |
| **1.5.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.5.0.tar.gz) (103 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.5.0.zip) |
| **1.4.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.4.2.tar.gz) (102 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.4.2.zip) |
| **1.4.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.4.1.tar.gz) (102 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.4.1.zip) |
| **1.4.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.4.0.tar.gz) (101 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.4.0.zip) |
| **1.3.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.3.1.tar.gz) (99 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.3.1.zip) |
| **1.3.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.3.0.tar.gz) (96 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.3.0.zip) |
| **1.2.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.2.0.tar.gz) (85 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.2.0.zip) |
| **1.1.9** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.9.tar.gz) (76 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.9.zip) |
| **1.1.8** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.8.tar.gz) (75 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.8.zip) |
| **1.1.7** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.7.tar.gz) (73 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.7.zip) |
| **1.1.6** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.6.tar.gz) (69 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.6.zip) |
| **1.1.5** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.5.tar.gz) (68 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.5.zip) |
| **1.1.4** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.4.tar.gz) (64 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.4.zip) |
| **1.1.3** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.3.tar.gz) (63 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.3.zip) |
| **1.1.2** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.2.tar.gz) (63 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.2.zip) |
| **1.1.1** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.1.tar.gz) (62 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.1.zip) |
| **1.1.0** | Python 3.7+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.0.tar.gz) (60 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.0.zip) |
| **1.0.0** | Python 3.7+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.0.0.tar.gz) (46 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.0.0.zip) |
<!-- VERSIONS:END -->

> ⚠️ 1.0.0 / 1.1.0 은 **Python 3.7+** 전용입니다. RHEL/CentOS 7 의 기본 **Python 3.6**
> 에서는 **1.1.1 이상**을 받으세요.

## ⬇️ wget 으로 받기 (중요: `/blob/` 아님 `/raw/`)
GitHub 파일 페이지(`/blob/...`)를 wget 하면 HTML 이 받아집니다. **`/raw/`** 를 쓰세요.

```bash
# 최신본(URL 고정) — 항상 가장 최신 버전
wget https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-latest.tar.gz

# 특정 버전 받기 (예: 1.1.1)
wget https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.1.tar.gz

# tar.gz + zip 둘 다 한 번에(bash 중괄호 확장)
wget https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.1.{tar.gz,zip}
```

### 비공개(private) 저장소라 wget 이 404/로그인 페이지를 주면
토큰(PAT)으로 GitHub API 를 통해 받습니다(파일 1MB 미만이라 가능):
```bash
curl -L -H "Authorization: Bearer <GITHUB_PAT>" \
     -H "Accept: application/vnd.github.raw" \
     -o isilon_usage-1.1.1.tar.gz \
  "https://api.github.com/repos/noainred/isilon_good/contents/download/isilon_usage-1.1.1.tar.gz?ref=claude/upbeat-bell-cXX8f"
```

## 설치/실행 (압축 해제만으로 — 별도 설치 불필요, Python 3.6+)
```bash
tar xzf isilon_usage-1.1.1.tar.gz        # 또는 unzip isilon_usage-1.1.1.zip
cd isilon_usage-1.1.1
python3 -m isilon_usage --version          # isilon_usage 1.1.1 (schema 1)

python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage \
        --mount-base /mnt/isilon --port 8765
```
표준 라이브러리만으로 동작합니다(psutil 은 선택). 자세한 사용법은 압축본 안의
`docs/USER_GUIDE.md` 참고.

## 압축본 재생성 / 새 버전 추가
```bash
python3 tools/make_release.py     # 현재 버전 + latest + 과거 버전 보관 + 인덱스 갱신
```
make_release.py 는 **기존 버전을 지우지 않고 누적**하며, `versions.json` 과 위
버전 표를 자동으로 갱신합니다.
