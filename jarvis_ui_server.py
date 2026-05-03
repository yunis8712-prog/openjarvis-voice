"""Tiny FastAPI server for the JARVIS web UI.

Serves jarvis_ui.html at / and proxies POST /api/chat to local Ollama
(stream-passthrough). Same-origin avoids CORS issues; serving via
http://localhost lets Chrome's webkitSpeechRecognition work (file:// is
restricted).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import edge_tts
import httpx
from ddgs import DDGS
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from lxml import html as lxml_html

ROOT = Path(__file__).parent.resolve()
HTML = ROOT / "jarvis_ui.html"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
SEARCH_REGION = os.environ.get("JARVIS_SEARCH_REGION", "kr-kr")

app = FastAPI(title="JARVIS UI")


# Keyword-based real-time intent detection. Quantised models (q3) are
# unreliable at native ollama tool calling, so we sidestep that entirely
# and let the server decide deterministically when to search.
REALTIME_KEYWORDS = (
    # weather
    "날씨", "기온", "기상", "예보", "미세먼지", "황사", "비와", "비 와", "비가 ", "눈 와", "눈이 ",
    # news / current events
    "뉴스", "헤드라인", "속보", "보도", "지금 무슨일", "최근 일",
    # markets / prices
    "환율", "시세", "주가", "주식", "코스피", "코스닥", "나스닥", "다우", "비트코인",
    "이더리움", "금값", "유가",
    # time
    "지금 몇", "현재 시", "몇 시", "며칠", "오늘 날짜", "무슨 요일",
    # temporal hints
    "오늘", "내일", "방금", "지금", "현재", "최근", "이번 주", "이번주",
    # entertainment / charts
    "박스오피스", "검색어", "트렌드 ", "인기 검색", "차트", "빌보드",
    # sports / results
    "경기 결과", "스코어", "오늘 경기",
)


def needs_realtime(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.replace(" ", "").lower()
    for kw in REALTIME_KEYWORDS:
        if kw.replace(" ", "").lower() in t:
            return True
    return False


# ── KBO baseball domain handler ─────────────────────────────────────────
KBO_KEYWORDS = (
    "야구", "kbo", "프로야구", "프로 야구",
    "kia", "lg", "두산", "삼성", "한화", "롯데", "ssg", "nc", "키움", "kt",
    "타이거즈", "트윈스", "베어스", "라이온즈", "이글스", "자이언츠",
    "랜더스", "다이노스", "히어로즈", "위즈",
    "잠실", "고척", "문학", "사직", "수원", "대구 야구", "광주 야구",
)
NODE_EXE = os.environ.get("JARVIS_NODE", r"D:\nodejs\node.exe")
NPM_CMD = os.environ.get("JARVIS_NPM", r"D:\nodejs\npm.cmd")
KST = timezone(timedelta(hours=9))


def needs_kbo(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in KBO_KEYWORDS)


def parse_relative_date(text: str) -> str:
    """Map natural-language date hints to YYYY-MM-DD (KST). Defaults to today."""
    today = datetime.now(KST).date()
    if "그제" in text or "그저께" in text:
        return (today - timedelta(days=2)).strftime("%Y-%m-%d")
    if "어제" in text:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if "내일" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "모레" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    # Try to find a YYYY-MM-DD or M월 D일 pattern
    m = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m:
        return f"{today.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return today.strftime("%Y-%m-%d")


_HANZI_RE = re.compile(r"[一-鿿]")


def has_hanzi(text: str) -> bool:
    return bool(_HANZI_RE.search(text or ""))


_KBO_STATUS = {
    "BEFORE": "예정",
    "READY": "예정",
    "IN_PROGRESS": "진행중",
    "END": "종료",
    "CANCEL": "취소",
    "CANCELED": "취소",
    "RAINY": "우천중단",
}


_NPM_ROOT_CACHE: str | None = None


async def _npm_root() -> str:
    global _NPM_ROOT_CACHE
    if _NPM_ROOT_CACHE:
        return _NPM_ROOT_CACHE
    proc = await asyncio.create_subprocess_exec(
        NPM_CMD, "root", "-g",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    _NPM_ROOT_CACHE = out.decode("utf-8", errors="ignore").strip().splitlines()[-1]
    return _NPM_ROOT_CACHE


async def _run_node(js: str, *, module: bool = True) -> str:
    """Run a JS snippet in Node and return stdout. Raises on non-zero exit."""
    args = [NODE_EXE]
    if module:
        args += ["--input-type=module", "-"]
    else:
        args += ["-"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(js.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {err.decode('utf-8', errors='ignore')[:300]}")
    return out.decode("utf-8", errors="ignore")


async def _kbo_fetch_raw(date_str: str) -> list[dict]:
    """Spawn Node with the kbo-game package to fetch a date's games."""
    npm_root = await _npm_root()
    js = (
        'import path from "node:path";'
        'import { pathToFileURL } from "node:url";'
        f'const entry = pathToFileURL(path.join({json.dumps(npm_root)}, "kbo-game", "dist", "index.js")).href;'
        'const { getGame } = await import(entry);'
        f'const games = await getGame(new Date("{date_str}T00:00:00+09:00"));'
        'console.log(JSON.stringify(games));'
    )
    out = await _run_node(js)
    return json.loads(out or "[]")


