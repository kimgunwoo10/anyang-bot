# 아냥봇 프로젝트 진행 상황 요약 (백업용)

이 파일은 대화가 유실될 경우를 대비한 백업 프롬프트입니다.
새 Claude 대화(Claude Code든 Claude Desktop이든)에 이 내용을 통째로 붙여넣으면 이어서 작업할 수 있습니다.

---

디스코드 봇 "아냥봇" 프로젝트를 이어서 작업하려고 해. 지금까지 진행된 내용을 정리해줄게.

## 프로젝트 위치
- 경로: C:\Users\82105\anyang-bot
- Python 가상환경: .venv (이미 생성됨, requirements.txt 패키지 설치 완료)
- discord.py 2.x, app_commands(슬래시 커맨드) 기반
- AI는 Anthropic Claude가 아니라 Google Gemini API(무료 티어)로 구현되어 있음 — google-genai 패키지 사용, 모델은 gemini-2.5-flash (gemini-2.0-flash는 이 계정에서 무료 티어 할당량이 0이라 못 씀)

## 프로젝트 구조
- main.py: 봇 실행 진입점, 인텐트(message_content, members) 설정, 6개 코그 로드, 슬래시 커맨드 동기화
- config.py: .env에서 DISCORD_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, DB_PATH 읽어옴 (python-dotenv)
- requirements.txt: discord.py, python-dotenv, google-genai, aiosqlite
- .env / .env.example: 토큰/키 저장 (.env는 .gitignore에 포함, 실제 값은 이미 채워져 있음)
- utils/db.py: aiosqlite 기반 SQLite 헬퍼 (execute/fetch_all/fetch_one), init_db()에서 테이블 생성 + 마이그레이션(ALTER TABLE로 컬럼 추가) 처리
- cogs/server_flavor.py: 트리거-응답 + 환영 메시지 기능 (완성)
- cogs/ai_qa.py: Gemini 기반 AI Q&A 기능 (완성)
- cogs/tts.py: 음성 채널 TTS 읽어주기 (완성) — edge-tts(무료 기본값) / Google Cloud TTS / ElevenLabs 3개 엔진 지원
- cogs/leveling.py: 레벨링(경험치) 시스템 (완성) — MEE6 표준 방식
- cogs/reputation.py, summary.py, activity_report.py, event_matching.py: 아직 뼈대만 있음 (Cog 클래스 + setup()만 존재, 실제 기능 없음)

## DB 스키마 (SQLite, data/anyang.db)
- triggers: id, guild_id, keyword, response_type(text/image), response_content, created_by, created_at
- guild_faq: guild_id, content (서버별 FAQ 텍스트)
- guild_config: guild_id, ai_qa_channel_id, moderation_channel_id, persona, welcome_channel_id, welcome_message, tts_channel_id, levelup_channel_id
- conversation_history: id, guild_id, user_id, role(user/model), content, created_at (유저별 대화 기억, 최근 6개만 유지)
- user_tts_voice: user_id, voice, provider('edge'|'google'|'elevenlabs') — 유저별 TTS 목소리 선택
- user_levels: guild_id, user_id, level, xp — 레벨링 시스템이 사용 중 (xp는 "현재 레벨에서 모은 XP", 누적 총합 아님)
- reputation, message_activity: 미래 기능용으로 테이블만 미리 생성해둠 (아직 안 씀)

## 완성된 기능 (총 슬래시 커맨드 12개, 최상위 8개)

### 1. 트리거-응답 + 환영 메시지 (cogs/server_flavor.py)
- `/트리거 등록 키워드:... 응답:... 타입:(텍스트|이미지 URL)` — 관리자 전용
- `/트리거 삭제 키워드:...`
- `/트리거 목록`
- 매칭 방식: 메시지 "전체"가 키워드와 정확히 일치할 때만 반응 (부분 포함 매칭 아님. 예: "픽픽"엔 반응하지만 "픽픽아"엔 반응 안 함)
- 도배 방지: 같은 채널+같은 트리거는 10초 쿨다운 (TRIGGER_COOLDOWN_SECONDS = 10.0, 코드 상수)
- `/환영 설정 채널:#... 메시지:...` — 신규 멤버 입장 시 자동 환영 메시지 (관리자 전용). 플레이스홀더: {user}=멘션, {username}=닉네임, {server}=서버 이름, {membercount}=멤버 수. 줄바꿈은 `\n`으로 입력하면 실제 줄바꿈으로 변환됨 (Discord 슬래시 커맨드 입력창이 한 줄이라 이렇게 처리함)
- `/환영 해제` — 환영 메시지 끄기
- `/환영 테스트` — 실제 입장 없이 현재 설정 미리보기 (ephemeral)
- on_member_join 이벤트로 동작, members 인텐트 필요(이미 켜져 있음)

