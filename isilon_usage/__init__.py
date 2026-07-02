"""isilon_usage — 초대용량 NAS 디렉터리 사용량 조사 도구.

메모리를 최소로 쓰면서 디렉터리별 파일 수/용량을 조사하고, 진행 상황과
서버 자원(특히 메모리/du 프로세스 메모리)을 웹 대시보드로 보여준다.
"""

# 애플리케이션 버전(릴리즈노트 CHANGELOG.md 와 git 태그 v<버전>에 대응)
__version__ = "1.99.20"

# DB 스키마 버전. 스키마가 바뀌면 1씩 올리고 마이그레이션을 추가한다.
# (per-run DB / manager DB 의 PRAGMA user_version 에 기록된다)
# 3: scan_runs 에 workers/active_workers(병렬 워커 수) 컬럼 추가
# 4: scan_runs 에 mount_readonly(대상 마운트 읽기전용 여부) 컬럼 추가
# 5: scan_runs 에 worker_dirs(워커별 현재 디렉터리, JSON) 컬럼 추가
# 6: scan_runs 에 session_started_at/elapsed_accum(재개 누적 시간) 컬럼 추가
# 7: scan_stats 테이블 추가(파일 나이/소유자/확장자별 집계 리포트)
# 8: top_files 테이블 추가(최대 파일 Top-N)
# 9: top_files 에 atime 컬럼 + scan_stats 의 atime_age(접근시각 나이 분포)
SCHEMA_VERSION = 9