async def fetch_kbo_for(text: str) -> tuple[str, str]:
    """Resolve date from text and return (date_str, formatted_summary)."""
    date_str = parse_relative_date(text)
    try:
        games = await _kbo_fetch_raw(date_str)
    except Exception as exc:
        return date_str, f"(KBO 조회 실패: {exc})"
    if not games:
        return date_str, f"({date_str} KBO 경기가 없거나 휴식일입니다.)"
    lines = [f"{date_str} KBO 경기:"]
    for g in games:
        away = g.get("awayTeam", "?")
        home = g.get("homeTeam", "?")
        score = g.get("score") or {}
        sa = score.get("away", 0)
        sh = score.get("home", 0)
        status = _KBO_STATUS.get(g.get("status"), g.get("status", "?"))
        stadium = g.get("stadium", "")
        start = g.get("startTime", "")
        line = f"- {stadium} {start} | {away} {sa} : {sh} {home} [{status}"
        if g.get("status") == "IN_PROGRESS" and g.get("currentInning"):
            line += f" {g['currentInning']}회"
        line += "]"
        lines.append(line)
    return date_str, "\n".join(lines)


# ── KBL basketball ──────────────────────────────────────────────────────
KBL_KEYWORDS = (
    "kbl", "농구", "프로농구",
    "세이커스", "정관장", "kt 소닉붐", "소닉붐", "캐롯", "한국가스공사", "삼성 썬더스", "썬더스",
    "kgc", "전자랜드", "현대모비스", "모비스", "디비 프로미", "디비프로미", "프로미",
    "창원 lg", "안양 정관장", "수원 kt", "고양 캐롯",
)


def needs_kbl(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in KBL_KEYWORDS)


async def fetch_kbl_for(text: str) -> tuple[str, str]:
    date_str = parse_relative_date(text)
    npm_root = await _npm_root()
    js = (
        'import path from "node:path";'
        'import { pathToFileURL } from "node:url";'
        f'const entry = pathToFileURL(path.join({json.dumps(npm_root)}, "kbl-results", "src", "index.js")).href;'
        'const m = await import(entry);'
        f'const r = await m.getKBLSummary({json.dumps(date_str)});'
        'console.log(JSON.stringify(r));'
    )
    try:
        out = await _run_node(js)
        data = json.loads(out or "{}")
    except Exception as exc:
        return date_str, f"(KBL 조회 실패: {exc})"
    matches = data.get("matches") or []
    rows = ((data.get("standings") or {}).get("rows") or [])[:5]
    lines = [f"{date_str} KBL"]
    if matches:
        lines.append("경기:")
        for m in matches:
            away = (m.get("awayTeam") or {}).get("name", "?")
            home = (m.get("homeTeam") or {}).get("name", "?")
            sa = m.get("awayScore", "")
            sh = m.get("homeScore", "")
            status = m.get("statusName") or m.get("status", "")
            lines.append(f"- {away} {sa} : {sh} {home} [{status}]")
    else:
        lines.append("이 날 경기 없음.")
    if rows:
        lines.append("순위 상위 5:")
        for row in rows:
            tn = (row.get("team") or {}).get("name", "?")
            lines.append(f"- {row.get('rank')}위 {tn} {row.get('win')}승 {row.get('loss')}패")
    return date_str, "\n".join(lines)