### 2. AI Q&A (cogs/ai_qa.py) — Gemini API 기반
- `/질문 내용:...` — 아무나 사용 가능, Gemini가 답변
- `/faq설정 내용:...` — 서버별 FAQ/지식베이스 등록 (관리자 전용, 실행할 때마다 기존 내용 전체 덮어씀, 최대 8000자), 질문할 때 컨텍스트로 자동 전달
- `/qa채널설정 채널:#...` — 지정 채널에서는 슬래시 커맨드 없이 자연어로 질문해도 자동 응답 (관리자 전용)
- `/qa채널해제` — 위 설정 해제 (관리자 전용)
- `/성격설정 내용:...` — 아냥봇의 말투/성격 커스터마이징, 시스템 프롬프트에 반영 (관리자 전용, 최대 2000자, 기본값: "친근하고 간결한 한국어로, 예의 바르게 답변해")
- `/성격초기화` — 커스터마이징한 말투/성격을 기본값으로 되돌림 (관리자 전용). 주의: /성격설정에 "원래대로 해줘" 같은 문장을 넣으면 그 문장이 그대로 성격으로 저장되므로 초기화는 반드시 이 명령어로 해야 함
- `/대화초기화` — 자기 자신과 봇의 대화 기억 초기화 (누구나 자기 것만 가능)
- 대화 맥락 기억: 유저별로 최근 대화 6개 메시지(약 3턴)를 DB에 저장했다가 다음 질문에 이어서 전달 (멀티턴 대화). google-genai의 contents 파라미터에 [{"role": "user"/"model", "parts": [{"text": ...}]}, ...] 형식으로 전달.

### 3. TTS 음성 읽어주기 (cogs/tts.py)
- `/tts입장`, `/tts퇴장` — 유저가 있는 음성 채널에 봇이 들어가고 나감
- `/tts채널설정 채널:#...`, `/tts채널해제` — 이 채널의 메시지를 음성으로 읽어줌 (관리자 전용)
- `/tts목소리` — Google Cloud Standard 목소리(A/B/C/D) 개인 선택 (GOOGLE_TTS_API_KEY 필요)
- `/tts프리미엄목소리` — ElevenLabs 프리미엄 목소리 검색/선택 (ELEVENLABS_API_KEY 필요)
- 엔진 3개: edge-tts(무료, 키 불필요, 기본 안전망), Google Cloud TTS(REST API 키 방식), ElevenLabs
- ffmpeg는 imageio-ffmpeg 내장 실행 파일 사용, 음성 재생에 PyNaCl 필요

### 4. 레벨링 시스템 (cogs/leveling.py) — MEE6 표준 방식
- 채팅 경험치: 메시지 1개당 15~25 XP 랜덤, 유저당 60초 쿨다운(도배 방지), 다음 레벨 필요 XP = 5×레벨² + 50×레벨 + 100
- 음성 경험치: 음성 채널 접속 1분마다 5~10 XP 랜덤 (잠수가 많아 채팅보다 낮게 책정). tasks.loop(60초)로 지급
- 음성 잠수 판정 3가지: ① 헤드셋 끔(self_deaf/deaf) (마이크만 끈 뮤트는 정상 취급) ② 채널에 봇 제외 2명 미만 ③ 서버 AFK 채널. 잠수면 0이 아니라 1분마다 1~2 XP만 소량 지급
- 채팅/음성 XP는 같은 레벨 풀에 합산 (레벨이 따로 있지 않음)

