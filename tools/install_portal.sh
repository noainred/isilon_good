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
#   기본 다운로드 소스는 사내(폐쇄망) 미러다(환경변수 IU_MIRROR_ROOT). 다른 미러면 --base-url 로 준다. 예:
#     http://<사내-미러-호스트>/repository/manager-upgrade/isilon_good/raw/<branch>
# =============================================================================
set -euo pipefail

# ===== 기본 설정 =====
BRANCH="claude/upbeat-bell-cXX8f"          # 다운로드할 git 브랜치
INSTALL_DIR="/opt/isilon_portal"           # 프로그램 설치 경로
DATA_DIR="/data/isilon_portal_data"        # 데이터(노드 레지스트리/복제본/설정)
DL_DIR="/opt"                              # 다운로드(압축본) 저장 경로
TMP_DIR=""                               # 임시 작업 디렉터리(빈값=mktemp -d 로 안전 생성)
PORT="8800"                                # 포탈 포트
SERVICE="isilon-portal"                    # systemd 서비스 이름
GITHUB_TOKEN="${GITHUB_TOKEN:-}"           # (선택) GitHub 비공개 저장소 토큰 — 미러를 쓰면 불필요
REPO="noainred/isilon_good"                # 저장소 이름(미러 경로/토큰 모드에 사용)
MIRROR_ROOT="${IU_MIRROR_ROOT:-}"          # 사내 폐쇄망 미러 루트(회사 비공개 — 소스에 박지 않음). 환경변수 IU_MIRROR_ROOT 또는 --base-url 로 지정
BASE_URL=""                                # 다운로드 베이스 직접 지정(비우면 미러: MIRROR_ROOT/BRANCH)

# ===== 인자로 덮어쓰기 =====
SET_PORT=""; SET_DATA_DIR=""; SET_INSTALL_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port)        PORT="$2"; SET_PORT=1;        shift 2;;
    --branch)      BRANCH="$2";      shift 2;;
    --data-dir)    DATA_DIR="$2"; SET_DATA_DIR=1;    shift 2;;
    --install-dir) INSTALL_DIR="$2"; SET_INSTALL_DIR=1; shift 2;;
    --tmp-dir)     TMP_DIR="$2";     shift 2;;
    --token|--github-token) GITHUB_TOKEN="$2"; shift 2;;
    --base-url|--mirror) BASE_URL="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "알 수 없는 옵션: $1 (도움말: --help)"; exit 1;;
  esac
done

# ===== 재실행(업그레이드) 시 이전 설치 값 유지 =====
# 옵션 없이 다시 실행하면 예전엔 포트·데이터 경로가 기본값으로 바뀌어, 데이터 폴더를 잃은 것처럼
# 빈 상태로 뜨거나 포탈이 엣지를 못 찾았다(2026-11 리뷰). 명시하지 않은 값은 기존 서비스 유닛에서 읽어 쓴다.
UNIT="/etc/systemd/system/${SERVICE}.service"
if [ -f "$UNIT" ] && command -v python3 >/dev/null 2>&1; then
  eval "$(python3 - "$UNIT" <<'PY'
import shlex, sys
prev = {}
try:
    for ln in open(sys.argv[1], encoding="utf-8"):
        ln = ln.strip()
        if ln.startswith("WorkingDirectory="):
            prev["INSTALL_DIR"] = ln.split("=", 1)[1].replace("%%", "%")
        elif ln.startswith("ExecStart="):
            a = [x.replace("%%", "%").replace("$$", "$") for x in shlex.split(ln.split("=", 1)[1])]
            for flag, var in (("--data-dir", "DATA_DIR"), ("--port", "PORT"), ("--mount-base", "MOUNT_BASE")):
                if flag in a and a.index(flag) + 1 < len(a):
                    prev[var] = a[a.index(flag) + 1]
except Exception:
    pass
for k, v in prev.items():
    print("PREV_%s=%s" % (k, shlex.quote(v)))
PY
)"
fi
if [ -z "$SET_PORT" ] && [ -n "${PREV_PORT:-}" ]; then PORT="$PREV_PORT"; fi
if [ -z "$SET_DATA_DIR" ] && [ -n "${PREV_DATA_DIR:-}" ]; then DATA_DIR="$PREV_DATA_DIR"; fi
if [ -z "$SET_INSTALL_DIR" ] && [ -n "${PREV_INSTALL_DIR:-}" ]; then INSTALL_DIR="$PREV_INSTALL_DIR"; fi

