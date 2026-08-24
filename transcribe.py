"""Transcribe one isolated vocal track into StageFront timed lyric data."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import requests
from faster_whisper import WhisperModel

BASE_URL = os.environ["STAGEFRONT_WORKER_URL"].rstrip("/")
HEADERS = {
    "Authorization": f"Bearer {os.environ['KARAOKE_WORKER_SECRET']}",
    "x-vercel-protection-bypass": os.environ["VERCEL_AUTOMATION_BYPASS_SECRET"],
}


def update(job_id: str, progress: float, failed: str | None = None) -> None:
    payload = {"progress": progress, "status": "failed" if failed else "running"}
    if failed:
        payload["error"] = failed[-2000:]
    response = requests.patch(f"{BASE_URL}/api/karaoke-v2/worker/transcription/jobs/{job_id}", headers=HEADERS, json=payload, timeout=30)
    response.raise_for_status()


def main() -> int:
    claim = requests.post(f"{BASE_URL}/api/karaoke-v2/worker/transcription/jobs/claim", headers=HEADERS, timeout=30)
    if claim.status_code == 204:
        print("No queued transcription jobs.")
        return 0
    claim.raise_for_status()
    task = claim.json()
    job_id = task["job"]["id"]
    try:
        with tempfile.TemporaryDirectory(prefix="stagefront-lyrics-") as temp:
            vocals = Path(temp) / "vocals.mp3"
            with requests.get(task["vocals"]["url"], stream=True, timeout=900) as source:
                source.raise_for_status()
                with vocals.open("wb") as target:
                    for chunk in source.iter_content(chunk_size=1024 * 1024):
                        target.write(chunk)
            update(job_id, 0.1)
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, info = model.transcribe(str(vocals), beam_size=5, word_timestamps=True, vad_filter=True)
            lines = []
            duration_ms = max(1, round(info.duration * 1000))
            for line_index, segment in enumerate(segments, start=1):
                tokens = []
                for word_index, word in enumerate(segment.words or [], start=1):
                    text = word.word.strip()
                    if not text or word.start is None or word.end is None:
                        continue
                    start_ms = max(0, round(word.start * 1000))
                    end_ms = max(start_ms + 1, round(word.end * 1000))
                    tokens.append({
                        "id": f"word-{line_index:04d}-{word_index:04d}", "text": text,
                        "startMs": start_ms, "endMs": end_ms, "confidence": round(float(word.probability), 4),
                    })
                if not tokens:
                    continue
                lines.append({
                    "id": f"line-{line_index:04d}", "text": " ".join(token["text"] for token in tokens),
                    "startMs": tokens[0]["startMs"], "endMs": tokens[-1]["endMs"], "tokens": tokens,
                })
            if not lines:
                raise RuntimeError("No sung words were detected in the vocal track.")
            update(job_id, 0.9)
            complete = requests.post(
                f"{BASE_URL}/api/karaoke-v2/worker/transcription/jobs/{job_id}/complete",
                headers=HEADERS,
                json={"language": info.language, "durationMs": duration_ms, "lines": lines}, timeout=120,
            )
            complete.raise_for_status()
            print(f"Completed transcription {job_id}: {len(lines)} timed lines.")
            return 0
    except Exception as error:
        update(job_id, 0, failed=str(error))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