### 5. 포인트 + 웹 레벨 상점 (cogs/leveling.py 포인트 적립 + cogs/web_shop.py)
- 포인트(상점 화폐, XP와 별개): 채팅 1분당 70P, 음성 1분당 80P, 음성 잠수 1분당 30P, `/출석체크` 하루 1회 1,000P (KST 날짜 기준)
- `/레벨 확인`에 보유 포인트 표시
- 웹 상점: 봇 프로세스 안에서 aiohttp 웹서버가 같이 돈다 (기본 http://localhost:8080)
  - Discord OAuth 로그인(identify 스코프) → web_sessions 테이블에 세션 저장
  - /api/me (내 레벨/XP/포인트/보유상품), /api/items, /api/buy
  - 구매 시 봇이 즉시 역할 지급: 칭호(역할 생성+부여), 이름색(개인 역할 색 변경, 봇 역할 바로 아래로 위치 조정 시도)
  - 상품 목록은 web_shop.py의 ITEMS 리스트에서 관리 (칭호 3종 + 이름색 변경권으로 시작)
  - 페이지는 web/shop.html — "금단증상" 새벽 감성 디자인, 접속 시각에 따라 새벽/아침/낮/노을/밤 테마 자동 전환
- 필요 설정(.env): DISCORD_CLIENT_SECRET(개발자 포털 OAuth2), OAUTH_REDIRECT_URI(포털 Redirects에 동일하게 등록), SHOP_GUILD_ID(비우면 "금단증상" 이름으로 자동 탐색)
- DB: user_points(points, last_daily), shop_purchases, web_sessions 테이블 추가
- `/레벨 확인 [유저]` — 레벨/경험치/진행 바/서버 순위 (누구나)
- `/레벨 랭킹` — 서버 상위 10명 리더보드 (누구나)
- `/레벨 채널설정 채널:#...` — 레벨업 축하 메시지를 지정 채널로 모아서 발송 (관리자 전용)
- `/레벨 채널해제` — 지정 해제, 기본 동작(레벨업한 그 채널에 바로 발송)으로 복귀 (관리자 전용)
- 쿨다운은 메모리(time.monotonic) 기반이라 봇 재시작 시 초기화됨 (큰 문제 아님)

## 실행 방법
PowerShell 실행 정책 문제로 .\.venv\Scripts\Activate.ps1이 막혀서, 대신 이렇게 직접 실행:
```
cd C:\Users\82105\anyang-bot
.\.venv\Scripts\python.exe main.py
```
(활성화 스크립트 안 쓰고 venv 안의 python.exe를 직접 호출하면 됨. 굳이 실행 정책을 풀고 싶으면 `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`)

## 겪었던 트러블슈팅 히스토리 (참고용, 다시 안 겪어도 되게 기록)
1. Discord Privileged Intents(message_content, members)를 개발자 포털에서 안 켜서 처음엔 연결 실패 → 포털 Bot 설정에서 토글 켜서 해결
2. 봇 초대 링크에 applications.commands 스코프가 빠져서 슬래시 커맨드가 안 보임 → client_id로 scope=bot+applications.commands 포함한 정식 OAuth2 URL(`https://discord.com/oauth2/authorize?client_id=...&permissions=...&scope=bot%20applications.commands`)로 재초대해서 해결
3. Gemini API 키의 gemini-2.0-flash 무료 티어 할당량이 0(RESOURCE_EXHAUSTED, limit:0) → gemini-2.5-flash로 모델 교체해서 해결 (같은 계정에서도 모델별로 무료 티어 지원 여부가 다를 수 있음)
4. 로컬 네트워크에서 discord.gg 게이트웨이 DNS 조회가 간헐적으로 실패(ClientConnectorDNSError) → discord.py가 자동 재연결(RESUME)하긴 하는데, 반복되면 ipconfig /flushdns나 DNS 서버(8.8.8.8 등) 변경 고려 필요
5. Discord 슬래시 커맨드의 문자열 입력창은 한 줄이라 실제 줄바꿈 입력이 안 됨 → 사용자가 `\n`을 리터럴로 입력하면 코드에서 `.replace("\\n", "\n")`으로 실제 줄바꿈으로 변환하는 방식으로 우회
6. utils/db.py의 init_db()는 CREATE TABLE IF NOT EXISTS만으로는 이미 존재하는 테이블에 새 컬럼이 안 생기므로, `_ensure_column()` 헬퍼로 PRAGMA table_info 확인 후 ALTER TABLE ADD COLUMN 하는 마이그레이션 패턴을 씀 (새 컬럼 추가할 때마다 이 패턴 재사용)

## Discord 봇 계정 정보 (참고)
- 봇 이름: 아냥봇#4053
- Application ID: 1524212886229094512

## 아직 안 한 것 / 다음에 이어서 할 수 있는 것
- reputation.py: 특정 리액션(예: 🙏)을 감사 표시로 집계해서 랭킹 보여주는 기능
- summary.py: 채널 최근 메시지 N개를 Gemini로 요약
- activity_report.py: 서버 활동 데이터 수집 + 주간 리포트 DM 발송
- event_matching.py: 이벤트 파티 자동 매칭, 텍스트 기반 협동 RPG 골격
- 서버 컨셉에 맞는 "직업/역할" 스토리형 성장 시스템 — 레벨링 시스템(cogs/leveling.py)은 완성됐으니, 레벨업 시 역할 자동 부여를 여기에 얹으면 됨
- 부적절한 발언 감지 후 모더레이터 채널 알림 (모더레이션 보조 기능, moderation_channel_id 컬럼은 이미 준비됨)

이 내용을 참고해서 이어서 도와줘.