# ── K League football ──────────────────────────────────────────────────
KLEAGUE_KEYWORDS = (
    "k리그", "케이리그", "축구", "fc서울", "전북 현대", "울산", "포항", "수원 삼성",
    "인천 유나이티드", "강원 fc", "광주 fc", "대구 fc", "성남", "제주", "김천 상무",
    "안양", "부천", "전남", "충남",
)


def needs_kleague(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in KLEAGUE_KEYWORDS)


async def fetch_kleague_for(text: str) -> tuple[str, str]:
    date_str = parse_relative_date(text)
    league_id = 2 if ("k리그2" in text.lower() or "케이리그2" in text or "2부" in text) else 1
    npm_root = await _npm_root()
    js = (
        'import path from "node:path";'
        'import { pathToFileURL } from "node:url";'
        f'const entry = pathToFileURL(path.join({json.dumps(npm_root)}, "kleague-results", "src", "index.js")).href;'
        'const m = await import(entry);'
        f'const r = await m.getKLeagueSummary({json.dumps(date_str)}, {{ leagueId: {league_id} }});'
        'console.log(JSON.stringify(r));'
    )
    try:
        out = await _run_node(js)
        data = json.loads(out or "{}")
    except Exception as exc:
        return date_str, f"(K리그 조회 실패: {exc})"
    matches = data.get("matches") or []
    rows = ((data.get("standings") or {}).get("rows") or [])[:5]
    lines = [f"{date_str} K리그{league_id}"]
    if matches:
        lines.append("경기:")
        for m in matches:
            home = (m.get("homeTeam") or {}).get("name", "?")
            away = (m.get("awayTeam") or {}).get("name", "?")
            hs = m.get("homeScore", "")
            as_ = m.get("awayScore", "")
            status = m.get("statusName") or m.get("status", "")
            lines.append(f"- {away} {as_} : {hs} {home} [{status}]")
    else:
        lines.append("이 날 경기 없음.")
    if rows:
        lines.append("순위 상위 5:")
        for row in rows:
            tn = (row.get("team") or {}).get("name", "?")
            lines.append(
                f"- {row.get('rank')}위 {tn} 승점 {row.get('points')} ({row.get('win')}승 {row.get('draw')}무 {row.get('loss')}패)"
            )
    return date_str, "\n".join(lines)


# ── Lotto ──────────────────────────────────────────────────────────────
LOTTO_KEYWORDS = ("로또", "당첨번호", "당첨 번호", "lotto", "동행복권")


def needs_lotto(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in LOTTO_KEYWORDS)


_LOTTO_ROUND_RE = re.compile(r"(\d{3,4})\s*회")


async def fetch_lotto_for(text: str) -> tuple[str, str]:
    npm_root = await _npm_root()
    m = _LOTTO_ROUND_RE.search(text)
    round_arg = f'{int(m.group(1))}' if m else "null"
    js = (
        f'process.env.NODE_PATH = {json.dumps(npm_root)};'
        'require("module").Module._initPaths();'
        'const lotto = require("k-lotto");'
        '(async () => {'
        f'  const round = {round_arg} || (await lotto.getLatestRound());'
        '  const r = await lotto.getResult(round);'
        '  console.log(JSON.stringify({ round, result: r }));'
        '})();'
    )
    try:
        out = await _run_node(js, module=False)
        data = json.loads(out or "{}")
    except Exception as exc:
        return "", f"(로또 조회 실패: {exc})"
    rnd = data.get("round")
    res = data.get("result") or {}
    nums = res.get("numbers") or res.get("winningNumbers") or []
    bonus = res.get("bonusNumber") or res.get("bonus") or ""
    date = res.get("drawDate") or res.get("date") or ""
    label = f"제 {rnd}회" if rnd else "최신회차"
    summary = f"{label} 로또"
    if date:
        summary += f" ({date})"
    if nums:
        summary += " 당첨번호: " + ", ".join(str(n) for n in nums)
    if bonus:
        summary += f", 보너스 {bonus}"
    return str(rnd or ""), summary


