# Isilon 디렉터리 사용량 대시보드 컨테이너 이미지
#
# 빌드:  docker build -t isilon-usage .
# 실행:  아이실론 마운트를 컨테이너 안으로 넣고(예: 호스트의 /mnt/isilon),
#        데이터 폴더를 볼륨으로 연결한다.
#   docker run -d --name isilon-usage -p 8765:8765 \
#       -v /mnt/isilon:/mnt/isilon:ro \
#       -v isilon_data:/data \
#       isilon-usage serve --data-dir /data --mount-base /mnt/isilon \
#                          --host 0.0.0.0 --port 8765
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# psutil 포함 설치(없어도 /proc 폴백으로 동작)
RUN pip install --no-cache-dir ".[monitor]"

EXPOSE 8765
VOLUME ["/data"]

ENTRYPOINT ["isilon-usage"]
CMD ["serve", "--data-dir", "/data", "--host", "0.0.0.0", "--port", "8765"]
