# 설계 검토 + PoC: 6 PB 아이실론에서 빠른 사용량 측정

> 실행 중 스캔을 strace/gdb 로 진단한 결과(단일 락 + GIL 직렬화)와, 그 처방을
> 검증한 **멀티프로세스 PoC 실측치**, 그리고 멀티노드/아이실론 네이티브(FSA·
> SmartQuotas) 설계를 정리한다.

## 1. 진단 — 왜 현재 스캐너가 느린가 (측정으로 확인)

- 대상: 6.31 PB 아이실론, 디렉터리 4,730만(처리 4%)·파일 수십억.
- `strace -f -c`: 시간의 **80%가 futex**(락/GIL 대기), DB 쓰기(fdatasync/pwrite)는
  거의 0. CPU 4%. → **자원 포화가 아니라 직렬화**.
- `gdb thread apply all bt`: 워커들이 `sem_wait`(파이썬 `_dlock` 획득 대기)·`futex`
  (GIL)에 몰림. 실제 NAS stat/DB는 각각 1개 스레드만.
- 구조적 원인 **세 가지**: ① 파이썬 **GIL**(한 번에 한 스레드만 실행) ② 단일
  **`_dlock`**(DB·카운터·하드링크집합 보호) ③ **단일 DB 커넥션**. 8 워커가 사실상
  1~2개 속도(≈1,000 stat/s)에 묶임.

## 2. PoC — 프로세스 분리가 정말 확장되는가 (이 저장소 `isilon_usage/pscan.py`)

스레드가 아니라 **프로세스**로 쪼개면 각자 GIL을 가져 진짜 병렬이 된다. 같은
작업(파일당 처리 비용 모사)을 **ThreadPool(GIL) vs ProcessPool**로 측정:

| 워커 | 스레드(GIL) | 프로세스(pscan) | 프로세스 이점 |
|---:|---:|---:|---:|
| 1 | 3.95 s | 3.82 s | 1.03× |
| 2 | 4.61 s | 1.98 s | 2.33× |
| 4 | 5.73 s | 1.05 s | **5.48×** |
| 8 | 5.03 s | 1.01 s | 4.99× |

- **스레드 1→8 = 0.78×** (GIL로 안 늘고 오히려 경합으로 느려짐) — 진단과 정확히 일치.
- **프로세스 1→8 = 3.75×** (논리 CPU 4개 → 코어 수만큼 거의 선형).

> 결론: 진단이 가리킨 처방(프로세스 분리 + 공유 락 제거)이 **실측으로 검증**됐다.
> 로컬 FS 라 절대속도는 작지만, 비율(스레드 정체 vs 프로세스 선형)이 본질이다.
> NAS 에서는 여기에 **메타데이터 지연 겹치기**가 더해져 이점이 더 커진다.

### pscan 설계 요약
- 루트의 **1단계 자식 디렉터리 = 작업 단위**(서로 disjoint → 중복 없음).
- 각 단위를 **별도 프로세스**가 재귀로 집계(공유 락 없음, 결과만 반환).
- 코디네이터가 루트 직속 파일 + 단위 결과를 합산. `du` 와 동일하게 디렉터리 inode
  분도 계산. **정확성**: `os.walk` 레퍼런스와 프로세스 1/2/4/8 모두 일치(테스트).
- 한계(정직): 하드링크 dedup 은 프로세스 경계를 못 넘음(단위 내만). 1단계 분할이라
  한 자식이 거대하면 straggler — 실제 구현은 **작업 훔치기/더 깊은 분할**로 개선.

```bash
python3 -m isilon_usage pscan /mnt/hadoop --processes 8           # 병렬 스캔
python3 -m isilon_usage pscan /mnt/hadoop --compare              # 1·2·4·8 확장성 비교
```

## 3. 멀티노드 — 아이실론 여러 노드로 부하 분산

아이실론은 스케일아웃(단일 네임스페이스, 여러 노드). 외부에서 한 노드로만 RPC를
보내면 그 노드가 병목이다. pscan 은 `--node-mount` 로 **같은 트리의 다른 노드
마운트**를 받아 작업 단위를 **라운드로빈 분산**한다 → 메타데이터 부하가 전 노드로.