# 상대 경로는 절대 경로로(검사 '전'에 — 현재 폴더 경로의 공백도 걸러지게, 23회차 점검) — 아래에서 cd / 뒤 폴더를 바꾸므로 상대 경로면 교체가 실패했다(22회차 점검)
case "$INSTALL_DIR" in /*) ;; *) INSTALL_DIR="$(pwd)/$INSTALL_DIR";; esac
case "$DATA_DIR" in /*) ;; *) DATA_DIR="$(pwd)/$DATA_DIR";; esac
# 설치 경로가 심볼릭 링크면 실제 경로로 — 폴더 바꾸기(mv)가 링크 자체를 실제 폴더로 바꿔 링크 대상엔 옛 코드가 남았다(21회차 점검)
if [ -L "$INSTALL_DIR" ]; then INSTALL_DIR="$(readlink -f "$INSTALL_DIR")"; fi
# systemd 유닛에 그대로 쓰므로 공백·따옴표·역슬래시가 든 경로는 받지 않는다(유닛 파싱이 깨짐).
for _v in "$INSTALL_DIR" "$DATA_DIR"; do
  case "$_v" in *[[:space:]]*|*\"*|*\\*) echo "✗ 경로에 공백/따옴표/역슬래시는 쓸 수 없습니다: $_v" >&2; exit 1;; esac
done
case "$PORT" in ''|*[!0-9]*) echo "✗ --port 는 숫자여야 합니다: $PORT" >&2; exit 1;; esac
SD_INSTALL_DIR="${INSTALL_DIR//%/%%}"; SD_DATA_DIR="${DATA_DIR//%/%%}"
SD_DATA_DIR_X="${SD_DATA_DIR//\$/\$\$}"

RAW_BASE="${BASE_URL:-$MIRROR_ROOT/$BRANCH}"   # 다운로드 베이스(--base-url 또는 환경변수 IU_MIRROR_ROOT/BRANCH)
case "$RAW_BASE" in
  http://*|https://*) ;;
  *) echo "다운로드 베이스가 없습니다 — 환경변수 IU_MIRROR_ROOT 를 사내 미러 루트로 설정하거나 --base-url <URL> 을 주세요." >&2; exit 1;;
esac

echo "============================================================"
echo " isilon_portal(HQ) 설치/업그레이드"
echo "   설치 경로 : $INSTALL_DIR"
echo "   데이터    : $DATA_DIR"
echo "   소스      : $RAW_BASE"
echo "   브랜치    : $BRANCH   포트: $PORT   임시: ${TMP_DIR:-자동(mktemp)}"
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
# 기본은 mktemp -d(예측 불가 이름·0700) — 고정 /tmp 경로를 rm 후 mkdir 하면 그 사이 다른 사용자가
# 같은 이름을 선점해 압축 해제물(.py)을 바꿔치기할 수 있다(root 로 설치·실행됨).
if [ -z "$TMP_DIR" ]; then TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/isilon_portal.XXXXXX")"
else rm -rf "$TMP_DIR"; mkdir -p "$TMP_DIR"; fi
echo "→ 임시 작업: $TMP_DIR"   # (만든 뒤에 출력 — 예전엔 빈 값으로 찍혔다)
tar xzf "$DEST" -C "$TMP_DIR" --strip-components=1
echo -n "→ 패키지 검증: "
( cd "$TMP_DIR" && python3 -m isilon_usage --version ) \
  || { echo "✗ 검증 실패 — 설치 중단(기존 설치 그대로 유지)."; rm -rf "$TMP_DIR"; exit 1; }

# ----- 4) 검증 통과분만 설치 경로로 반영 -----
# (서비스를 멈추기 '전'에 검사 — 예전엔 멈춘 뒤 거부해 서비스가 꺼진 채 끝났다) 데이터 폴더가 설치 폴더 안이면 rsync --delete 가 DB·설정을 통째로 지운다 → 거부.
case "${DATA_DIR%/}/" in
  "${INSTALL_DIR%/}"/*) echo "✗ --data-dir($DATA_DIR)이 설치 폴더($INSTALL_DIR) 안에 있습니다 — 업그레이드 때 삭제됩니다. 다른 경로를 지정하세요."; rm -rf "$TMP_DIR"; exit 1;;
esac
echo "→ 설치: $INSTALL_DIR"
# 새 파일은 설치 폴더 옆 단계 폴더에 '서비스를 멈추기 전에' 다 복사한다 — 예전엔 멈춘 뒤 설치 폴더에 바로 복사해,
# 복사가 중간에 실패하면(디스크 가득 등) 반쯤 덮인 설치로 서비스가 꺼진 채 끝났다(20회차 점검).
NEW_DIR="${INSTALL_DIR%/}.new.$$"; OLD_DIR="${INSTALL_DIR%/}.old.$$"
rm -rf "$NEW_DIR"; mkdir -p "$NEW_DIR" "$DATA_DIR"
if ! cp -a "$TMP_DIR"/. "$NEW_DIR"/; then
  echo "✗ 새 파일 복사 실패(디스크 여유를 확인하세요) — 설치 중단(기존 설치·서비스는 그대로)."
  rm -rf "$NEW_DIR" "$TMP_DIR"; exit 1
fi
chmod 755 "$NEW_DIR"                                  # mktemp 폴더(0700) 권한이 따라오지 않게
find "$NEW_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
for f in info.MD portal_info.MD .iu_data_persist.json; do   # 설치 폴더에 앱이 쓰는 런타임 파일은 옮겨 담는다
  if [ -e "$INSTALL_DIR/$f" ]; then cp -a "$INSTALL_DIR/$f" "$NEW_DIR/" 2>/dev/null || true; fi
done
systemctl stop "$SERVICE" >/dev/null 2>&1 || true     # 파일 교체 안정화
mkdir -p "$DATA_DIR"
cd /                                                  # 설치 폴더 안에서 실행해도 옛 폴더(지워짐)에 머물지 않게
if [ -d "$INSTALL_DIR" ] && ! mv "$INSTALL_DIR" "$OLD_DIR" 2>/dev/null; then
  # 옮길 수 없는 폴더(마운트 지점 등)면 예전처럼 제자리 반영 — 단계 폴더에서 복사
  if command -v rsync >/dev/null 2>&1; then
    # --delete 는 설치 폴더(서비스 WorkingDirectory)에 앱이 쓰는 런타임 파일까지 지운다 → 보호.
    rsync -a --delete --exclude info.MD --exclude portal_info.MD --exclude .iu_data_persist.json "$NEW_DIR"/ "$INSTALL_DIR"/
  else
    cp -a "$NEW_DIR"/. "$INSTALL_DIR"/
    # 릴리스 파일은 수정 시각이 고정(2020-01-01)이라, cp 는 옛 __pycache__ 를 남겨 크기가 같은 모듈의 옛 바이트코드가
    # 그대로 실행됐다(새 버전을 깔아도 옛 버전으로 보고) — 캐시를 지운다(rsync --delete 는 이미 지움).
    find "$INSTALL_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  fi
  rm -rf "$NEW_DIR"
else
  if ! mv "$NEW_DIR" "$INSTALL_DIR"; then             # 이름 바꾸기 실패 — 옛 설치를 되돌리고 서비스를 다시 띄운다
    [ -d "$OLD_DIR" ] && mv "$OLD_DIR" "$INSTALL_DIR"
    systemctl start "$SERVICE" >/dev/null 2>&1 || true
    echo "✗ 새 설치로 바꾸지 못했습니다 — 옛 설치로 되돌렸습니다."; rm -rf "$NEW_DIR" "$TMP_DIR"; exit 1
  fi
  # 옛 폴더를 작업 폴더로 쓰던 다른 프로세스(같은 폴더를 쓰는 엣지·포탈 등)는 지워진 폴더에 남는다 — 알린다(21회차 점검)
  for _p in /proc/[0-9]*; do
    _c="$(readlink "$_p/cwd" 2>/dev/null || true)"
    case "$_c" in "$OLD_DIR"|"$OLD_DIR"/*) echo "   ⚠ 프로세스 ${_p#/proc/} 가 옛 설치 폴더를 쓰고 있습니다 — 그 서비스도 재시작하세요.";; esac
  done
  rm -rf "$OLD_DIR"
fi
rm -rf "$TMP_DIR"
cd "$INSTALL_DIR"

# ----- 5) systemd 서비스 등록 -----
echo "→ 서비스 등록: $UNIT"
cat > "$UNIT" <<EOF
[Unit]
Description=Isilon Portal (HQ) - 글로벌 통합 관제
After=network.target remote-fs.target local-fs.target
# 데이터 폴더가 별도 영속 디스크 마운트면, 그 마운트가 붙은 뒤에 시작한다(재부팅 시 빈 폴더에
# 노드 0·새 토큰으로 초기화되어 노드/설정이 사라지는 사고 방지).
RequiresMountsFor=$SD_DATA_DIR

[Service]
Type=simple
User=root
WorkingDirectory=$SD_INSTALL_DIR
ExecStart=/usr/bin/python3 -m isilon_usage portal --data-dir "$SD_DATA_DIR_X" --host 0.0.0.0 --port $PORT
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
