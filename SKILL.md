---
name: openjarvis-voice
description: Browser-based Korean voice assistant with a JARVIS-style cyberpunk UI. Push-to-talk loop using Chrome STT, Edge TTS Neural voices, and a local Ollama LLM (qwen2.5:14b-instruct-q3_K_M). Server-side intent router fetches authoritative data for KBO baseball, KBL basketball, K League football, Lotto, and falls back to DuckDuckGo web search for weather/news/prices.
license: MIT
metadata:
  category: voice
  locale: ko-KR
  phase: v1
---

# OpenJarvis Voice

## What this skill does

Stand up a local Korean voice assistant with a sci-fi web UI. The user
speaks via the browser microphone, the server routes the question to the
right backend (KBO / KBL / K League / Lotto / web search) before sending
it to a local LLM, and the answer comes back as natural Korean speech.

## When to use

- "음성으로 자비스 띄워줘"
- "JARVIS UI 켜줘"
- "한국어 음성 비서 셋업해줘"
- "프로야구 / KBL / K리그 / 로또 결과를 음성으로 묻고 싶어"

## Prerequisites

- Windows 10/11 (Python parts are cross-platform; only `voice.cmd` /
  `jarvis_ui.cmd` are Windows shell wrappers)
- Python 3.11 (`uv python install 3.11`)
- Ollama running on `localhost:11434` with
  `qwen2.5:14b-instruct-q3_K_M` pulled
- Node.js 18+ for sports/lotto domain routers
- Chrome (for `webkitSpeechRecognition`; Edge also works)

## Install

```bash
git clone https://github.com/yunis8712-prog/openjarvis-voice
cd openjarvis-voice
uv sync
ollama pull qwen2.5:14b-instruct-q3_K_M
npm install -g kbo-game kbl-results kleague-results k-lotto
```

## Run

```powershell
.\jarvis_ui.cmd
```

The launcher opens `http://127.0.0.1:7860/` in the default browser.
Press **Space** to record, **Space** again to send, **q+Enter** to
quit (CLI variant), **ESC** to interrupt the answer.

## How the intent router works

Server-side `needs_kbo` / `needs_kbl` / `needs_kleague` / `needs_lotto`
/ `needs_realtime` keyword detectors run on the latest user message.
The first match wins:

| Match | Backend | Notes |
|---|---|---|
| 야구 / KBO / 잠실 / 두산 / KIA / … | `kbo-game` npm | Official KBO JSON, date-aware |
| KBL / 농구 / LG 세이커스 / … | `kbl-results` npm | Standings + match list |
| K리그 / FC서울 / 전북 / … | `kleague-results` npm | League 1/2 toggle |
| 로또 / 당첨번호 / 1222회 | `k-lotto` npm | Latest or specific round |
| 날씨 / 뉴스 / 환율 / 지금 / … | `ddgs` + page fetch | 8 results + top-3 body extract |
| (otherwise) | LLM only | Common knowledge, conversation |

The matched result is injected as a fresh `system` message, then the
LLM is asked to summarise it in two sentences without markdown.

## Output constraints

To keep voice-bound answers tight:
- `num_predict=140`, `temperature=0.2`
- Stop tokens on markdown list / heading starts
- Defensive retry if the response leaks Hanja (CJK Unified Ideographs)
- Korean-only system prompt with explicit "no markdown / no Hanja /
  conclusion-first" rules

## Customisation

- TTS voice: append `?voice=ko-KR-InJoonNeural` to the URL (or change
  `TTS_VOICE` in `jarvis_ui.html`)
- Default LLM: edit `MODEL` in `jarvis_ui.html` (e.g. swap to
  `qwen3.5:4b` for ~2 s answers at lower quality)
- New domain handler: add a `KEYWORDS` tuple, a `needs_X(text)` check,
  a `fetch_X_for(text)` async function, and a branch in the router.

## Done when

- Browser at `http://127.0.0.1:7860/` shows the JARVIS UI
- Pressing Space starts recording (orb turns amber)
- A test question like "오늘 프로야구 일정" returns a one-paragraph
  Korean answer spoken in `ko-KR-SunHiNeural`
- `jarvis doctor`-style smoke check: `curl http://127.0.0.1:7860/healthz`
  reports `ollama: 200 Ollama is running`

## Repository

https://github.com/yunis8712-prog/openjarvis-voice
