# E3DEX 플러그인 마켓플레이스

현재 열린 E3D 프로젝트·MDB와 쓰기 가능한 DB·SITE를 읽고 사용자가 선택한 위치에 작업합니다. PIPE 끝단 처리, GS 구조·브레이싱, 장비 CTOR·VP 노즐, PLAN/DXF 기능을 제공합니다. 정확한 세션과 대상 DB를 실행 직전에 검증합니다. Windows x64 / E3D 2.1 전용입니다. 관리자가 승인한 계정만 사용할 수 있으며, 배관·구조·장비·PLAN 생성은 관리 서버에서 실행되어 항상 최신 버전이 적용됩니다.

- 지원: **Claude Code**, **Codex** (Windows x64 + AVEVA E3D 2.1)
- 플러그인: `e3dex` 4.3.2
- 사용하려면 관리자 승인 계정이 필요합니다: https://choi-macmini-1.tailf0b78b.ts.net/signup

## Claude Code

```
/plugin install e3dex --marketplace choiwangchun/e3dex-marketplace
```

(Claude Code 2.1.275 이상은 위 한 줄로 끝납니다. 이전 버전은 아래 두 줄)

```
/plugin marketplace add choiwangchun/e3dex-marketplace
/plugin install e3dex@e3dex
```

터미널에서는 `claude plugin marketplace add choiwangchun/e3dex-marketplace` 후 `claude plugin install e3dex@e3dex`.
업데이트: `/plugin marketplace update e3dex`.

## Codex

```
codex plugin marketplace add choiwangchun/e3dex-marketplace
codex plugin add e3dex@e3dex
```

또는 Codex 앱의 플러그인 화면에서 `E3DEX` 마켓플레이스를 선택해 설치합니다.
업데이트: `codex plugin marketplace upgrade e3dex`.

## 처음 사용할 때

1. https://choi-macmini-1.tailf0b78b.ts.net/signup 에서 사용 신청 → 관리자 승인을 기다립니다.
2. 새 채팅에서 "E3DEX 로그인해줘" → 브라우저에서 코드를 확인하고 **연결하기**.
3. 처음 쓰는 PC라면 "E3DEX 초기 설정해줘"라고 요청합니다.

Claude Code와 Codex를 같은 PC에서 함께 쓰면 로그인은 한 번만 하면 됩니다.
배관·구조·장비·PLAN 생성은 관리 서버에서 실행되며, 이 저장소에는 E3D 연결 부분만 들어 있습니다.
