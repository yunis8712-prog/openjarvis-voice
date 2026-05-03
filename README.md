# OpenJarvis Voice

Browser-based **Korean voice assistant** with a JARVIS-style cyberpunk UI.
Runs entirely on a local LLM (Ollama) plus free Edge TTS — no API keys
required for the core loop.

![status](https://img.shields.io/badge/status-v1-cyan)
![license](https://img.shields.io/badge/license-MIT-blue)

## What it does

- **Push-to-talk web UI** (Space → record → Space → answer)
- **STT**: Chrome `webkitSpeechRecognition` (`ko-KR`, on-device or cloud-aided)
- **TTS**: Microsoft Edge Neural voices via `edge-tts` — natural Korean
  (`ko-KR-SunHiNeural` / `InJoonNeural` / `BongJinNeural` / `JiMinNeural`)
- **LLM**: Ollama, default `qwen2.5:14b-instruct-q3_K_M` (fits 12 GB VRAM
  fully on GPU)
- **Domain routers**: server detects intent in the user's last turn and
  fetches authoritative data instead of letting the model hallucinate:
  - **KBO baseball** — `kbo-game` npm
  - **KBL basketball** — `kbl-results` npm
  - **K League football** — `kleague-results` npm
  - **Lotto** — `k-lotto` npm
  - **Web search fallback** — DuckDuckGo (`ddgs`) + page-body fetch for
    weather, news, time, prices, etc.
- **JARVIS UI**: animated particle orb (canvas) with idle / listening /
  thinking / speaking states, system log, protocol panel, recent queries

## Demo

```text
🎤 LISTENING: 오늘 프로야구 일정 알려줘
> KBO SCHEDULE LOOKUP
> KBO 2026-05-03: 5 games
> SYNTHESIZING ANSWER...
🤖 자비스: "오늘은 5월 3일에 총 5경기가 진행 중입니다.
   잠실에서 NC와 LG, 문학에서 롯데와 SSG, 대구에서 한화와 삼성,
   광주에서 KT와 KIA, 고척에서 두산과 키움이 경기를 하고 있습니다."
🔊 (edge-tts ko-KR-SunHiNeural plays)
```

## Quick start

### Prerequisites
- Windows 10 / 11 (Linux/macOS untested but the Python parts are portable)
- Python 3.11 (`uv python install 3.11` recommended)
- [Ollama](https://ollama.com) running locally on port 11434
- Node.js 18+ (for KBO / KBL / K League / Lotto domain routers)
- Chrome browser (for `webkitSpeechRecognition`)

### Install

```powershell
# 1) Python deps via uv
uv sync   # creates .venv and installs from pyproject.toml

# 2) Pull the LLM into Ollama
ollama pull qwen2.5:14b-instruct-q3_K_M

# 3) Domain npm packages (for sports / lotto)
npm install -g kbo-game kbl-results kleague-results k-lotto

# 4) Run
.\jarvis_ui.cmd
```

Browser opens at `http://127.0.0.1:7860/`. Allow the microphone permission
on first run, then press **Space** to talk.

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `JARVIS_UI_HOST` | `127.0.0.1` | Server bind host |
| `JARVIS_UI_PORT` | `7860` | Server port |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama backend |
| `JARVIS_SEARCH_REGION` | `kr-kr` | DuckDuckGo region |
| `JARVIS_NODE` | `D:\nodejs\node.exe` | Node executable |
| `JARVIS_NPM` | `D:\nodejs\npm.cmd` | npm executable |

URL parameter `?voice=ko-KR-InJoonNeural` switches the TTS voice.

## Files

```
jarvis_ui.html        — single-file UI (canvas orb, STT/TTS, ollama bridge)
jarvis_ui_server.py   — FastAPI: serves UI, /api/chat (intent router),
                         /api/tts (edge-tts proxy), /healthz
jarvis_ui.cmd         — launcher (.venv python + auto-open browser)
voice_chat.py         — alt CLI: faster-whisper + edge-tts + ollama
voice.cmd             — launcher for voice_chat.py
SKILL.md              — Claude Code K-skill manifest
```

## How the intent router works

The server inspects the latest user message:

1. **Domain match** (KBO / KBL / K League / Lotto): fetch from official
   npm package, format result as plain text, inject as a `system`
   message right before the user's turn so the LLM treats it as
   authoritative context.
2. **Real-time keyword match** (날씨 / 뉴스 / 환율 / 시세 / 지금 / …):
   DuckDuckGo search (8 results) + fetch top-3 page bodies via lxml,
   inject as `system` message.
3. **Otherwise**: pass straight to the LLM.

Output constraints: `num_predict=140`, `temperature=0.2`, stop tokens on
markdown patterns, and a one-shot retry if the response leaks Hanja.
This keeps voice-bound answers to one short paragraph.

## Why route-then-inject instead of native tool calling?

`qwen2.5:14b-instruct-q3_K_M` is fast (fits 100% on a 12 GB GPU) but
unreliable at emitting structured `tool_calls`. It often emits
"`/WebAPI 호출 …`" as plaintext instead. Server-side intent detection
sidesteps that brittleness — the model only ever sees synthesized
`system` context, never has to format a tool call.

## License

MIT
