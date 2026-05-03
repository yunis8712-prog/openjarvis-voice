"""Korean voice chat with OpenJarvis (Ollama qwen2.5:14b q3).

Push-to-talk loop:
  - Press Enter to start recording, press Enter again to stop.
  - Type 'q' + Enter to quit.

Pipeline: faster-whisper (STT) -> Ollama chat -> edge-tts (TTS) -> PyAV+sounddevice.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile


# Hold DllDirectoryCookie objects for the lifetime of the process.
# os.add_dll_directory removes the entry when its cookie is garbage-collected,
# so we must keep references alive at module scope.
_DLL_COOKIES: list = []


def _register_nvidia_dlls() -> None:
    """Windows: pip-installed nvidia-* runtime DLLs aren't on PATH by default.
    ctranslate2 (faster-whisper backend) calls LoadLibrary on cublas64_12.dll
    etc. and fails unless we register their dirs first.
    """
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return
    venv_lib = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    if not os.path.isdir(venv_lib):
        return
    for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
        bin_dir = os.path.join(venv_lib, sub, "bin")
        if os.path.isdir(bin_dir):
            try:
                _DLL_COOKIES.append(os.add_dll_directory(bin_dir))
            except OSError:
                pass
    # Also prepend to PATH so any LoadLibrary path-search variant finds the DLLs.
    extra_path = os.pathsep.join(
        os.path.join(venv_lib, sub, "bin")
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")
        if os.path.isdir(os.path.join(venv_lib, sub, "bin"))
    )
    if extra_path:
        os.environ["PATH"] = extra_path + os.pathsep + os.environ.get("PATH", "")


_register_nvidia_dlls()

import av  # noqa: E402
import edge_tts  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402
import soundfile as sf  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

SAMPLE_RATE = 16000
CHANNELS = 1
WHISPER_MODEL_SIZE = "small"
WHISPER_DOWNLOAD_ROOT = r"D:\Tools\whisper-models"
TTS_VOICE = "ko-KR-SunHiNeural"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:14b-instruct-q3_K_M"
SYSTEM_PROMPT = (
    "당신은 친근한 한국어 음성 비서입니다. "
    "말로 답하기 좋게 짧고 자연스럽게, 마크다운 없이 대답하세요."
)


def record_audio() -> np.ndarray:
    print("🎤 녹음 중... Enter 누르면 종료.", flush=True)
    frames: list[np.ndarray] = []

    def callback(indata, _frames, _time, _status):
        frames.append(indata.copy())

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        callback=callback,
        dtype="float32",
    ):
        input()
    if not frames:
        return np.zeros((0, CHANNELS), dtype="float32")
    return np.concatenate(frames, axis=0)


def transcribe(audio: np.ndarray, model: WhisperModel) -> str:
    if audio.size == 0:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        sf.write(wav_path, audio, SAMPLE_RATE)
        segments, _info = model.transcribe(
            wav_path,
            language="ko",
            beam_size=1,
            vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()
    finally:
        os.unlink(wav_path)


def ask_ollama(history: list[dict], client: httpx.Client) -> str:
    r = client.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "messages": history, "stream": False},
    )
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def _decode_mp3(path: str) -> tuple[np.ndarray, int]:
    """Decode an MP3 file to mono float32 PCM via PyAV. Returns (samples, rate)."""
    with av.open(path) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        sample_rate = stream.rate
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
        chunks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                arr = resampled.to_ndarray()
                chunks.append(arr.reshape(-1))
        # Flush
        for resampled in resampler.resample(None):
            arr = resampled.to_ndarray()
            chunks.append(arr.reshape(-1))
    if not chunks:
        return np.zeros(0, dtype="float32"), sample_rate
    return np.concatenate(chunks).astype("float32"), sample_rate


async def synthesize_and_play(text: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        mp3_path = f.name
    try:
        await edge_tts.Communicate(text, TTS_VOICE).save(mp3_path)
        samples, rate = _decode_mp3(mp3_path)
        if samples.size == 0:
            return
        sd.play(samples, rate)
        sd.wait()
    finally:
        try:
            os.unlink(mp3_path)
        except PermissionError:
            pass


def main() -> int:
    print("OpenJarvis 음성 대화 — qwen2.5:14b q3 + ko-KR-SunHiNeural")
    print("초기화 중...", flush=True)

    print("  Whisper", WHISPER_MODEL_SIZE, "로딩 (첫 실행은 모델 다운로드 ~244MB)...", flush=True)
    try:
        model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device="cuda",
            compute_type="float16",
            download_root=WHISPER_DOWNLOAD_ROOT,
        )
        print("  ✅ CUDA (GPU)")
    except Exception as exc:
        print(f"  ⚠️  CUDA 실패 ({exc}) → CPU로 fallback", flush=True)
        model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type="int8",
            download_root=WHISPER_DOWNLOAD_ROOT,
        )

    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    client = httpx.Client(timeout=120.0)

    print("\n준비 완료. Enter→녹음, q+Enter→종료.\n")

    try:
        while True:
            cmd = input("[Enter=녹음, q=종료] ").strip().lower()
            if cmd == "q":
                return 0
            audio = record_audio()
            print("📝 받아쓰기...", flush=True)
            user_text = transcribe(audio, model)
            if not user_text:
                print("(빈 음성, 다시 시도)\n")
                continue
            print(f"🗣️  너: {user_text}")
            history.append({"role": "user", "content": user_text})
            print("💭 답변 생성...", flush=True)
            try:
                reply = ask_ollama(history, client)
            except Exception as exc:
                print(f"❌ Ollama 호출 실패: {exc}\n")
                history.pop()
                continue
            history.append({"role": "assistant", "content": reply})
            print(f"🤖 자비스: {reply}")
            print("🔊 재생...", flush=True)
            try:
                asyncio.run(synthesize_and_play(reply))
            except Exception as exc:
                print(f"❌ TTS 실패: {exc}")
            print()
    except KeyboardInterrupt:
        print("\n중단됨.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
