#!/usr/bin/env bash
# =============================================================================
# isilon_portal 자동 설치/업그레이드 스크립트 (HQ 통합 포탈)
#   - 최신 패키지를 다운받아 /tmp/isilon_portal 에서 검증한 뒤 /opt/isilon_portal 에
#     설치하고, systemd 서비스(isilon-portal)로 등록한 뒤 재시작한다.
#   - 다시 실행하면 최신 버전으로 교체 후 재시작(업그레이드).
#   - 포탈은 스캔을 하지 않으므로 --mount-base 가 없다(엣지와 분리 운영 권장).
#
# 사용:  sudo bash install_portal.sh [--port 8800] [--branch <git브랜치>]
#                                    [--data-dir DIR] [--install-dir DIR] [--tmp-dir DIR]
#                                    [--base-url <미러베이스>]
#   기본 다운로드 소스는 사내(폐쇄망) 미러다. 다른 미러면 --base-url 로 베이스를 준다. 예:
#     http://repository.dvc.lgensol.com:8081/repository/manager-upgrade/isilon_good/raw/<branch>
# =============================================================================
set -euo pipefail

# ===== 기본 설정 =====
BRANCH="claude/upbeat-bell-cXX8f"          # 다운로드할 git 브랜치
INSTALL_DIR="/opt/isilon_portal"           # 프로그램 설치 경로
DATA_DIR="/data/isilon_portal_data"        # 데이터(노드 레지스트리/복제본/설정)
DL_DIR="/opt"                              # 다운로드(압축본) 저장 경로
TMP_DIR="/tmp/isilon_portal"               # 임시 작업(압축 해제·검증) 디렉터리
PORT="8800"                                # 포탈 포트
SERVICE="isilon-portal"                    # systemd 서비스 이름
GITHUB_TOKEN="${GITHUB_TOKEN:-}"           # (선택) GitHub 비공개 저장소 토큰 — 미러를 쓰면 불필요
REPO="noainred/isilon_good"                # 저장소 이름(미러 경로/토큰 모드에 사용)
MIRROR_ROOT="http://repository.dvc.lgensol.com:8081/repository/manager-upgrade/isilon_good/raw"  # 폐쇄망 미러 루트
BASE_URL=""                                # 다운로드 베이스 직접 지정(비우면 미러: MIRROR_ROOT/BRANCH)

# ===== 인자로 덮어쓰기 =====
while [ $# -gt 0 ]; do
  case "$1" in
    --port)        PORT="$2";        shift 2;;
    --branch)      BRANCH="$2";      shift 2;;
    --data-dir)    DATA_DIR="$2";    shift 2;;
    --install-dir) INSTALL_DIR="$2"; shift 2;;
    --tmp-dir)     TMP_DIR="$2";     shift 2;;
    --token|--github-token) GITHUB_TOKEN="$2"; shift 2;;
    --base-url|--mirror) BASE_URL="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "알 수 없는 옵션: $1 (도움말: --help)"; exit 1;;
  esac
done

RAW_BASE="${BASE_URL:-$MIRROR_ROOT/$BRANCH}"   # 다운로드 베이스(기본=폐쇄망 미러)

echo "============================================================"
echo " isilon_portal(HQ) 설치/업그레이드"
echo "   설치 경로 : $INSTALL_DIR"
echo "   데이터    : $DATA_DIR"
echo "   소스      : $RAW_BASE"
echo "   브랜치    : $BRANCH   포트: $PORT   임시: $TMP_DIR"
echo "============================================================"

# ----- 사전 점검 -----
[ "$(id -u)" = "0" ] || { echo "✗ root 권한이 필요합니다. sudo 로 실행하세요."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "✗ python3 가 필요합니다(3.6+)."; exit 1; }
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
  || { echo "✗ curl 또는 wget 이 필요합니다."; exit 1; }

# 다운로드 함수: FETCH <dest> <relpath>
#  - 베이스가 github.com 이고 GITHUB_TOKEN 이 있으면 GitHub API(비공개)로 인증 다운로드.
#  - 그 외(폐쇄망 미러/공개 raw)는 단순 다운로드(인증 불필요). curl 우선, 없으면 wget.
FETCH() {
  dest="$1"; rel="$2"
  case "$RAW_BASE" in
    *github.com*)
      if [ -n "$GITHUB_TOKEN" ]; then
        url="https://api.github.com/repos/${REPO}/contents/${rel}?ref=${BRANCH}"
        if command -v curl >/dev/null 2>&1; then
          curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" \
               -H "Accept: application/vnd.github.raw" -o "$dest" "$url"
        else
          wget -q --header="Authorization: Bearer ${GITHUB_TOKEN}" \
               --header="Accept: application/vnd.github.raw" -O "$dest" "$url"
        fi
        return
      fi
      ;;
  esac
  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$dest" "${RAW_BASE}/${rel}"
  else wget -qO "$dest" "${RAW_BASE}/${rel}"; fi
}
_hint_private() {
  echo "  ↳ 소스 접근을 확인하세요: $RAW_BASE"
  echo "    - 폐쇄망 미러면 주소/포트/방화벽과 versions.json 존재 여부를 확인."
  echo "    - 다른 미러로 받으려면: sudo bash $0 --base-url <미러베이스URL>"
}

