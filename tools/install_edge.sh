#!/usr/bin/env bash
# =============================================================================
# isilon_edge 자동 설치/업그레이드 스크립트
#   - 최신 패키지를 다운받아 /opt/isilon_edge 에 설치하고
#     systemd 서비스(isilon-edge)로 등록한 뒤 재시작한다.
#   - 다시 실행하면 최신 버전으로 교체 후 재시작(업그레이드)된다.
#
# 사용:  sudo bash install_edge.sh [--port 8765] [--mount-base /mnt/isilon]
#                                  [--branch <git브랜치>] [--data-dir DIR] [--install-dir DIR]
#                                  [--tmp-dir DIR] [--api-token TOKEN] [--base-url <미러베이스>]
#   기본 다운로드 소스는 사내(폐쇄망) 미러다. 다른 미러를 쓰려면 --base-url 로 베이스를 준다.
#   --api-token : 포탈 연동/업그레이드 푸시 인증 토큰(생략하면 기존 유지·없으면 자동 생성)
#   --base-url U: 다운로드 베이스 URL. 비우면 사내 미러(환경변수 IU_MIRROR_ROOT/<branch>)를 쓴다. 예:
#                 http://<사내-미러-호스트>/repository/manager-upgrade/isilon_good/raw/<branch>
#   --hq URL    : HQ 포탈 주소(예: http://10.0.0.5:8800). 주면 설치 후 이 엣지를
#                 포탈에 자동 등록(enroll)한다. --region/--node-id/--enroll/--advertise-host 동반 가능.
#   --enroll T  : 포탈 enroll 공유 토큰(포탈 설정 → 설치·구성에서 정한 값 — 자기등록에 필요).
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
TMP_DIR=""                               # 임시 작업 디렉터리(빈값=mktemp -d 로 안전 생성)
API_TOKEN=""                             # 포탈 연동/업그레이드 푸시 인증 토큰(비우면 자동 생성·유지)
GITHUB_TOKEN="${GITHUB_TOKEN:-}"         # (선택) GitHub 비공개 저장소 토큰 — 미러를 쓰면 불필요
REPO="noainred/isilon_good"              # 저장소 이름(미러 경로/토큰 모드에 사용)
MIRROR_ROOT="${IU_MIRROR_ROOT:-}"        # 사내 폐쇄망 미러 루트(회사 비공개 — 소스에 박지 않음). 환경변수 IU_MIRROR_ROOT 또는 --base-url 로 지정
BASE_URL=""                              # 다운로드 베이스 직접 지정(비우면 미러: MIRROR_ROOT/BRANCH)
HQ=""                                    # 포탈(HQ) URL — 주면 설치 후 포탈에 자기등록(enroll)
REGION=""                                # 노드 지역 라벨(표시용)
NODE_ID=""                               # 노드 id(비우면 hostname)
ENROLL_TOKEN=""                          # 포탈 enroll 공유 토큰(자기등록에 필요 — 포탈 설정에서 정함)
ADVERTISE_HOST=""                        # 포탈이 이 엣지를 찾아올 IP/호스트(비우면 자동 감지)

# ===== 인자로 덮어쓰기 =====
SET_PORT=""; SET_DATA_DIR=""; SET_INSTALL_DIR=""; SET_MOUNT_BASE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port)        PORT="$2"; SET_PORT=1;        shift 2;;
    --mount-base)  MOUNT_BASE="$2"; SET_MOUNT_BASE=1;  shift 2;;
    --branch)      BRANCH="$2";      shift 2;;
    --data-dir)    DATA_DIR="$2"; SET_DATA_DIR=1;    shift 2;;
    --install-dir) INSTALL_DIR="$2"; SET_INSTALL_DIR=1; shift 2;;
    --tmp-dir)     TMP_DIR="$2";     shift 2;;
    --api-token)   API_TOKEN="$2";   shift 2;;
    --token|--github-token) GITHUB_TOKEN="$2"; shift 2;;
    --hq|--portal) HQ="$2";          shift 2;;
    --region)      REGION="$2";      shift 2;;
    --node-id|--name) NODE_ID="$2";  shift 2;;
    --enroll|--enroll-token) ENROLL_TOKEN="$2"; shift 2;;
    --advertise-host) ADVERTISE_HOST="$2"; shift 2;;
    --base-url|--mirror) BASE_URL="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "알 수 없는 옵션: $1 (도움말: --help)"; exit 1;;
  esac
done

