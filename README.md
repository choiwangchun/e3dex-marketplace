# E3DEX 플러그인 마켓플레이스

AVEVA E3D PIPE·Structures·장비·PLAN 작업 도구입니다. 지원: **Claude Code**, **Codex** (Windows x64 + AVEVA E3D 2.1)

| 플러그인 | 버전 | 사용 조건 |
|---|---|---|
| `e3dex-open` | 1.0.0 | 무료 · 로그인 없이 바로 사용 |
| `e3dex` | 4.4.1 | 로그인 + Paddle 정기 구독 또는 관리자가 부여한 수동 이용 권한 |

두 플러그인은 같은 기능입니다. 한 PC에는 둘 중 하나만 설치하세요.

## Claude Code

```
/plugin marketplace add choiwangchun/e3dex-marketplace
/plugin install e3dex-open@e3dex
/plugin install e3dex@e3dex
```

터미널에서는 `claude plugin marketplace add choiwangchun/e3dex-marketplace` 후 `claude plugin install <플러그인>@e3dex`.
업데이트: `/plugin marketplace update e3dex`.

## Codex

```
codex plugin marketplace add choiwangchun/e3dex-marketplace
codex plugin add e3dex-open@e3dex
codex plugin add e3dex@e3dex
```

또는 Codex 앱의 플러그인 화면에서 `E3DEX` 마켓플레이스를 선택해 설치합니다.
업데이트: `codex plugin marketplace upgrade e3dex`.

## 처음 사용할 때

**무료 (`e3dex-open`)**: 설치 후 새 채팅에서 "E3DEX 초기 설정해줘"라고 요청합니다. 로그인 없이 바로 연결됩니다.

**구독 (`e3dex`)**:

1. https://choi-macmini-1.tailf0b78b.ts.net/signup 에서 가입하고 Paddle 정기 구독을 결제합니다. 관리자가 수동 이용 권한을 부여한 계정은 결제 없이 사용할 수 있습니다.
2. 새 채팅에서 "E3DEX 로그인해줘" → 브라우저에서 코드를 확인하고 **연결하기**.
3. 처음 쓰는 PC라면 "E3DEX 초기 설정해줘"라고 요청합니다.

초기 설정은 E3D를 닫은 상태에서 E3D 폴더에 E3DEX 애드인을 설치합니다 (Windows 관리자 승인이 필요할 수 있음).
배관·구조·장비·PLAN 생성은 관리 서버에서 실행되며, 이 저장소에는 E3D 연결 부분만 들어 있습니다.
