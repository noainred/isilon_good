# 오프라인(폐쇄망) 다운로드

git 을 쓸 수 없는 폐쇄망 서버를 위해, **압축본 하나만 받아서** 옮기면 바로 실행할
수 있도록 미리 만들어 둔 파일입니다. (현재 버전: **1.1.0**)

| 파일 | 용도 |
|------|------|
| `isilon_usage-1.1.0.tar.gz` | 리눅스용(이 버전 고정) |
| `isilon_usage-1.1.0.zip` | 윈도우 등(이 버전 고정) |
| `isilon_usage-latest.tar.gz` | **링크가 안 바뀌는 최신본**(리눅스) |
| `isilon_usage-latest.zip` | 링크가 안 바뀌는 최신본(윈도우) |

> `tar.gz`(또는 `zip`) **한 개가 프로그램 전체**입니다. 하나만 받으면 됩니다.

## ⬇️ wget 으로 받기 (중요)
GitHub **파일 페이지(`/blob/...`) 주소를 wget 하면 HTML 페이지가 받아집니다.**
반드시 아래 **raw 주소**(`raw.githubusercontent.com`)를 쓰세요. 인터넷 되는 PC에서
받아 폐쇄망 서버로 `scp`/USB 로 옮깁니다.

```bash
# 최신본(링크 고정) — 리눅스
wget https://raw.githubusercontent.com/noainred/isilon_good/claude/upbeat-bell-cXX8f/download/isilon_usage-latest.tar.gz

# tar.gz + zip 둘 다 한 번에(bash 중괄호 확장)
wget https://raw.githubusercontent.com/noainred/isilon_good/claude/upbeat-bell-cXX8f/download/isilon_usage-latest.{tar.gz,zip}

# 특정 버전으로 받기
wget https://raw.githubusercontent.com/noainred/isilon_good/claude/upbeat-bell-cXX8f/download/isilon_usage-1.1.0.tar.gz
```

### 비공개(private) 저장소라서 위 wget 이 404/로그인 페이지를 주면
토큰(PAT)으로 GitHub API 를 통해 받습니다(파일이 1MB 미만이라 가능):
```bash
curl -L -H "Authorization: Bearer <GITHUB_PAT>" \
     -H "Accept: application/vnd.github.raw" \
     -o isilon_usage-latest.tar.gz \
  "https://api.github.com/repos/noainred/isilon_good/contents/download/isilon_usage-latest.tar.gz?ref=claude/upbeat-bell-cXX8f"
```

## 설치/실행 (압축 해제만으로 — 별도 설치 불필요)
```bash
tar xzf isilon_usage-latest.tar.gz        # 또는 unzip isilon_usage-latest.zip
cd isilon_usage-1.1.0
python3 -m isilon_usage --version          # isilon_usage 1.1.0 (schema 1)

python3 -m isilon_usage serve --data-dir /var/lib/isilon_usage \
        --mount-base /mnt/isilon --port 8765
```
표준 라이브러리만으로 동작합니다(psutil 은 선택). 자세한 사용법은 압축본 안의
`docs/USER_GUIDE.md` 참고.

## 압축본 재생성
```bash
python3 tools/make_release.py     # download/ 의 버전본 + latest 본 갱신(재현 가능)
```
