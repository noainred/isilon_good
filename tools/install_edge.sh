#!/usr/bin/env bash
# =============================================================================
# isilon_edge 자동 설치/업그레이드 스크립트
#   - 최신 패키지를 다운받아 /opt/isilon_edge 에 설치하고
#     systemd 서비스(isilon-edge)로 등록한 뒤 재시작한다.
#   - 다시 실행하면 최신 버전으로 교체 후 재시작(업그레이드)된다.
#
# 사용:  sudo bash install_edge.sh [--port 8765] [--mount-base /mnt/isilon]
#                                  [--branch <git브랜치>] [--data-dir DIR] [--install-dir DIR]
# =============================================================================
set -euo pipefail

# ===== 기본 설정(요청 사양) =====
BRANCH="claude/upbeat-bell-cXX8f"        # 다운로드할 git 브랜치
INSTALL_DIR="/opt/isilon_edge"           # 프로그램 설치 경로
DATA_DIR="/data/isilon_edge_data"        # 데이터(DB/설정) 디렉터리
DL_DIR="/opt"                            # 다운로드(압축본) 저장 경로
PORT="8765"                              # 대시보드 포트
MOUNT_BASE=""                            # 스캔 허용 경로(예: /mnt/isilon). 비우면 전체 허용
SERVICE="isilon-edge"                    # systemd 서비스 이름

# ===== 인자로 덮어쓰기 =====
while [ $# -gt 0 ]; do
  case "$1" in
    --port)        PORT="$2";        shift 2;;
    --mount-base)  MOUNT_BASE="$2";  shift 2;;
    --branch)      BRANCH="$2";      shift 2;;
    --data-dir)    DATA_DIR="$2";    shift 2;;
    --install-dir) INSTALL_DIR="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "알 수 없는 옵션: $1 (도움말: --help)"; exit 1;;
  esac
done

BASE_URL="https://github.com/noainred/isilon_good/raw/${BRANCH}/download"

echo "============================================================"
echo " isilon_edge 설치/업그레이드"
echo "   설치 경로 : $INSTALL_DIR"
echo "   데이터    : $DATA_DIR"
echo "   다운로드  : $DL_DIR"
echo "   브랜치    : $BRANCH   포트: $PORT"
echo "============================================================"

# ----- 사전 점검 -----
[ "$(id -u)" = "0" ] || { echo "✗ root 권한이 필요합니다. sudo 로 실행하세요."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "✗ python3 가 필요합니다(3.6+)."; exit 1; }

# 다운로드 도구(curl 우선, 없으면 wget)
if   command -v curl >/dev/null 2>&1; then GET(){ curl -fsSL -o "$1" "$2"; }
elif command -v wget >/dev/null 2>&1; then GET(){ wget -qO "$1" "$2"; }
else echo "✗ curl 또는 wget 이 필요합니다."; exit 1; fi

# ----- 1) 최신 버전 확인 -----
mkdir -p "$DL_DIR"
VJSON="$DL_DIR/.isilon_versions.json"
echo "→ 최신 버전 확인: $BASE_URL/versions.json"
GET "$VJSON" "$BASE_URL/versions.json" || { echo "✗ 버전 정보 조회 실패(네트워크/브랜치 확인)."; exit 1; }
VER="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['latest'])" "$VJSON" 2>/dev/null || true)"
[ -n "$VER" ] || { echo "✗ 최신 버전을 읽지 못했습니다."; exit 1; }
echo "   최신 버전: $VER"

# ----- 2) 다운로드 (/opt) -----
TARBALL="isilon_usage-${VER}.tar.gz"
DEST="$DL_DIR/$TARBALL"
echo "→ 다운로드: $BASE_URL/$TARBALL"
GET "$DEST" "$BASE_URL/$TARBALL" || { echo "✗ 다운로드 실패."; exit 1; }
# 무결성 간단 확인(정상 gzip tar 인지)
tar tzf "$DEST" >/dev/null 2>&1 || { echo "✗ 내려받은 파일이 손상되었습니다."; exit 1; }

# ----- 3) 설치(압축 해제: 최상위 isilon_usage-<버전>/ 제거) -----
echo "→ 설치: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# 실행 중 서비스가 있으면 잠시 멈춰 파일 교체 안정화
systemctl stop "$SERVICE" >/dev/null 2>&1 || true
tar xzf "$DEST" -C "$INSTALL_DIR" --strip-components=1
mkdir -p "$DATA_DIR"

# ----- 4) 설치 확인 -----
cd "$INSTALL_DIR"
echo -n "→ 설치 확인: "
python3 -m isilon_usage --version || { echo "✗ 실행 확인 실패."; exit 1; }

# ----- 5) systemd 서비스 등록 -----
UNIT="/etc/systemd/system/${SERVICE}.service"
MB_ARG=""; [ -n "$MOUNT_BASE" ] && MB_ARG="--mount-base $MOUNT_BASE"
echo "→ 서비스 등록: $UNIT"
cat > "$UNIT" <<EOF
[Unit]
Description=Isilon Edge - 디렉터리 사용량 스캐너
After=network.target remote-fs.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 -m isilon_usage serve --data-dir $DATA_DIR --host 0.0.0.0 --port $PORT $MB_ARG
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
echo "✅ 완료 — isilon_edge $VER 설치·서비스 등록·재시작"
echo "   접속 : http://<서버주소>:$PORT/"
echo "   로그 : journalctl -u $SERVICE -f"
echo "   상태 : systemctl status $SERVICE"
if [ -z "$MOUNT_BASE" ]; then
  echo "   ⚠ 보안: 지금은 스캔 허용 경로가 전체입니다."
  echo "      예) sudo bash $0 --mount-base /mnt/isilon  (로 다시 실행)"
fi
echo "   업그레이드 : 이 스크립트를 다시 실행하면 최신으로 교체 후 재시작합니다."