# ── Running / Marathon ─────────────────────────────────────────────────
RUNNING_KEYWORDS = (
    "러닝", "달리기", "조깅", "마라톤", "풀코스", "하프코스", "하프 코스",
    "페이스", "주행거리", "러닝코스", "러닝 코스", "달리기코스", "달리기 코스",
    "마라톤대회", "마라톤 대회", "10k 대회", "10km 대회", "5k 대회", "하프 마라톤",
)

# Named distances (km). Order matters: longest keys first to avoid '하프'
# accidentally matching inside '하프코스' before we resolve the longer name.
_DIST_NAMED: dict[str, float] = {
    "풀코스": 42.195, "풀 코스": 42.195,
    "하프코스": 21.0975, "하프 코스": 21.0975,
    "10키로": 10.0, "10킬로": 10.0,
    "5키로": 5.0, "5킬로": 5.0,
    "3키로": 3.0, "3킬로": 3.0,
    "하프": 21.0975,
    "풀": 42.195,
}


def needs_running(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in RUNNING_KEYWORDS)


def _parse_distance_km(text: str) -> float | None:
    t = text.lower()
    for name, km in _DIST_NAMED.items():
        if name in t:
            return km
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|킬로미터|키로미터|킬로|키로|k\b|케이\b)", t)
    if m:
        return float(m.group(1))
    return None