```bash
# 같은 /ifs 를 노드별로 마운트(SmartConnect 존 또는 노드 IP)
mount -o nconnect=8 node1:/ifs/data /mnt/n1
mount -o nconnect=8 node2:/ifs/data /mnt/n2
mount -o nconnect=8 node3:/ifs/data /mnt/n3
python3 -m isilon_usage pscan /mnt/n1 --processes 12 \
        --node-mount /mnt/n1 --node-mount /mnt/n2 --node-mount /mnt/n3
```

- 여기에 **`nconnect`(마운트당 TCP 다중화)** + **`sunrpc.tcp_slot_table_entries`**
  를 올리면(→ `🔧 튜닝 점검` 메뉴) 노드당 동시 RPC도 늘어 효과가 배가된다.
- **여러 클라이언트 호스트**로도 확장 가능: 네임스페이스를 호스트별로 쪼개 각 호스트가
  다른 노드를 물면 호스트×프로세스×노드의 곱으로 병렬. (기성 도구 `mpiFileUtils dwalk`
  가 이 방식 — 외부 워킹의 상한.)

## 4. 더 빠른 정답 — 아이실론 네이티브(FSA·SmartQuotas)

외부에서 stat 수억 번을 때리는 것 자체가 6 PB엔 비효율이다. OneFS는 **자기 내부
메타데이터(LIN 트리)** 로 사용량을 안다.

- **SmartQuotas 어카운팅 쿼터** — 디렉터리에 accounting quota 를 걸면 OneFS가
  사용량을 **상시 유지**. `isi quota quotas list` 또는 PAPI 로 **즉시(O(1))** 조회.
  *주기 모니터링의 정답.*
- **FSA(File System Analytics, InsightIQ)** — 잡 엔진이 **전 노드 병렬**로 분석
  리포트(상위 디렉터리·나이·유형…) 생성. *전체 분석의 정답.*

### 연동 설계(대시보드가 PAPI 를 읽는 형태)
OneFS Platform API(PAPI, HTTPS, 보통 8080) 예:
```
GET https://<cluster>:8080/platform/1/quota/quotas?path=/ifs/data&recurse-path-children=true
  → 각 디렉터리의 usage.{fs_logical,fs_physical,inodes}  (걷지 않고 즉시)
```
- 본 도구의 **글로벌 포탈(HQ)** 이 이미 스토리지 어레이 상태(`isilon.py`)를 PAPI로
  조회한다 → 같은 클라이언트에 **quota usage 수집기**를 더해, 대시보드/리포트가
  "스캔 결과"처럼 소비하면 된다(스키마 재사용). 워킹 엔진만 FSA/쿼터로 대체.
- ⚠ 이 부분은 **실제 아이실론 + InsightIQ/SmartQuotas 라이선스**가 있어야 검증 가능
  (본 PoC 컨테이너에선 미검증). 인증·버전(`/platform/N/…`)은 클러스터에 맞춰 조정.

## 5. 권장 우선순위 (정직한 결론)

| 순위 | 방법 | 속도 | 전제 |
|---|---|---|---|
| ★1 | **SmartQuotas 어카운팅 쿼터** | 즉시 | 클러스터 설정 권한 |
| ★2 | **FSA / InsightIQ** | 매우 빠름 | InsightIQ 라이선스 |
| 3 | **멀티노드 + 멀티프로세스 워킹**(이 PoC를 발전) | 빠름 | 마운트/개발 |
| 4 | `mpiFileUtils dwalk`(멀티호스트) | 빠름 | 클라이언트 여러 대 |
| 5 | 현재 스레드 스캐너 + fold-depth + 튜닝 | 제한적 | 지금 당장 |
| ✗ | DB 엔진 교체 / 저장형식 변경 | ~0 | (측정상 무의미) |

**중요**: 이 도구의 가치는 **대시보드·리포트·모니터링·멀티DC 포탈**이다(잘 만듦).
재작성 = 전부 버리기가 아니라 **느린 워킹 엔진만 교체**(프로세스 병렬 또는 FSA/쿼터),
**리포트 계층은 재사용**. pscan PoC 가 그 교체의 첫 조각이다.

## 다음 단계(이 PoC를 제품화)
1. pscan 에 **작업 훔치기/더 깊은 분할**(straggler 완화) + 진행률 IPC.
2. 프로세스별 결과를 **per-run DB 로 병합**(기존 스키마 재사용) → 대시보드가 그대로 소비.
3. **재개**(프론티어를 DB에) + fold-depth 통합.
4. (클러스터 권한 있으면) **SmartQuotas/FSA 수집기**를 포탈에 추가 — 가장 빠른 길.
