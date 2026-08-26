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
| **1.99.26** (latest) | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.26.tar.gz) (538 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.26.zip) |
| **1.99.25** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.25.tar.gz) (534 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.25.zip) |
| **1.99.24** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.24.tar.gz) (525 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.24.zip) |
| **1.99.23** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.23.tar.gz) (512 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.23.zip) |
| **1.99.22** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.22.tar.gz) (512 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.22.zip) |
| **1.99.21** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.21.tar.gz) (511 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.21.zip) |
| **1.99.20** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.20.tar.gz) (509 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.20.zip) |
| **1.99.19** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.19.tar.gz) (498 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.19.zip) |
| **1.99.18** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.18.tar.gz) (496 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.18.zip) |
| **1.99.17** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.17.tar.gz) (493 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.99.17.zip) |
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
| **1.13.0** | Python 3.6+ | [tar.gz](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.13.0.tar.gz) (124 KB) | [zip](https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download/isilon_usage-1.13.0.zip) |
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