def _parse_time_seconds(text: str) -> int | None:
    """'1시간 30분 25초' / '90분' / '25분 30초' / '1:30:25' / '25:00'."""
    t = text.replace(" ", "")
    m = re.search(r"(\d+)시간(?:(\d+)분)?(?:(\d+)초)?", t)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + s
    m = re.search(r"(\d+)분(?:(\d+)초)?", t)
    if m and "시간" not in t:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"\b(\d+):(\d{1,2}):(\d{1,2})\b", t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _parse_pace_sec_per_km(text: str) -> int | None:
    """'페이스 5분', '페이스 4분 30초', '4:30 페이스', '5분 페이스'."""
    m = re.search(r"페이스\s*(\d+)\s*분(?:\s*(\d+)\s*초)?", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*분(?:\s*(\d+)\s*초)?\s*페이스", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"페이스\s*(\d+):(\d{1,2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _fmt_pace(sec_per_km: float) -> str:
    s = int(round(sec_per_km))
    return f"{s // 60}분 {s % 60:02d}초/km"


def _fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분 {s}초"
    return f"{m}분 {s}초"


def compute_running(text: str) -> str | None:
    """Try distance/time/pace math from the user's text. Returns a formatted
    summary or None if not enough info to compute."""
    km = _parse_distance_km(text)
    sec = _parse_time_seconds(text)
    pace = _parse_pace_sec_per_km(text)
    # Prefer pace match over generic '5분' that the time parser would catch.
    if pace and sec and pace == sec:
        sec = None
    if km and sec and not pace:
        pace_calc = sec / km
        speed_kmh = km / (sec / 3600)
        return (
            f"{km}km를 {_fmt_dur(sec)}에 뛰면 페이스는 {_fmt_pace(pace_calc)}, "
            f"평균 속도는 {speed_kmh:.2f} km/h입니다."
        )
    if km and pace and not sec:
        sec_calc = km * pace
        return (
            f"{km}km를 페이스 {_fmt_pace(pace)}로 뛰면 약 {_fmt_dur(sec_calc)} 걸립니다."
        )
    if sec and pace and not km:
        km_calc = sec / pace
        return (
            f"{_fmt_dur(sec)} 동안 페이스 {_fmt_pace(pace)}로 뛰면 약 {km_calc:.2f}km 갑니다."
        )
    return None


# ── Cloud GPT (OpenAI) escape hatch ────────────────────────────────────
GPT_KEYWORDS = (
    "gpt", "지피티", "chatgpt", "챗지피티",
    "openai", "오픈ai", "오픈 ai", "오픈에이아이",
)
GPT_MODEL = os.environ.get("JARVIS_GPT_MODEL", "gpt-4o-mini")
# Phrases stripped from the user message before forwarding to OpenAI so
# the model isn't told "ask GPT" — it IS GPT now.
_GPT_STRIP = (
    "GPT 연결해서", "GPT한테", "GPT로", "GPT에게", "GPT한테 물어봐", "GPT 한테",
    "지피티 연결해서", "지피티한테", "지피티로", "지피티에게",
    "ChatGPT한테", "ChatGPT에게", "챗지피티한테", "챗지피티에게",
    "gpt 연결해서", "gpt한테", "gpt로", "gpt에게",
)


def needs_gpt(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in GPT_KEYWORDS)


def _strip_gpt_triggers(content: str) -> str:
    out = content
    for s in _GPT_STRIP:
        out = out.replace(s, "")
    return out.strip(" ,.?!·")


async def ask_openai_chat(messages: list[dict]) -> tuple[str, str | None]:
    """Returns (answer, error_or_None)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return (
            "OpenAI API 키가 설정되지 않았습니다. "
            "PowerShell에서 환경변수 OPENAI_API_KEY를 등록한 뒤 서버를 다시 띄워주세요.",
            "no_key",
        )
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return ("openai 패키지가 설치되지 않았습니다.", "import_error")

    cleaned = []
    for m in messages:
        if m.get("role") == "user":
            cleaned.append({**m, "content": _strip_gpt_triggers(m.get("content", ""))})
        else:
            cleaned.append(m)

    client = AsyncOpenAI(api_key=api_key)
    try:
        resp = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=cleaned,
            max_tokens=220,
            temperature=0.3,
        )
        return (resp.choices[0].message.content.strip(), None)
    except Exception as exc:
        return (f"GPT 호출 실패: {exc}", "api_error")


SEARCH_RESULTS = 8
FETCH_TOP_N = 3
FETCH_MAX_CHARS = 1800
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
_STRIP_TAGS_XPATH = (
    "//script | //style | //noscript | //nav | //header | //footer "
    "| //aside | //form | //iframe | //svg"
)


def _ddg_search_sync(query: str, max_results: int) -> list[dict]:
    return list(
        DDGS().text(query, region=SEARCH_REGION, safesearch="moderate", max_results=max_results)
    )


async def _fetch_clean(client: httpx.AsyncClient, url: str, max_chars: int = FETCH_MAX_CHARS) -> str:
    """Fetch a page and return main-body plain text (best-effort, fast-fail)."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    try:
        r = await client.get(url)
    except Exception:
        return ""
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "").lower():
        return ""
    try:
        doc = lxml_html.fromstring(r.text)
    except Exception:
        return ""
    for el in doc.xpath(_STRIP_TAGS_XPATH):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    text = " ".join(doc.xpath("//body//text()"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


async def web_search(query: str) -> str:
    """DuckDuckGo search + body-fetch enrichment, formatted for the model."""
    try:
        results = await asyncio.to_thread(_ddg_search_sync, query, SEARCH_RESULTS)
    except Exception as exc:
        return f"(검색 실패: {exc})"
    if not results:
        return "(검색 결과 없음)"

    fetch_count = min(FETCH_TOP_N, len(results))
    timeout = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=5.0)
    fetched: list[str] = [""] * fetch_count
    async with httpx.AsyncClient(
        timeout=timeout, headers=FETCH_HEADERS, follow_redirects=True
    ) as client:
        tasks = [_fetch_clean(client, results[i].get("href", "")) for i in range(fetch_count)]
        fetched = await asyncio.gather(*tasks)

    parts = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        snippet = (r.get("body") or "").strip()
        href = r.get("href") or ""
        body = fetched[i - 1] if i - 1 < len(fetched) else ""
        block = f"[{i}] {title}\n요약: {snippet}"
        if body:
            block += f"\n본문: {body}"
        block += f"\n출처: {href}"
        parts.append(block)
    return "\n\n".join(parts)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HTML, media_type="text/html; charset=utf-8")


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


@app.post("/api/chat")
async def chat(req: Request) -> StreamingResponse:
    """Chat endpoint with keyword-driven web search.

    The server inspects the latest user turn; if it contains a real-time
    keyword, it runs DuckDuckGo + page fetch up-front and injects the
    results as a system message before invoking the model. This is more
    reliable than native ollama tool calling on quantised q3 14B, which
    often emits tool-call-shaped *text* instead of structured calls.

    Stream lines (NDJSON):
      {"_progress": "..."}               — UI status updates
      {"message": {"content": "..."}, "done": true}  — final answer
      {"error": "..."}                   — error
    """
    data = await req.json()
    messages = list(data.get("messages") or [])
    model = data.get("model") or "qwen2.5:14b-instruct-q3_K_M"
    search_enabled = bool(data.get("tools_enabled", True))

    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = (m.get("content") or "").strip()
            break

    async def gen():
        # Cloud GPT escape hatch — explicit user opt-in via "GPT" / "지피티".
        # Wins over every domain router below (overrides local ollama too).
        if last_user and needs_gpt(last_user):
            yield _ndjson({"_progress": f"OPENAI {GPT_MODEL}"})
            answer, err = await ask_openai_chat(messages)
            if err:
                yield _ndjson({"_progress": f"GPT: {err}"})
            yield _ndjson({"message": {"content": answer}, "done": True})
            return

        injected = None  # tuple (label, content) when a domain handler ran
        if search_enabled and last_user:
            if needs_kbo(last_user):
                yield _ndjson({"_progress": "KBO SCHEDULE LOOKUP"})
                date_str, summary = await fetch_kbo_for(last_user)
                yield _ndjson({"_progress": f"KBO {date_str}: {summary.splitlines()[0][:80]}"})
                injected = (
                    f"다음은 {date_str} KBO 경기 데이터입니다 (kbo-game 공식 소스). "
                    "이 데이터만 근거로 사용자 질문에 답하세요. "
                    "마크다운/리스트/줄바꿈 없이 한국어 한 단락으로, "
                    "두 문장 이내로 핵심만 요약하세요.",
                    summary,
                )
            elif needs_kbl(last_user):
                yield _ndjson({"_progress": "KBL LOOKUP"})
                date_str, summary = await fetch_kbl_for(last_user)
                yield _ndjson({"_progress": f"KBL {date_str}: {summary.splitlines()[0][:80]}"})
                injected = (
                    f"다음은 {date_str} KBL 데이터입니다 (kbl-results 공식 소스). "
                    "이 데이터만 근거로 사용자 질문에 답하세요. "
                    "마크다운 없이 한 단락으로, 두 문장 이내로 핵심만 요약하세요.",
                    summary,
                )
            elif needs_kleague(last_user):
                yield _ndjson({"_progress": "K-LEAGUE LOOKUP"})
                date_str, summary = await fetch_kleague_for(last_user)
                yield _ndjson({"_progress": f"KLEAGUE {date_str}: {summary.splitlines()[0][:80]}"})
                injected = (
                    f"다음은 {date_str} K리그 데이터입니다 (kleague-results 공식 소스). "
                    "이 데이터만 근거로 사용자 질문에 답하세요. "
                    "마크다운 없이 한 단락으로, 두 문장 이내로 핵심만 요약하세요.",
                    summary,
                )
            elif needs_lotto(last_user):
                yield _ndjson({"_progress": "LOTTO LOOKUP"})
                _round_str, summary = await fetch_lotto_for(last_user)
                yield _ndjson({"_progress": f"LOTTO: {summary[:80]}"})
                injected = (
                    "다음은 동행복권 공식 로또 데이터입니다 (k-lotto). "
                    "이 데이터만 근거로 사용자 질문에 답하세요. "
                    "마크다운 없이 한 단락, 두 문장 이내. 번호는 콤마로 자연스럽게 읽도록 적으세요.",
                    summary,
                )
            elif needs_running(last_user):
                # Two sub-modes: deterministic pace math (no network) or
                # web search for marathon events / running courses.
                pace_summary = compute_running(last_user)
                if pace_summary is not None:
                    yield _ndjson({"_progress": "RUNNING PACE CALC"})
                    yield _ndjson({"_progress": pace_summary[:80]})
                    injected = (
                        "다음은 러닝 페이스/시간/거리 계산 결과입니다. "
                        "이 값을 그대로 한 문장으로 자연스럽게 전달하세요. 마크다운 금지.",
                        pace_summary,
                    )
                else:
                    yield _ndjson({"_progress": f"RUNNING SEARCH: {last_user}"})
                    try:
                        results = await web_search(last_user)
                    except Exception as exc:
                        yield _ndjson({"_progress": f"SEARCH ERROR: {exc}"})
                        results = ""
                    if results:
                        preview = results.splitlines()[0][:80]
                        yield _ndjson({"_progress": f"RESULTS: {preview}"})
                        injected = (
                            "다음은 러닝/마라톤 관련 검색 결과입니다. "
                            "이 정보만 근거로 핵심만 한 문장으로 답하세요. 마크다운 금지.",
                            results,
                        )
            elif needs_realtime(last_user):
                yield _ndjson({"_progress": f"WEB SEARCH: {last_user}"})
                try:
                    results = await web_search(last_user)
                except Exception as exc:
                    yield _ndjson({"_progress": f"SEARCH ERROR: {exc}"})
                    results = ""
                if results:
                    preview = results.splitlines()[0][:80]
                    yield _ndjson({"_progress": f"RESULTS: {preview}"})
                    injected = (
                        "다음은 방금 가져온 최신 검색 결과입니다. "
                        "이 정보만 근거로 사용자 질문에 답하세요. "
                        "마크다운/리스트/제목 없이 자연스러운 구어체 한 단락으로, "
                        "두 문장 이내·100자 이내로 핵심 수치 한두 개만 골라 답하세요.",
                        results,
                    )

            if injected is not None:
                preface, body = injected
                # Insert as system message right before the latest user turn
                # so the model treats it as fresh, authoritative context.
                insert_at = len(messages) - 1
                messages.insert(insert_at, {"role": "system", "content": f"{preface}\n\n{body}"})
                yield _ndjson({"_progress": "SYNTHESIZING ANSWER..."})

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": 140,
                "temperature": 0.2,
                # Cut on markdown list/heading starts but allow plain
                # paragraph continuations (so we don't truncate at ":\n\n").
                "stop": ["\n- ", "\n* ", "\n#", "\n1. ", "\n2. ", "###", "```"],
            },
        }
        timeout = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            except Exception as exc:
                yield _ndjson({"error": f"ollama unreachable: {exc}"})
                return
            if r.status_code != 200:
                yield _ndjson({"error": f"ollama {r.status_code}: {r.text[:300]}"})
                return
            final = (r.json().get("message", {}).get("content") or "").strip()

            # Defensive retry: q3 14B occasionally leaks Chinese characters
            # mid-Korean. One re-roll with an explicit Hanja-ban prompt.
            if has_hanzi(final):
                yield _ndjson({"_progress": "RETRY (한자 감지)"})
                retry_messages = list(messages) + [
                    {"role": "assistant", "content": final},
                    {
                        "role": "user",
                        "content": "방금 답변에 한자(중국어 글자)가 섞였습니다. 의미는 그대로 두고 한자 부분만 한글로 바꿔서 똑같이 짧게 다시 답해주세요. 마크다운/줄바꿈 없이 한 단락으로.",
                    },
                ]
                retry_payload = dict(payload)
                retry_payload["messages"] = retry_messages
                try:
                    r2 = await client.post(f"{OLLAMA_URL}/api/chat", json=retry_payload)
                    if r2.status_code == 200:
                        retried = (r2.json().get("message", {}).get("content") or "").strip()
                        if retried and not has_hanzi(retried):
                            final = retried
                except Exception:
                    pass

            yield _ndjson({"message": {"content": final}, "done": True})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/tts")
async def tts(req: Request) -> StreamingResponse:
    data = await req.json()
    text = (data.get("text") or "").strip()
    voice = data.get("voice") or "ko-KR-SunHiNeural"
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    async def stream():
        comm = edge_tts.Communicate(text, voice)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]

    return StreamingResponse(stream(), media_type="audio/mpeg")


@app.get("/healthz")
async def healthz() -> Response:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{OLLAMA_URL}/")
            return Response(
                content=f"ollama: {r.status_code} {r.text[:60]}",
                media_type="text/plain",
            )
    except Exception as exc:
        return Response(content=f"ollama unreachable: {exc}", status_code=503)


def main() -> int:
    import uvicorn

    host = os.environ.get("JARVIS_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("JARVIS_UI_PORT", "7860"))
    print(f"JARVIS UI → http://{host}:{port}")
    print(f"  proxying ollama at {OLLAMA_URL}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
