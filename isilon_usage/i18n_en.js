/* i18n_en.js — The Davinci NAS Management: 한국어 원본 → 영어 토글(드롭인, 단일 소스).
 * 엣지 서버·포탈이 /i18n.js 로 서빙하고, portal.html·dashboard.html 이 <script src="/i18n.js"> 로 공유한다.
 * 마크업/렌더 로직을 수정하지 않고, DOM 텍스트노드·title/placeholder 를 '완결 라벨'만 정확 매칭해 치환한다
 * (사전에 없으면 한글 그대로 폴백 — 어순 깨짐 없음). 언어는 localStorage(iuLang)에 저장, 토글 버튼은 헤더에 주입.
 * 동적 문장(숫자 끼임)은 v1 범위 밖(폴백). 데이터·동작·색상 임계는 무변경. */
(function () {
  "use strict";
  var DICT = {
    // --- 공통/짧은 라벨 ---
    "노드": "Node", "지역": "Region", "상태": "Status", "설정": "Settings", "경로": "Path",
    "파일": "Files", "사용": "Used", "사용량": "Usage", "용량": "Capacity", "스토리지": "Storage",
    "디렉터리": "Directory", "결과": "Result", "요약": "Summary", "시작": "Start", "중지": "Stop",
    "저장": "Save", "닫기": "Close", "✕ 닫기": "✕ Close", "추가": "Add", "조회": "Query", "질문": "Ask",
    "검증": "Verify", "오류": "Error", "손실": "Loss", "동작": "Action", "작업": "Task", "예약": "Schedule",
    "엔진": "Engine", "백엔드": "Backend", "기준": "Basis", "깊이": "Depth", "단계": "Level", "유형": "Type",
    "이름": "Name", "주소": "Address", "계정": "Account", "지연": "Latency", "측정": "Measure", "탐색": "Discovery",
    "판정": "Verdict", "비교": "Compare", "경로 비교": "Path Compare", "분포": "Distribution", "비고": "Note",
    "비중": "Share", "자원": "Resources", "추이": "Trend", "회차": "Run", "루트": "Root", "(루트)": "(root)",
    "현재": "Current", "보기": "View", "하위": "Sub", "전체 노드": "All Nodes", "전체 용량": "Total Capacity",
    "조사 용량": "Scanned", "직접 용량": "Own Size", "재귀 용량": "Recursive Size", "논리 크기": "Logical Size",
    "사용 용량": "Used Capacity", "용량 기준": "Size Basis", "파일 수": "File Count", "총 파일 수": "Total Files",
    "디렉터리 수": "Directory Count", "하위디렉터리": "Subdirectories", "상위 폴더": "Parent Folder",
    "최대 깊이": "Max Depth", "최소/최대": "Min/Max", "최대 대비": "vs Max", "크기 변화": "Size Change",
    "파일수 변화": "File Count Change", "파일(재귀)": "Files (recursive)", "파일 수(재귀)": "File Count (recursive)",
    "소요 시간": "Elapsed", "완료 예상시각": "ETA", "예상 잔여(ETA)": "Remaining (ETA)", "평균 지연": "Avg Latency",
    "무응답 노드": "Unresponsive", "도달 가능 노드": "Reachable Nodes", "노드 온라인": "Nodes Online",
    "진행 중 스캔": "Active Scans", "관리 중 루트 수": "Managed Roots", "루트 (최신 스캔)": "Root (latest scan)",
    "탐색된 디렉터리": "Discovered Dirs", "집계 완료 디렉터리": "Aggregated Dirs", "현재 진행 상황": "Progress",
    "표시 이름(선택)": "Display Name (optional)", "표시할 워커 수": "Workers to show", "표시할 워커 수:": "Workers to show:",
    "표시 구간:": "Window:", "버킷:": "Bucket:", "화면 표시": "Display", "이번 재시작 후": "Since restart",
    "전 노드 합계": "All nodes total", "최근 동기화 기준": "As of last sync", "마지막 스캔": "Last Scan",
    "마지막 동기화": "Last Sync", "스캐너 PID": "Scanner PID", "앱 버전": "App Version",
    "커밋 배치 크기": "Commit batch size", "DB 크기(생존 지표)": "DB size (liveness)",
    "DB 디스크 여유": "DB disk free", "로그 디스크 여유": "Log disk free", "서버 메모리": "Server RAM",
    "스왑 사용": "Swap used", "파일시스템 사용": "Filesystem used", "파일시스템 여유": "Filesystem free",
    "파일시스템 전체": "Filesystem total", "디스크 사용": "Disk used", "마운트 상태": "Mount status",
    "확인된 사용량 / 전체 디스크 사용량": "Scanned usage / total disk usage",
    "조사 용량 합계": "Total scanned", "총 사용 용량 (전 DC)": "Total Used (all DCs)",
    "스토리지(루트)": "Storage (roots)", "시간당 처리용량": "Throughput/hr", "시간당 처리량": "Throughput/hr",
    "상위 디렉터리 표시 개수": "Top directories to show", "오류(접근불가) 디렉터리": "Error (inaccessible) dirs",
    "관리 중 루트": "Managed roots", "누적(fold 시 보존)": "Cumulative (kept when folded)",
    "누적(fold 시 보존)": "Cumulative (kept when folded)", "경과 시간(누적 작업)": "Elapsed (cumulative)",
    "예상 잔여(ETA)": "Remaining (ETA)",
    // --- 시간/단위 ---
    "초": "sec", "분": "min", "1분": "1 min", "5분": "5 min", "10분": "10 min", "30초": "30 sec",
    "5초": "5 sec", "1시간": "1 h", "1일": "1 day", "7일": "7 days", "30일": "30 days", "90일": "90 days",
    "365일": "365 days", "24시간": "24 h", "분마다": "per min", "분 마다": "per min", "시간 마다": "per hour",
    "일 마다": "per day", "주 마다": "per week", "개월 마다": "per month", "반복주기": "Interval", "복제 주기": "Replication interval",
    "반복 단위": "Repeat unit", "복제 단위": "Replication unit", "반복 간격(Y)": "Interval (every N)",
    "자원 샘플링 주기(초)": "Resource sampling interval (s)", "대시보드 새로고침(ms)": "Dashboard refresh (ms)",
    "응답 대기(초)": "Response timeout (s)", "실행 시각": "Run time", "시작 월": "Start month", "시작 일": "Start day",
    "🔄 5초 자동": "🔄 auto 5s", "자동(1.5s)": "auto (1.5s)", "🔄 지금 확인": "🔄 Check now",
    // --- 버튼/액션 ---
    "새로고침": "Refresh", "↻ 새로고침": "↻ Refresh", "🔄 새로고침": "🔄 Refresh", "설정 저장": "Save settings",
    "일정 저장": "Save schedule", "제목 저장": "Save title", "토큰 저장": "Save token", "감시 폴더 저장": "Save watch folder",
    "릴리스 폴더 저장": "Save release folder", "저장(추가/수정)": "Save (add/edit)", "설정/변경": "Set / change",
    "분석 시작": "Start analysis", "생성 시작": "Start generation", "점검 실행": "Run check", "지금 진단": "Diagnose now",
    "이력 보기": "View history", "이력 비우기": "Clear history", "연결 테스트": "Test connection", "예시 채우기": "Fill example",
    "중복 찾기": "Find duplicates", "계획·스크립트 만들기": "Build plan / script", "1개 적용": "Apply one",
    "전체 일괄 적용": "Apply to all", "로그인/로그아웃": "Log in / out", "🔑 로그인": "🔑 Log in",
    "🔓 비밀번호 해제": "🔓 Remove password", "🔓 해제": "🔓 Remove", "예약 추가": "Add schedule",
    "＋ 경로": "+ Path", "＋ 경로 추가": "+ Add path", "🏠 루트": "🏠 Root", "🏠 루트로": "🏠 Root", "🏠 루트로": "🏠 To root",
    "🔍 검색 루트로": "🔍 Search root", "검색 루트로": "To search root", "📁 폴더": "📁 Folder", "📁 찾아보기": "📁 Browse",
    "📁 지금 저장": "📁 Save now", "📋 복사": "📋 Copy", "클릭하면 복사": "Click to copy", "⬆ 위로": "⬆ Up",
    "⬆ 가져오기": "⬆ Import", "⬆ 지금 업그레이드": "⬆ Upgrade now", "⬆ 자동 업그레이드": "⬆ Auto upgrade",
    "⬇ 백업 다운로드": "⬇ Download backup", "⬇ 샘플 CSV 받기": "⬇ Sample CSV", "⬇ 현재 구성 내보내기": "⬇ Export config",
    "CSV 내보내기": "Export CSV", "JSON 내보내기": "Export JSON", "되돌리기(다시 불러오기)": "Reset (reload)",
    "예시 경로": "Example path", "예약 시차 배치": "Stagger schedules", "예약 시차 배치": "Stagger schedules",
    "전체 노드": "All nodes", "이 디렉터리 선택 ↓": "Select this directory ↓", "▶ 이 디렉터리 스캔 시작": "▶ Scan this directory",
    "▶ 전체 노드 스캔 시작": "▶ Start scan on all nodes", "전 노드 지금 업그레이드": "Upgrade all nodes now",
    "전 노드 SSH 업그레이드": "SSH-upgrade all nodes", "📡 Ping 체크": "📡 Ping check", "폴더 탐색으로 선택": "Pick via folder browser",
    "이 디렉터리 선택": "Select this directory", "🔬 오토튜닝": "🔬 Auto-tune",
    // --- 상태값/옵션 ---
    "사용 안 함": "Disabled", "끔(수동)": "Off (manual)", "검증 안 함": "No verify", "검증": "Verify",
    "복제만": "Replicate only", "폴링만": "Poll only", "복제 + 폴링(권장)": "Replicate + poll (recommended)",
    "systemd(권장)": "systemd (recommended)", "nohup(간단)": "nohup (simple)", "설치 방식": "Install method",
    "오토튜닝(최적 자동)": "Auto-tune (best)", "threads (상세 트리)": "threads (detailed tree)",
    "native (빠름·저메모리)": "native (fast, low-memory)", "du (du 프로세스 메모리 표시)": "du (shows du process memory)",
    "pscan (멀티프로세스·빠른 용량)": "pscan (multiprocess, fast size)", "기본 백엔드": "Default backend",
    "기본 용량 기준": "Default size basis", "디스크 점유(du 기본)": "Disk usage (du, default)", "논리 크기": "Logical size",
    "기본 한 파일시스템(-x)": "Default one filesystem (-x)", "마운트 읽기전용(ro) 확인": "Check mount read-only (ro)",
    "사용(시작 시 검사·표시)": "On (check at start)", "mtime(수정시각·권장)": "mtime (modified, recommended)",
    "atime(접근시각)": "atime (accessed)", "— 노드 선택 —": "— select node —", "진행 중 노드:": "Running nodes:",
    "대상 노드(전체 또는 개별)": "Target nodes (all or individual)", "대상 노드(개별 선택 → 1개 적용)": "Target node (pick one)",
    "드롭다운에서 고른 노드 1개에 적용": "Apply to one selected node", "인증서 검증(자체 서명이면 끔)": "Verify certificate (off if self-signed)",
    "비밀번호 보호 사용(체크 해제 후 저장하면 보호 끔)": "Enable password protection (uncheck + save to disable)",
    "하드링크 중복 제거(끄면 메모리 절약·수십억 파일 대비)": "Hardlink dedup (off saves memory for billions of files)",
    // --- 헤더/탭/섹션(내비) ---
    "글로벌 대시보드": "Global Dashboard", "네트워크 모니터링": "Network Monitoring", "지역별 스토리지 현황": "Storage by Region",
    "전체 스토리지 용량 (전 DC)": "Total Storage Capacity (all DCs)",
    "📈 처리량 추이": "📈 Throughput", "📊 분석 리포트": "📊 Analysis Report", "📁 디렉터리 분석": "📁 Directory Analysis",
    "🧭 대상 분석": "🧭 Target Analysis", "🧩 추가 기능": "🧩 Extras", "🔬 스캔·성능": "🔬 Scan · Performance",
    "🔔 알림·예약": "🔔 Alerts · Schedule", "🔒 보안·연동": "🔒 Security · Integration", "🔧 트러블슈팅": "🔧 Troubleshoot",
    "📖 버전 기록": "📖 Version History", "📋 스캔 이력": "📋 Scan History", "📋 노드 관리": "📋 Node Management",
    "📉 추세·비교": "📉 Trend · Compare", "🗄 스토리지": "🗄 Storage", "ℹ️ 소개": "ℹ️ About", "⚙ 설정": "⚙ Settings",
    "⚙ 일반": "⚙ General", "🔑 계정 · 보안": "🔑 Account · Security", "🩺 트러블슈팅 — 어느 구간이 느린가": "🩺 Troubleshoot — where is it slow",
    "📈 모니터링 · 백업": "📈 Monitoring · Backup", "🔌 외부 사용량 API (집계 데이터 제공)": "🔌 External Usage API (aggregated data)",
    "🔐 노드 작업 비밀번호 (포탈에서 설정)": "🔐 Node op password (set from portal)",
    "🔑 엣지 자기등록 토큰 (enroll token)": "🔑 Edge enroll token", "🏷 포탈 제목 (브랜딩)": "🏷 Portal title (branding)",
    // --- 카드 제목(주요) ---
    "🧬 중복 파일 찾기 (내용 해시 — 회수 가능 용량)": "🧬 Find Duplicate Files (content hash — reclaimable)",
    "❄ 콜드데이터 이동 계획 (오래 안 쓴 파일 → 별도 저장소)": "❄ Cold-data Move Plan (old files → separate store)",
    "🧩 추가 기능 — 폴더 마지막 접근시각(atime)": "🧩 Extras — folder last access time (atime)",
    "🕒 파일 나이(수정 mtime)": "🕒 File Age (modified mtime)", "🧊 마지막 접근 나이(atime)": "🧊 Last-access Age (atime)",
    "👤 소유자별 사용량 Top": "👤 Top Usage by Owner", "🗂 확장자별 사용량 Top": "🗂 Top Usage by Extension",
    "🐘 최대 파일 Top": "🐘 Largest Files", "📏 파일 크기별 분포": "📏 File Size Distribution",
    "📈 용량 소진 예측": "📈 Capacity Forecast", "🔀 변화 Top (직전 스캔 대비)": "🔀 Top Changes (vs previous scan)",
    "디스크 사용량 파이 — 디렉터리를 클릭해 더 깊이 들여다보기": "Disk usage pie — click a directory to drill down",
    "디스크 사용량 트리맵 — 면적이 곧 용량(클릭해 더 깊이)": "Disk usage treemap — area = size (click to drill down)",
    "용량 상위 디렉터리 (실시간)": "Top Directories by Size (live)", "스토리지 어레이 상태 (Isilon / PowerStore)": "Storage Array Status (Isilon / PowerStore)",
    "💬 물어보기": "💬 Ask", "💬 질의응답(자연어) — 로컬 LLM": "💬 Q&A (natural language) — local LLM",
    "📊 법인별 시간당 처리량": "📊 Throughput per Entity (per hour)",
    "🔧 노드 토큰 강제 맞추기 (포탈 저장값 교정)": "🔧 Force-match Node Token (fix portal value)",
    "🔧 튜닝 점검 — OS·마운트 설정으로 스캔 가속": "🔧 Tuning Check — speed up scans via OS/mount",
    "🚀 원격 자동 구성 — IP만 넣으면 엣지 설치 명령 생성 + 노드 자동 등록": "🚀 Remote Provisioning — enter IP to generate install + auto-register",
    "🔄 원격 버전 업그레이드 — 엣지를 HQ 최신 코드로": "🔄 Remote Version Upgrade — edges to HQ latest",
    "🌐 인터넷(URL) 자동 업그레이드 — 포탈 + 전 엣지": "🌐 Internet (URL) Auto-upgrade — portal + all edges",
    "⬆ 자동 업그레이드 — 인터넷(GitHub) 모니터링": "⬆ Auto-upgrade — internet (GitHub) monitoring",
    "🔁 자동 업그레이드 (감시 폴더 + 전 노드)": "🔁 Auto-upgrade (watch folder + all nodes)",
    "📦 엣지 오프라인 업그레이드 (포탈 릴리스 폴더 → 엣지가 당겨감)": "📦 Edge Offline Upgrade (portal release folder → edges pull)",
    "📜 업그레이드 기록 (History)": "📜 Upgrade History", "🧪 테스트 데이터 생성": "🧪 Generate Test Data",
    "🧪 테스트 데이터": "🧪 Test Data", "💾 백업 (포탈 설정 + 노드 목록)": "💾 Backup (portal settings + node list)",
    "🔒 접속 보안 (로그인 비밀번호)": "🔒 Access Security (login password)",
    "🩺 인프라 체크 (서버 Ping)": "🩺 Infra Check (server ping)", "NAS별 현황 — 한 서버에서 여러 NAS 점검": "Per-NAS Status — check many NAS from one server",
    "통합 디렉터리 매트릭스 — 행=디렉터리, 열=노드 · 클릭하면 더 깊이": "Unified Directory Matrix — rows=dir, cols=node · click to drill down",
    "🔒 접속 보안 (로그인 비밀번호)": "🔒 Access Security (login password)",
    // --- 소개/브랜딩 ---
    "저작자 · AUTHOR": "Author", "저작권 · COPYRIGHT": "Copyright", "주요 기능 · KEY FEATURES": "Key Features",
    "Python · 표준 라이브러리": "Python · standard library", "© 2026 박준호": "© 2026 Park Junho",
    "The Davinci Platform · 전세계 분산 아이실론 사용량 통합 관제": "The Davinci Platform · unified Isilon usage control across sites",
    "Isilon 디렉터리 사용량 스캐너": "Isilon Directory Usage Scanner", "글로벌 통합 포탈(HQ) — DB 복제 인증": "Global Portal (HQ) — DB replication auth",
    // --- 폼/입력/속성(placeholder·title) ---
    "비밀번호": "Password", "새 비밀번호": "New password", "새 비밀번호(변경 시에만)": "New password (only if changing)",
    "인증토큰": "Auth token", "토큰(선택)": "Token (optional)", "API 토큰(비우면 공개)": "API token (public if empty)",
    "(미설정)": "(unset)", "(자동 생성)": "(auto-generated)", "(변경 시에만)": "(only if changing)", "(변경 안 함)": "(unchanged)",
    "(공용 비번)": "(shared password)", "(노드 id)": "(node id)", "(IP 기반)": "(IP-based)", "(키 인증 시 비움)": "(empty if key auth)",
    "작업 보호": "Op protection", "표시할 워커 수": "Workers to show", "모델: qwen2.5:7b": "Model: qwen2.5:7b",
    "예: qwen2.5:7b": "e.g. qwen2.5:7b", "엔드포인트: http://127.0.0.1:11434/v1": "Endpoint: http://127.0.0.1:11434/v1",
    "GitHub PAT 등 — 공개면 비움": "GitHub PAT etc — empty if public", "enroll 공유 토큰": "enroll shared token",
    "엣지 api_token": "edge api_token", "경로 검색 후 Enter": "Type a path, press Enter", "경로 입력 또는 📁 로 탐색": "Enter a path or browse 📁",
    "스캔했던 루트 중에서 선택": "Pick from previously scanned roots", "스캔 이력에서 선택…": "Pick from scan history…",
    "부제 (예: 한국 HQ) — 선택": "Subtitle (e.g. Korea HQ) — optional", "표시 이름(선택)": "Display name (optional)",
    "/mnt/hadoop/예시/폴더": "/mnt/hadoop/example/folder", "/mnt/hadoop 처럼 분석할 경로": "path to analyze, e.g. /mnt/hadoop",
    "예: /mnt/isilon/ifs/project": "e.g. /mnt/isilon/ifs/project", "원본 루트  /mnt/hadoop/예시/폴더": "source root  /mnt/hadoop/example/folder",
    "이동 대상(콜드 저장소)  /mnt/cold/…": "target (cold store)  /mnt/cold/…", "/data/iutest 처럼 쓰기 가능한 경로": "a writable path, e.g. /data/iutest",
    "예: /opt/isilon_releases": "e.g. /opt/isilon_releases", "서버에 저장할 폴더 (예: /opt/isilon_backups)": "folder on server (e.g. /opt/isilon_backups)",
    "https://raw… 또는 사내 미러/사설 레포 폴더 주소": "https://raw… or internal mirror/private repo folder",
    "제목 (예: 다빈치 글로벌 아이실론 관제센터)": "Title (e.g. Davinci Global Isilon Control Center)",
    "대상 노드(전체 또는 개별)": "Target nodes (all or individual)", "대상 경로 로딩 중…": "Loading target path…",
    "로딩 중…": "Loading…", "불러오는 중…": "Loading…", "상태 불러오는 중…": "Loading status…", "시작하는 중…": "Starting…",
    "로그인": "Log in", "로그아웃": "Log out",
    // --- 안내/설명(완결문) ---
    "누가 공간을 쓰는지(차지백/쇼백).": "Who uses the space (chargeback/showback).",
    "무엇이 공간을 쓰는지(로그·이미지·백업 등).": "What uses the space (logs, images, backups, etc.).",
    "가장 큰 파일(경로·크기·수정시각·소유자) — 정리 1순위 후보.": "Largest files (path, size, mtime, owner) — top cleanup candidates.",
    "가장 많이 커진/줄어든/신규/삭제 디렉터리.": "Most grown/shrunk/new/deleted directories.",
    "상단에 경로를 입력하고 “탐색”을 누르세요.": "Enter a path above and press “Explore”.",
    "폴더 경로를 입력하고 ‘중복 찾기’를 누르세요. 회수 가능 용량이 큰 순으로 표시됩니다.": "Enter a folder path and press ‘Find duplicates’. Sorted by reclaimable space.",
    "닫아도 전파는 백그라운드로 계속됩니다.": "Closing this keeps propagation running in the background.",
    "닫아도 업그레이드는 백그라운드로 계속됩니다.": "Closing this keeps the upgrade running in the background.",
    "설정이 잠겨 있습니다(--lock-settings). 읽기 전용입니다.": "Settings are locked (--lock-settings). Read-only.",
    "The Davinci Platform · 초대용량 NAS·아이실론 디렉터리 사용량 조사 · 분석": "The Davinci Platform · very-large NAS/Isilon directory usage survey · analysis",
    "지역별 요약, 노드 온라인 상태, 용량 큰 루트/노드 순위": "Per-region summary, node online status, top roots/nodes by size",
    "전세계 분산 노드의 루트·용량·파일시스템·스캔을 단일 화면에서 집계": "Aggregate roots, capacity, filesystems and scans of globally distributed nodes on one screen",
    // --- 추가 커버리지(눈에 잘 띄는 것) ---
    "— 전사 집계를 문장으로 물어보면 답합니다": "— ask about org-wide totals in a sentence",
    "— 전사 집계를 문장으로 물어보면 답합니다 LLM": "— ask about org-wide totals in a sentence LLM",
    "데이터센터별 사용량 — 행 클릭=스토리지 펼침 · 지역 카드=필터 · 머리글=정렬": "Usage by Data Center — click row = expand storage · region card = filter · header = sort",
    "한 파일시스템(-x)": "One filesystem (-x)", "기본 한 파일시스템(-x)": "Default one filesystem (-x)",
    "안 하는 노드만": "Idle nodes only", "멈추고 다시 시작": "Stop and restart",
    "예: 전체 용량 얼마야? · 어느 노드가 제일 커? · 지금 스캔 진행 중이야?": "e.g. What's the total capacity? · Which node is largest? · Any scan running now?",
    "노드": "Node", "지역": "Region", "스토리지": "Storage", "조사 용량": "Scanned", "전체 용량": "Total Capacity",
    "마지막 스캔": "Last Scan", "상태": "Status", "온라인": "Online", "오프라인": "Offline", "스캔 중": "Scanning",
    "사용 안 함": "Disabled", "열기 ↗": "Open ↗", "▶ 스캔 시작": "▶ Start scan", "🔒 스캔 시작": "🔒 Start scan",
    "집계 진행률": "Aggregation", "구버전": "Outdated", "반복 동작 중": "repeating",
    "엔진": "Engine", "Engine": "Engine", "새 스캔 시작 — 조사할 디렉터리 지정": "New Scan — pick a directory to survey",
    "마지막 갱신: 방금": "Last updated: just now"
  };

  var LANG = "ko";
  try { LANG = localStorage.getItem("iuLang") || "ko"; } catch (e) {}
  var applying = false;

  function repl(node) {
    var p = node.parentNode;
    if (!p) return;
    var pn = p.nodeName;
    if (pn === "SCRIPT" || pn === "STYLE" || pn === "NOSCRIPT") return;
    var raw = node.nodeValue, s = raw.trim();
    if (!s) return;
    var e = DICT[s];
    if (e != null && e !== s) node.nodeValue = raw.replace(s, e);
  }
  function walk(root) {
    if (LANG !== "en" || !root) return;
    applying = true;
    try {
      if (root.nodeType === 3) { repl(root); }
      else if (root.querySelectorAll) {
        var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
        var list = [], n;
        while ((n = w.nextNode())) list.push(n);
        list.forEach(repl);
        var els = root.querySelectorAll("[title],[placeholder]");
        for (var i = 0; i < els.length; i++) {
          var el = els[i];
          ["title", "placeholder"].forEach(function (a) {
            var v = el.getAttribute(a); if (!v) return;
            var t = DICT[v.trim()]; if (t != null) el.setAttribute(a, t);
          });
        }
      }
    } finally { applying = false; }
  }
  function observe() {
    if (!window.MutationObserver) return;
    var obs = new MutationObserver(function (muts) {
      if (applying || LANG !== "en") return;
      for (var i = 0; i < muts.length; i++) {
        var an = muts[i].addedNodes;
        for (var j = 0; j < an.length; j++) walk(an[j]);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }
  function mountToggle() {
    var h = document.querySelector("header");
    if (!h || document.getElementById("iuLangBtn")) return;
    var b = document.createElement("button");
    b.id = "iuLangBtn"; b.type = "button";
    b.textContent = (LANG === "en") ? "한국어" : "EN";
    b.title = "언어 전환 / Language";
    b.style.cssText = "margin-left:6px; flex:none;";
    b.onclick = function () {
      var nx = (LANG === "en") ? "ko" : "en";
      try { localStorage.setItem("iuLang", nx); } catch (e) {}
      location.reload();
    };
    h.appendChild(b);
  }
  function init() {
    mountToggle();
    if (LANG === "en") { walk(document.body); observe(); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
