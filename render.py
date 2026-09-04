"""Render one queued StageFront karaoke project into an MP4 video."""

from __future__ import annotations

import os
import json
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_URL = os.environ["STAGEFRONT_WORKER_URL"].rstrip("/")
ASSET_DIR = Path(__file__).resolve().parent / "assets"
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


def media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


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
            intro_audio = ASSET_DIR / "stagefront-intro.wav"
            outro_video = ASSET_DIR / "stagefront-outro.mp4"
            if not intro_audio.is_file() or not outro_video.is_file():
                raise RuntimeError("StageFront intro or outro media is missing.")
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
            background_url = task["video"].get("backgroundImageUrl")
            intro_video_url = task["video"].get("introVideoUrl")
            intro_ms = max(0, min(15000, int(task["video"].get("introDurationMs", 0))))
            outro_ms = max(0, min(15000, int(task["video"].get("outroDurationMs", 0))))
            main_seconds = (intro_ms / 1000) + media_duration(instrumental)
            outro_seconds = outro_ms / 1000
            audio_filter = (
                f"[1:a]adelay={intro_ms}:all=1,apad[music];"
                "[2:a]volume=1.0[intro];"
                f"[music][intro]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.96,"
                f"atrim=duration={main_seconds:.3f},asetpts=PTS-STARTPTS,aresample=48000[mainaudio];"
                f"[4:a]aresample=48000,apad,atrim=duration={outro_seconds:.3f},asetpts=PTS-STARTPTS[outroaudio]"
            )
            if not intro_video_url:
                raise RuntimeError("StageFront intro animation is missing.")
            intro_video = work / "intro.mp4"
            with requests.get(
                intro_video_url,
                headers={"x-vercel-protection-bypass": HEADERS["x-vercel-protection-bypass"]},
                stream=True,
                timeout=300,
            ) as source:
                source.raise_for_status()
                with intro_video.open("wb") as target:
                    for chunk in source.iter_content(chunk_size=1024 * 1024):
                        target.write(chunk)
            intro_seconds = intro_ms / 1000
            intro_overlay = (
                f"[3:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,format=rgba,"
                f"fade=t=out:st={max(0, intro_seconds - 0.65):.3f}:d=0.65:alpha=1[introvisual];"
            )
            outro_segment = (
                f"[4:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30,trim=duration={outro_seconds:.3f},"
                "setpts=PTS-STARTPTS[outrovisual];"
            )
            if background_url:
                image_suffix = Path(urlparse(background_url).path).suffix.lower()
                background_image = work / f"background{image_suffix if image_suffix in {'.jpg', '.jpeg', '.png', '.webp'} else '.jpg'}"
                image_headers = {"x-vercel-protection-bypass": HEADERS["x-vercel-protection-bypass"]} if task["video"].get("backgroundImageIsTemplate") else None
                with requests.get(background_url, headers=image_headers, stream=True, timeout=300) as source:
                    source.raise_for_status()
                    with background_image.open("wb") as target:
                        for chunk in source.iter_content(chunk_size=1024 * 1024):
                            target.write(chunk)
                command = [
                    "ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(background_image),
                    "-i", str(instrumental),
                    "-i", str(intro_audio), "-i", str(intro_video), "-i", str(outro_video),
                    "-filter_complex", f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[background];{intro_overlay}[background][introvisual]overlay=enable='lt(t,{intro_seconds:.3f})'[composite];[composite]ass={subtitles.as_posix()},fps=30,trim=duration={main_seconds:.3f},setpts=PTS-STARTPTS[mainvideo];{outro_segment}{audio_filter};[mainvideo][mainaudio][outrovisual][outroaudio]concat=n=2:v=1:a=1[video][audio]",
                    "-map", "[video]", "-map", "[audio]",
                ]
            else:
                command = [
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=0x{background}:s={width}x{height}:r=30",
                    "-i", str(instrumental),
                    "-i", str(intro_audio), "-i", str(intro_video), "-i", str(outro_video),
                    "-filter_complex", f"{intro_overlay}[0:v][introvisual]overlay=enable='lt(t,{intro_seconds:.3f})'[composite];[composite]ass={subtitles.as_posix()},fps=30,trim=duration={main_seconds:.3f},setpts=PTS-STARTPTS[mainvideo];{outro_segment}{audio_filter};[mainvideo][mainaudio][outrovisual][outroaudio]concat=n=2:v=1:a=1[video][audio]",
                    "-map", "[video]", "-map", "[audio]",
                ]
            command.extend([
                "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "320k", "-shortest", "-movflags", "+faststart", str(output),
            ])
            subprocess.run(command, check=True)
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("FFmpeg did not create the karaoke video.")
            update(job_id, 0.85)
            upload(task["output"], output)
            update(job_id, 0.96)
            complete = requests.post(
                f"{BASE_URL}/api/karaoke-v2/worker/render/jobs/{job_id}/complete",
                headers=HEADERS, json={"renderSize": output.stat().st_size, "storageKey": task["output"]["path"]}, timeout=30,
            )
            complete.raise_for_status()
            print(f"Completed video render {job_id}.")
            return 0
    except Exception as error:
        update(job_id, 0, failed=str(error))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
