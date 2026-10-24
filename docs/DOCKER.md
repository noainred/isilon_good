# Docker 로 실행하기

The Davinci NAS Management 는 **순수 Python 표준 라이브러리**라 `pip install` 이 필요 없습니다 →
Dockerfile 이 아주 단순하고, **인터넷 없이(폐쇄망)** 도 빌드/배포됩니다. 자원 모니터는 psutil 없이
`/proc` 폴백으로 동작합니다(리눅스 컨테이너면 충분).

## 가장 간단 — docker compose
`docker-compose.yml` 의 `/mnt/isilon` 을 실제 스캔 대상 경로로 바꾼 뒤:
```bash
docker compose up -d        # 시작
docker compose logs -f      # 로그
# 접속: http://<호스트-IP>:8765
docker compose down         # 중지
```

## docker 명령으로 직접
```bash
docker build -t isilon-usage:latest .
docker run -d --name isilon-usage -p 8765:8765 \
    -v /mnt/isilon:/mnt/isilon:ro \        # 스캔 대상 NAS(읽기전용)
    -v isilon_data:/data \                 # 설정·DB 영속 볼륨
    isilon-usage:latest \
    serve --data-dir /data --host 0.0.0.0 --port 8765 --mount-base /mnt/isilon
```
- **스캔 대상**은 `-v 호스트경로:컨테이너경로:ro` 로 넣고, `--mount-base` 로 스캔 허용 범위를 그 경로로 제한합니다.
  여러 개면 `-v` 를 여러 번(또는 상위 마운트 하나). 웹 UI 에서 컨테이너 안 경로(예: `/mnt/isilon/...`)를 지정해 스캔합니다.
- **데이터 영속**: `/data` 를 볼륨으로 두면 설정·스캔 DB 가 컨테이너 교체/업그레이드에도 보존됩니다.
- **NFS 를 직접** 붙이려면 호스트에서 마운트 후 그 경로를 `-v ...:ro` 로 넣는 게 가장 간단합니다.

## 폐쇄망(에어갭) 배포
인터넷 되는 곳에서 이미지를 만들어 파일로 옮깁니다:
```bash
docker build -t isilon-usage:1.99.23 .
docker save isilon-usage:1.99.23 | gzip > isilon-usage-docker-1.99.23.tar.gz
# → USB 등으로 옮긴 뒤, 대상 호스트에서:
docker load < isilon-usage-docker-1.99.23.tar.gz
```
사내 레지스트리의 파이썬 베이스만 쓸 수 있다면 빌드 시 지정:
```bash
docker build --build-arg BASE=<사내레지스트리>/python:3.12-slim -t isilon-usage:latest .
```
(베이스는 Python 3.6+ 면 무엇이든 됩니다 — 표준 라이브러리만 쓰므로.)

## 업그레이드
새 코드로 이미지를 다시 빌드(또는 새 이미지 load)한 뒤 컨테이너만 교체하면 됩니다. `/data` 볼륨은 그대로 두므로
설정·이력이 보존됩니다:
```bash
docker compose build && docker compose up -d      # compose
# 또는
docker rm -f isilon-usage && docker run -d ... isilon-usage:<새버전> ...
```

## 참고
- **포트**: 기본 8765. 바꾸려면 `-p 9000:8765` (호스트 9000 → 컨테이너 8765) 또는 `--port` 도 함께 바꿔 맞추세요.
- **비루트 실행**을 원하면 `docker run --user 1000:1000 ...` — 단 마운트한 NAS 경로를 그 UID 가 읽을 수 있어야 합니다.
- **자원 모니터 정밀도**를 높이려면(선택) 이미지에 psutil 을 추가할 수 있습니다:
  `RUN pip install --no-cache-dir psutil` (인터넷 필요). 없어도 `/proc` 폴백으로 정상 동작합니다.
- **방화벽**: 호스트에서 8765/TCP 를 허용하세요.
