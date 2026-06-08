"""isilon_usage — 초대용량 NAS 디렉터리 사용량 조사 도구.

메모리를 최소로 쓰면서 디렉터리별 파일 수/용량을 조사하고, 진행 상황과
서버 자원(특히 메모리/du 프로세스 메모리)을 웹 대시보드로 보여준다.
"""

# 애플리케이션 버전(릴리즈노트 CHANGELOG.md 와 git 태그 v<버전>에 대응)
__version__ = "1.1.6"

# DB 스키마 버전. 스키마가 바뀌면 1씩 올리고 마이그레이션을 추가한다.
# (per-run DB / manager DB 의 PRAGMA user_version 에 기록된다)
SCHEMA_VERSION = 2
