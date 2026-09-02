"""Render one queued StageFront karaoke project into an MP4 video."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import requests

BASE_URL = os.environ["STAGEFRONT_WORKER_URL"].rstrip("/")
HEADERS = {
    "Authorization": f"Bearer {os.environ['KARAOKE_WORKER_SECRET']}",
    "x-vercel-protection-bypass": os.environ["VERCEL_AUTOMATION_BYPASS_SECRET"],
}


def update(job_id: str, progress: float, failed: str | None = None) -> None:
    payload = {"progress": progress, "status": "failed" if failed else "running"}
    if failed:
        payload["error"] = failed[-2000:]
    response = requests.patch(
        f"{BASE_URL}/api/karaoke-v2/worker/render/jobs/{job_id}", headers=HEADERS, json=payload, timeout=30
    )
    response.raise_for_status()


def upload(destination: dict, path: Path) -> None:
    with path.open("rb") as video:
        response = requests.put(
            destination["signedUrl"],
            headers={"x-upsert": "true"},
            data={"cacheControl": "3600"},
            files={"": (path.name, video, "video/mp4")},
            timeout=1800,
        )
    if not response.ok:
        raise RuntimeError(f"Video upload failed ({response.status_code}): {response.text[:500]}")


def main() -> int:
    claim = requests.post(f"{BASE_URL}/api/karaoke-v2/worker/render/jobs/claim", headers=HEADERS, timeout=30)
    if claim.status_code == 204:
        print("No queued video renders.")
        return 0
    claim.raise_for_status()
    task = claim.json()
    job_id = task["job"]["id"]
    try:
        with tempfile.TemporaryDirectory(prefix="stagefront-render-") as temp:
            work = Path(temp)
            instrumental = work / "instrumental.mp3"
            subtitles = work / "karaoke.ass"
            output = work / "karaoke.mp4"
            with requests.get(task["instrumental"]["url"], stream=True, timeout=900) as source:
                source.raise_for_status()
                with instrumental.open("wb") as target:
                    for chunk in source.iter_content(chunk_size=1024 * 1024):
                        target.write(chunk)
            subtitles.write_text(task["subtitles"], encoding="utf-8")
            update(job_id, 0.15)

            width = max(640, min(3840, int(task["video"].get("width", 1920))))
            height = max(360, min(2160, int(task["video"].get("height", 1080))))
            background = str(task["video"].get("backgroundColor", "#08080b")).lstrip("#")
            if len(background) != 6 or any(character not in "0123456789abcdefABCDEF" for character in background):
                background = "08080b"
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=0x{background}:s={width}x{height}:r=30",
                    "-i", str(instrumental),
                    "-vf", f"ass={subtitles.as_posix()}",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "320k", "-shortest", "-movflags", "+faststart", str(output),
                ],
                check=True,
            )
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("FFmpeg did not create the karaoke video.")
            update(job_id, 0.85)
            upload(task["output"], output)
            update(job_id, 0.96)
            complete = requests.post(
                f"{BASE_URL}/api/karaoke-v2/worker/render/jobs/{job_id}/complete",
                headers=HEADERS, json={"renderSize": output.stat().st_size}, timeout=30,
            )
            complete.raise_for_status()
            print(f"Completed video render {job_id}.")
            return 0
    except Exception as error:
        update(job_id, 0, failed=str(error))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