# ===== 재실행(업그레이드) 시 이전 설치 값 유지 =====
# 옵션 없이 다시 실행하면 예전엔 포트·데이터 경로·스캔 허용 경로가 기본값으로 바뀌어, 데이터 폴더를 잃은 것처럼
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
if [ -z "$SET_MOUNT_BASE" ] && [ -n "${PREV_MOUNT_BASE:-}" ]; then MOUNT_BASE="$PREV_MOUNT_BASE"; fi
# systemd 유닛에 그대로 쓰므로 공백·따옴표·역슬래시가 든 경로는 받지 않는다(유닛 파싱이 깨짐).
for _v in "$INSTALL_DIR" "$DATA_DIR" "$MOUNT_BASE"; do
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
echo " isilon_edge 설치/업그레이드"
echo "   설치 경로 : $INSTALL_DIR"
echo "   데이터    : $DATA_DIR"
echo "   소스      : $RAW_BASE"
echo "   브랜치    : $BRANCH   포트: $PORT"
[ -n "$HQ" ] && echo "   포탈(HQ)  : $HQ   (설치 후 자기등록)"
echo "============================================================"

# ----- 사전 점검 -----
[ "$(id -u)" = "0" ] || { echo "✗ root 권한이 필요합니다. sudo 로 실행하세요."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "✗ python3 가 필요합니다(3.6+)."; exit 1; }
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
  || { echo "✗ curl 또는 wget 이 필요합니다."; exit 1; }

# 다운로드 함수: FETCH <dest> <relpath>   (relpath 예: download/versions.json)
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

# 다운로드가 막힐 때 안내
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
# 무결성 간단 확인(정상 gzip tar 인지)
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
# 기본은 mktemp -d(예측 불가 이름·0700) — 고정 /tmp 경로를 rm 후 mkdir 하면 그 사이 다른 사용자가
# 같은 이름을 선점해 압축 해제물(.py)을 바꿔치기할 수 있다(root 로 설치·실행됨).
if [ -z "$TMP_DIR" ]; then TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/isilon_edge.XXXXXX")"
else rm -rf "$TMP_DIR"; mkdir -p "$TMP_DIR"; fi
tar xzf "$DEST" -C "$TMP_DIR" --strip-components=1
echo -n "→ 패키지 검증: "
( cd "$TMP_DIR" && python3 -m isilon_usage --version ) \
  || { echo "✗ 검증 실패 — 설치 중단(기존 설치 그대로 유지)."; rm -rf "$TMP_DIR"; exit 1; }

# ----- 4) 검증 통과분만 설치 경로로 반영 -----
echo "→ 설치: $INSTALL_DIR"
# 파일 교체 전에 기존 엣지를 안전하게 중단(포트 충돌 방지):
#  현재 서비스 + 구버전 isilon_usage 서비스(있으면 유닛까지 제거) + 잔여 serve 프로세스
systemctl stop "$SERVICE" >/dev/null 2>&1 || true
if systemctl list-unit-files 2>/dev/null | grep -q '^isilon_usage\.service'; then
  systemctl disable --now isilon_usage >/dev/null 2>&1 || true
fi
if [ -f /etc/systemd/system/isilon_usage.service ]; then
  rm -f /etc/systemd/system/isilon_usage.service
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
if pgrep -f 'isilon_usage serve' >/dev/null 2>&1; then
  pkill -f 'isilon_usage serve' >/dev/null 2>&1 || true
  sleep 1