# ----- 1) 최신 버전 확인 -----
mkdir -p "$DL_DIR"
VJSON="$DL_DIR/.isilon_versions.json"
echo "→ 최신 버전 확인: download/versions.json"
if ! FETCH "$VJSON" "download/versions.json"; then
  echo "✗ 버전 정보 조회 실패(네트워크/브랜치/권한 확인)."; _hint_private; exit 1
fi
VER="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['latest'])" "$VJSON" 2>/dev/null || true)"
if [ -z "$VER" ]; then
  echo "✗ 최신 버전을 읽지 못했습니다(받은 내용이 올바른 JSON 이 아님)."; _hint_private; exit 1
fi
echo "   최신 버전: $VER"

# ----- 2) 다운로드 (/opt) -----
TARBALL="isilon_usage-${VER}.tar.gz"
DEST="$DL_DIR/$TARBALL"
echo "→ 다운로드: download/$TARBALL"
FETCH "$DEST" "download/$TARBALL" || { echo "✗ 다운로드 실패."; _hint_private; exit 1; }
tar tzf "$DEST" >/dev/null 2>&1 || { echo "✗ 내려받은 파일이 손상되었습니다."; exit 1; }
# 체크섬 검증: versions.json 의 sha256 과 대조(변조/탈취 미러 차단). 없으면 건너뜀(구버전 호환).
EXPSHA="$(python3 -c "import json,sys
d=json.load(open(sys.argv[1]))
print(next((v.get('sha256','') for v in d.get('versions',[]) if str(v.get('version'))==sys.argv[2]), ''))" "$VJSON" "$VER" 2>/dev/null || true)"
if [ -n "$EXPSHA" ]; then
  GOTSHA="$(sha256sum "$DEST" 2>/dev/null | awk '{print $1}')"
  [ "$GOTSHA" = "$EXPSHA" ] || { echo "✗ 무결성 검증 실패(sha256 불일치) — 설치 중단."; echo "   기대 ${EXPSHA} / 받음 ${GOTSHA}"; exit 1; }
  echo "   ✓ sha256 검증 통과"
fi

# ----- 3) 임시 디렉터리에 압축 해제 + 검증(최상위 isilon_usage-<버전>/ 제거) -----
echo "→ 임시 작업: $TMP_DIR"
rm -rf "$TMP_DIR"; mkdir -p "$TMP_DIR"
tar xzf "$DEST" -C "$TMP_DIR" --strip-components=1
echo -n "→ 패키지 검증: "
( cd "$TMP_DIR" && python3 -m isilon_usage --version ) \
  || { echo "✗ 검증 실패 — 설치 중단(기존 설치 그대로 유지)."; rm -rf "$TMP_DIR"; exit 1; }

# ----- 4) 검증 통과분만 설치 경로로 반영 -----
echo "→ 설치: $INSTALL_DIR"
systemctl stop "$SERVICE" >/dev/null 2>&1 || true     # 파일 교체 안정화
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$TMP_DIR"/ "$INSTALL_DIR"/
else
  cp -a "$TMP_DIR"/. "$INSTALL_DIR"/
fi
rm -rf "$TMP_DIR"
cd "$INSTALL_DIR"

# ----- 5) systemd 서비스 등록 -----
UNIT="/etc/systemd/system/${SERVICE}.service"
echo "→ 서비스 등록: $UNIT"
cat > "$UNIT" <<EOF
[Unit]
Description=Isilon Portal (HQ) - 글로벌 통합 관제
After=network.target remote-fs.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 -m isilon_usage portal --data-dir $DATA_DIR --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ----- 6) 재시작 -----
echo "→ 서비스 재시작"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 1
systemctl --no-pager -l status "$SERVICE" 2>/dev/null | head -6 || true

echo
echo "✅ 완료 — isilon_portal(HQ) $VER 설치·서비스 등록·재시작"
echo "   접속 : http://<HQ주소>:$PORT/   (노드 등록은 웹의 '노드 설정')"
echo "   로그 : journalctl -u $SERVICE -f"
echo "   업그레이드 : 이 스크립트를 다시 실행하면 최신으로 교체 후 재시작합니다."
