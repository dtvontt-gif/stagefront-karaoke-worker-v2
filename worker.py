"""Process one queued StageFront Karaoke v2 vocal-separation job."""

from __future__ import annotations

import os
import subprocess
import sys
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
        f"{BASE_URL}/api/karaoke-v2/worker/jobs/{job_id}", headers=HEADERS, json=payload, timeout=30
    )
    response.raise_for_status()


def upload(destination: dict, path: Path) -> None:
    with path.open("rb") as audio:
        response = requests.put(
            destination["signedUrl"],
            headers={"x-upsert": "false"},
            data={"cacheControl": "3600"},
            files={"": (path.name, audio, "audio/wav")},
            timeout=900,
        )
    if not response.ok:
        raise RuntimeError(f"Stem upload failed ({response.status_code}): {response.text[:500]}")


def main() -> int:
    claim = requests.post(f"{BASE_URL}/api/karaoke-v2/worker/jobs/claim", headers=HEADERS, timeout=30)
    if claim.status_code == 204:
        print("No queued songs.")
        return 0
    claim.raise_for_status()
    task = claim.json()
    job_id = task["job"]["id"]

    try:
        with tempfile.TemporaryDirectory(prefix="stagefront-") as temp:
            work = Path(temp)
            suffix = {
                "audio/mpeg": ".mp3",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/flac": ".flac",
                "audio/mp4": ".m4a",
                "audio/x-m4a": ".m4a",
            }.get(task["source"]["mimeType"], ".audio")
            source_path = work / f"source{suffix}"
            with requests.get(task["source"]["url"], stream=True, timeout=900) as source:
                source.raise_for_status()
                with source_path.open("wb") as target:
                    for chunk in source.iter_content(chunk_size=1024 * 1024):
                        target.write(chunk)

            update(job_id, 0.1)
            output_root = work / "separated"
            subprocess.run(
                [sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", str(output_root), str(source_path)],
                check=True,
            )
            stem_dir = output_root / "htdemucs" / source_path.stem
            vocals = stem_dir / "vocals.wav"
            instrumental = stem_dir / "no_vocals.wav"
            if not vocals.is_file() or not instrumental.is_file():
                raise RuntimeError("Separator did not create both output tracks.")

            update(job_id, 0.8)
            upload(task["outputs"]["vocals"], vocals)
            upload(task["outputs"]["instrumental"], instrumental)
            update(job_id, 0.95)
            complete = requests.post(
                f"{BASE_URL}/api/karaoke-v2/worker/jobs/{job_id}/complete",
                headers=HEADERS,
                json={"vocalsSize": vocals.stat().st_size, "instrumentalSize": instrumental.stat().st_size},
                timeout=30,
            )
            complete.raise_for_status()
            print(f"Completed job {job_id}.")
            return 0
    except Exception as error:
        update(job_id, 0, failed=str(error))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