fi
# 데이터 폴더가 설치 폴더 안이면 rsync --delete 가 DB·설정을 통째로 지운다 → 거부.
case "${DATA_DIR%/}/" in
  "${INSTALL_DIR%/}"/*) echo "✗ --data-dir($DATA_DIR)이 설치 폴더($INSTALL_DIR) 안에 있습니다 — 업그레이드 때 삭제됩니다. 다른 경로를 지정하세요."; rm -rf "$TMP_DIR"; exit 1;;
esac
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
if command -v rsync >/dev/null 2>&1; then
  # --delete 는 설치 폴더(서비스 WorkingDirectory)에 앱이 쓰는 런타임 파일까지 지운다 → 보호.
  rsync -a --delete --exclude info.MD --exclude portal_info.MD --exclude .iu_data_persist.json "$TMP_DIR"/ "$INSTALL_DIR"/
else
  cp -a "$TMP_DIR"/. "$INSTALL_DIR"/
fi
rm -rf "$TMP_DIR"
cd "$INSTALL_DIR"

# ----- 4.5) api_token 보장(포탈 연동·'전 노드 업그레이드' 푸시 인증) -----
#   포탈에서 엣지로 업그레이드를 푸시하려면 엣지에 api_token 이 있어야 한다(없으면 403).
#   --api-token 으로 주면 그 값을, 없으면 기존 값을 유지, 그것도 없으면 새로 생성한다.
SETTINGS="$DATA_DIR/settings.json"
TOKEN="$(API_TOKEN="$API_TOKEN" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os, secrets, shutil
sp = os.environ["SETTINGS"]
want = (os.environ.get("API_TOKEN") or "").strip()
# 설정 읽기: 본 파일이 깨졌으면 .bak(앱이 남긴 직전 정상본) — 빈 값으로 덮어 비밀번호·토큰을 잃지 않게
s = None
for p in (sp, sp + ".bak"):
    try:
        with open(p, encoding="utf-8") as fh:
            v = json.load(fh)
        if isinstance(v, dict):
            s = v
            break
    except Exception:
        continue
if s is None:
    if os.path.exists(sp):           # 둘 다 못 읽으면 덮기 전에 원본을 보관
        shutil.copyfile(sp, sp + ".broken")
    s = {}
tok = want or str(s.get("api_token") or "").strip() or secrets.token_hex(16)
s["api_token"] = tok
# 쓰기: 앱(settings.write_json_atomic)과 같은 규칙 — 처음부터 소유자만(0600), 원자적 교체
tmp = sp + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(s, fh, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, sp)
print(tok)
PY
)"
[ -n "$TOKEN" ] && echo "→ api_token 설정 완료: $SETTINGS"

# ----- 5) systemd 서비스 등록 -----
MB_ARG=""; if [ -n "$MOUNT_BASE" ]; then _mb="${MOUNT_BASE//%/%%}"; MB_ARG="--mount-base \"${_mb//\$/\$\$}\""; fi
echo "→ 서비스 등록: $UNIT"
cat > "$UNIT" <<EOF
[Unit]
Description=Isilon Edge - 디렉터리 사용량 스캐너
After=network.target remote-fs.target local-fs.target
# 데이터 폴더가 별도 영속 디스크 마운트면, 그 마운트가 붙은 뒤에 시작한다(재부팅 시 빈 폴더에
# 새 토큰·빈 상태로 초기화되어 노드/토큰/설정이 사라지는 사고 방지).
RequiresMountsFor=$SD_DATA_DIR

[Service]
Type=simple
User=root
WorkingDirectory=$SD_INSTALL_DIR
ExecStart=/usr/bin/python3 -m isilon_usage serve --data-dir "$SD_DATA_DIR_X" --host 0.0.0.0 --port $PORT $MB_ARG
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

# ----- 7) 포탈(HQ) 자기등록(enroll) — --hq 가 주어진 경우 -----
if [ -n "$HQ" ]; then
  HQ="${HQ%/}"
  ADV="$ADVERTISE_HOST"
  [ -z "$ADV" ] && ADV="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -z "$ADV" ] && ADV="$(hostname 2>/dev/null)"
  NID="$NODE_ID"; [ -z "$NID" ] && NID="$(hostname 2>/dev/null)"
  NODE_URL="http://$ADV:$PORT"
  echo "→ 포탈 자기등록: $HQ  (이 엣지 $NODE_URL, id=$NID)"
  # JSON 은 python3 로 안전하게 직렬화(셸 주입 방지)
  BODY="$(NID="$NID" NODE_URL="$NODE_URL" TOK="$TOKEN" REGION="$REGION" ENROLL_TOKEN="$ENROLL_TOKEN" MB="$MOUNT_BASE" python3 - <<'PY'
import json, os
print(json.dumps({
    "id": os.environ["NID"], "url": os.environ["NODE_URL"],
    "token": os.environ.get("TOK", ""), "region": os.environ.get("REGION", ""),
    "enroll_token": os.environ.get("ENROLL_TOKEN", ""), "path": os.environ.get("MB", ""),
}))
PY
)"
  if command -v curl >/dev/null 2>&1; then
    RESP="$(curl -fsS -X POST -H 'Content-Type: application/json' -d "$BODY" "$HQ/api/portal/enroll" 2>&1)" && OK=1 || OK=0
  else
    RESP="$(wget -qO- --header='Content-Type: application/json' --post-data="$BODY" "$HQ/api/portal/enroll" 2>&1)" && OK=1 || OK=0
  fi
  if [ "$OK" = "1" ]; then
    echo "   ✓ 포탈 등록됨: $RESP"
  else
    echo "   ⚠ 포탈 등록 실패(엣지 설치 자체는 정상). 포탈 주소/네트워크/enroll_token 확인:"
    echo "     $RESP"
    echo "     수동 등록: 포탈 '노드 설정'에서 url=$NODE_URL, API 토큰=$TOKEN"
  fi
fi

echo
echo "✅ 완료 — isilon_edge $VER 설치·서비스 등록·재시작"
echo "   접속 : http://<서버주소>:$PORT/"
echo "   로그 : journalctl -u $SERVICE -f"
echo "   상태 : systemctl status $SERVICE"
if [ -n "$TOKEN" ]; then
  echo "   API 토큰 : $TOKEN"
  if [ -n "$HQ" ]; then
    echo "      ↳ 위 토큰으로 포탈에 자기등록했습니다(폴링이 바로 인증됨)."
  else
    echo "      ↳ 포탈에 자동 등록하려면 다시 실행에 --hq http://<HQ-IP>:8800 을 추가하세요."
    echo "        (수동이라면 포탈 '노드 설정'에서 위 토큰을 입력)"
  fi
fi
if [ -z "$MOUNT_BASE" ]; then
  echo "   ⚠ 보안: 지금은 스캔 허용 경로가 전체입니다."
  echo "      예) sudo bash $0 --mount-base /mnt/isilon  (로 다시 실행)"
fi
echo "   업그레이드 : 이 스크립트를 다시 실행하면 최신으로 교체 후 재시작합니다."
