# 오프라인(폐쇄망) 다운로드

git 을 쓸 수 없는 폐쇄망 서버를 위해, **압축본 하나만 받아서** 옮기면 바로 실행할
수 있도록 미리 만들어 둔 압축 파일입니다. (현재 버전: **1.1.0**)

| 파일 | 용도 |
|------|------|
| `isilon_usage-1.1.0.tar.gz` | 리눅스 서버용 |
| `isilon_usage-1.1.0.zip` | 윈도우 PC 등에서 받아 옮길 때 |

## 받는 법 (인터넷 되는 PC의 브라우저에서)
GitHub 파일 페이지에서 **“Download raw file”** 버튼을 누르거나, 아래 주소로 바로 받습니다.
그런 다음 폐쇄망 서버로 `scp`/USB 등으로 옮깁니다.

## 설치/실행 (압축 해제 후 — 별도 설치 불필요)
```bash
tar xzf isilon_usage-1.1.0.tar.gz       # 또는 unzip isilon_usage-1.1.0.zip
cd isilon_usage-1.1.0
python3 -m isilon_usage --version       # isilon_usage 1.1.0 (schema 1)

# 대시보드 실행(마운트한 NAS를 웹에서 골라 스캔)
python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage \
        --mount-base /mnt/isilon --port 8765
```
표준 라이브러리만으로 동작하므로 추가 설치가 필요 없습니다(psutil 은 선택).
자세한 사용법은 압축본 안의 `docs/USER_GUIDE.md` 를 보세요.

## 압축본 재생성
```bash
python3 tools/make_release.py     # download/ 아래에 .tar.gz, .zip 갱신
```
