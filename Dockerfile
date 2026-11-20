# The Davinci NAS Management — 컨테이너 이미지
#
# 순수 Python 표준 라이브러리라 pip install 이 필요 없다(폐쇄망/오프라인 빌드 가능).
# 자원 모니터는 psutil 없이 /proc 폴백으로 동작한다(리눅스 컨테이너면 충분).
#
# 빌드:  docker build -t isilon-usage:latest .
#        (사내 베이스 이미지가 있으면:  docker build --build-arg BASE=<사내>/python:3.12-slim -t isilon-usage .)
# 실행:  스캔할 NAS 를 읽기전용으로 넣고, 데이터 폴더를 볼륨으로 연결한다.
#   docker run -d --name isilon-usage -p 8765:8765 \
#       -v /mnt/isilon:/mnt/isilon:ro \
#       -v isilon_data:/data \
#       isilon-usage serve --data-dir /data --host 0.0.0.0 --port 8765 --mount-base /mnt/isilon
ARG BASE=python:3.12-slim
FROM ${BASE}

WORKDIR /app
# 애플리케이션 패키지만 복사(.dockerignore 로 download/·.git·tests 등 제외 → 이미지 슬림).
COPY isilon_usage/ /app/isilon_usage/

EXPOSE 8765
VOLUME ["/data"]

# python -m 로 실행(콘솔 스크립트 설치 불필요). serve 인자는 CMD 로 override 가능.
ENTRYPOINT ["python", "-m", "isilon_usage"]
CMD ["serve", "--data-dir", "/data", "--host", "0.0.0.0", "--port", "8765"]
