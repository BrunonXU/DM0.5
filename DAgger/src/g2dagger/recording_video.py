"""Offline review video encoding, outside acquisition/control threads.

CFR video is a frame container for review; its PTS are not sensor timestamps.
Exact raw timestamp + host clock + observation ID mapping lives beside it.
"""
import json
import os
from pathlib import Path
import subprocess

from .recording_audit import audit_payload_archive
from .recording_observation import CAMERAS


def encode_review_videos(archive, output, *, fps=30):
    if not isinstance(fps, int) or not 1 <= fps <= 120:
        raise ValueError("integer review FPS must be 1..120")
    archive, output = Path(archive), Path(output)
    report = audit_payload_archive(archive)
    if not report["valid"]:
        raise ValueError(f"payload audit failed: {report['errors']}")
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise FileExistsError(output)
    partial.mkdir(parents=True)
    # Metadata only; image bytes are streamed one at a time.
    with (archive / "frames.jsonl").open() as handle:
        rows = [json.loads(line) for line in handle]
    for camera_index, camera in enumerate(CAMERAS):
        video = partial / f"{camera}.mp4"
        error_log = (partial / f"{camera}.encode.log").open("wb")
        process = subprocess.Popen([
            "ffmpeg", "-nostdin", "-v", "error", "-n", "-f", "image2pipe",
            "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264rgb",
            "-crf", "0", "-threads", "1", str(video)], stdin=subprocess.PIPE,
            stderr=error_log)
        error_log.close()
        try:
            with (partial / f"{camera}.timestamps.jsonl").open("x") as mapping:
                shape = None
                for i, row in enumerate(rows):
                    frame = row["frames"][camera_index]
                    current_shape = (frame["width"], frame["height"], frame["media_type"])
                    if shape is not None and current_shape != shape:
                        raise ValueError("video channel changes dimensions/encoding")
                    shape = current_shape
                    process.stdin.write((archive / frame["path"]).read_bytes())
                    mapping.write(json.dumps(dict(video_frame_index=i,
                        review_pts_numerator=i, review_pts_denominator=fps,
                        observation_id=row["observation_id"], source_frame=frame)) + "\n")
            process.stdin.close()
            if process.wait(timeout=120) != 0:
                raise RuntimeError(f"{camera} encoding failed; see partial encode log")
        except BaseException:
            process.kill()
            process.wait()
            process.stdin.close()
            raise
        probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                "-show_entries", "stream=nb_read_frames", "-of", "json", str(video)],
                               check=True, capture_output=True, text=True)
        if int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"]) != len(rows):
            raise ValueError(f"{camera} decoded frame count mismatch")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(video),
                        "-f", "null", "-"], check=True, capture_output=True)
    (partial / "manifest.json").write_text(json.dumps(dict(
        complete=True, training_ready=False, format="lossless-rgb-review-mp4",
        frame_count=len(rows), review_fps=fps, source_archive=str(archive.resolve()),
        timestamp_semantics="CFR PTS for review only; source clocks retained in sidecars",
        training_blockers=report["limitations"]), indent=2) + "\n")
    os.replace(partial, output)
    return output
