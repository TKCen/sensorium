"""Manual talking-head artifact worker wrapper.

This module is intentionally a manual motor wrapper, not a Sensorium sensor,
cron, dispatcher, or outbox. It can plan or execute a local talking-head
pipeline and record only bounded artifact references back into Sensorium.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .artifacts import store_artifact
from .config import DEFAULT_TTS_CONFIG
from .schemas import utc_now_iso
from .sensors import classify_media_capacity, media_capacity_sample
from .store import SensoriumStore

DEFAULT_ARTIFACT_ROOT = "~/.hermes/artifacts/sensorium/talking-head"
# Generic loopback/engine defaults; resolved from env then the shared config
# defaults so no deployment-specific voice/model/url is baked into the code.
DEFAULT_TTS_BASE_URL = os.environ.get("SENSORIUM_TTS_BASE_URL", DEFAULT_TTS_CONFIG["base_url"])
DEFAULT_TTS_MODEL = os.environ.get("SENSORIUM_TTS_MODEL", DEFAULT_TTS_CONFIG["model"])
DEFAULT_TTS_VOICE = os.environ.get("SENSORIUM_TTS_VOICE", DEFAULT_TTS_CONFIG["voice"])
VALID_CROP_MODES = {"center-square", "copy", "existing"}


@dataclass(frozen=True)
class TalkingHeadRequest:
    script_file: Path
    source_still: Path
    slug: str
    thread_id: str = ""
    action_id: str = ""
    instance: str = "default"
    state_dir: str | None = None
    artifact_root: Path = Path(DEFAULT_ARTIFACT_ROOT).expanduser()
    real_run: bool = False
    crop_mode: str = "center-square"
    crop_path: Path | None = None
    lipsync_command: str = ""
    tts_base_url: str = DEFAULT_TTS_BASE_URL
    tts_model: str = DEFAULT_TTS_MODEL
    tts_voice: str = DEFAULT_TTS_VOICE
    tts_format: str = "wav"
    tts_speed: float = 1.0
    overwrite: bool = False
    source_plate_ref: str = ""
    source_prompt_hash: str = ""
    intended_handoff_mode: str = "present_thread"
    sensitivity: str = "private"


@dataclass(frozen=True)
class PlannedPaths:
    run_dir: Path
    script: Path
    audio: Path
    source_fullframe: Path
    face_crop: Path
    video: Path
    ffprobe: Path
    contact_sheet: Path
    manifest: Path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_slug(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    if not cleaned:
        cleaned = utc_now_iso().replace(":", "").replace("-", "").rstrip("Z")
    return cleaned[:120]


def _planned_paths(req: TalkingHeadRequest) -> PlannedPaths:
    slug = _safe_slug(req.slug)
    run_dir = req.artifact_root.expanduser() / slug
    audio_ext = req.tts_format.strip().lower() or "wav"
    if audio_ext == "ogg":
        audio_ext = "opus"
    return PlannedPaths(
        run_dir=run_dir,
        script=run_dir / "script.txt",
        audio=run_dir / f"chatterbox-audio.{audio_ext}",
        source_fullframe=run_dir / "source-fullframe.png",
        face_crop=run_dir / "source-face-crop.png",
        video=run_dir / "talking-head.mp4",
        ffprobe=run_dir / "ffprobe.json",
        contact_sheet=run_dir / "contact-sheet.jpg",
        manifest=run_dir / "manifest.json",
    )


def _file_meta(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {"exists": True, "size": stat.st_size, "sha256": _sha256_file(path)}


def _capacity_probe() -> dict:
    sample = media_capacity_sample()
    _signal, state = classify_media_capacity(sample, state={})
    return state.get("capacity_record") or {"status": state.get("status", "unknown")}


def _require_capacity_ok(probe: Callable[[], dict]) -> dict:
    record = probe()
    status = str(record.get("status") or "unknown")
    if status != "almost_idle":
        raise RuntimeError(
            "media_capacity_not_ok: "
            + json.dumps({
                "status": status,
                "reasons": record.get("reasons") or [],
                "unknown": record.get("unknown") or [],
            }, separators=(",", ":"))
        )
    return record


def _copy_source(src: Path, dst: Path, *, overwrite: bool) -> None:
    if dst.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing artifact: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _make_crop(req: TalkingHeadRequest, paths: PlannedPaths) -> None:
    if req.crop_mode not in VALID_CROP_MODES:
        raise ValueError(f"crop_mode must be one of {sorted(VALID_CROP_MODES)}")
    if req.crop_mode == "existing":
        if not req.crop_path:
            raise ValueError("crop_path is required when crop_mode=existing")
        _copy_source(req.crop_path, paths.face_crop, overwrite=req.overwrite)
        return
    if req.crop_mode == "copy":
        _copy_source(paths.source_fullframe, paths.face_crop, overwrite=req.overwrite)
        return

    if paths.face_crop.exists() and not req.overwrite:
        raise FileExistsError(f"Refusing to overwrite existing artifact: {paths.face_crop}")
    paths.face_crop.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y" if req.overwrite else "-n",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(paths.source_fullframe),
        "-vf",
        "crop='min(iw,ih)':'min(iw,ih)',scale=1024:1024",
        str(paths.face_crop),
    ]
    subprocess.run(cmd, check=True)


def _generate_chatterbox_audio(req: TalkingHeadRequest, script_text: str, output_path: Path) -> None:
    if output_path.exists() and not req.overwrite:
        raise FileExistsError(f"Refusing to overwrite existing artifact: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": req.tts_model,
        "voice": req.tts_voice,
        "input": script_text,
        "response_format": req.tts_format,
        "speed": req.tts_speed,
    }
    data = json.dumps(payload).encode("utf-8")
    endpoint = req.tts_base_url.rstrip("/") + "/audio/speech"
    http_req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_req, timeout=300) as resp:  # noqa: S310 - caller supplies local sidecar URL
            audio = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"tts_request_failed: {exc}") from exc
    if not audio:
        raise RuntimeError("tts_request_failed: empty audio response")
    output_path.write_bytes(audio)


def _render_lipsync_command(template: str, paths: PlannedPaths) -> str:
    mapping = {
        "audio": shlex.quote(str(paths.audio)),
        "source": shlex.quote(str(paths.source_fullframe)),
        "fullframe": shlex.quote(str(paths.source_fullframe)),
        "crop": shlex.quote(str(paths.face_crop)),
        "video": shlex.quote(str(paths.video)),
        "output": shlex.quote(str(paths.video)),
        "workdir": shlex.quote(str(paths.run_dir)),
    }
    return template.format_map(mapping)


def _run_lipsync_command(req: TalkingHeadRequest, paths: PlannedPaths) -> None:
    if not req.lipsync_command.strip():
        raise ValueError("--lipsync-command is required for --real-run")
    if paths.video.exists() and not req.overwrite:
        raise FileExistsError(f"Refusing to overwrite existing artifact: {paths.video}")
    cmd = _render_lipsync_command(req.lipsync_command, paths)
    subprocess.run(cmd, shell=True, check=True, cwd=str(paths.run_dir))  # noqa: S602 - explicit manual local invocation
    if not paths.video.exists():
        raise RuntimeError(f"lipsync_command_completed_without_video: {paths.video}")


def _write_qc(paths: PlannedPaths, *, overwrite: bool) -> None:
    if paths.ffprobe.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing artifact: {paths.ffprobe}")
    ffprobe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(paths.video)],
        check=True,
        capture_output=True,
        text=True,
    )
    paths.ffprobe.write_text(ffprobe.stdout)

    if paths.contact_sheet.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing artifact: {paths.contact_sheet}")
    subprocess.run(
        [
            "ffmpeg",
            "-y" if overwrite else "-n",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(paths.video),
            "-vf",
            "fps=1,scale=320:-1,tile=5x1",
            str(paths.contact_sheet),
        ],
        check=True,
    )


def _store_artifact_record(
    store: SensoriumStore,
    *,
    kind: str,
    ref_path: Path,
    provenance: dict,
    why_created: str,
    req: TalkingHeadRequest,
) -> dict:
    result = store_artifact(
        store,
        kind=kind,
        ref_path=str(ref_path),
        provenance=provenance,
        why_created=why_created,
        intended_handoff_mode=req.intended_handoff_mode,
        delivery_state="prepared" if req.real_run else "held_for_review",
        capacity_requirements={
            "requires_explicit_real_run": True,
            "requires_media_capacity_status": "almost_idle",
            "manual_only_no_autonomous_trigger": True,
        },
        source_thread_id=req.thread_id,
        source_action_id=req.action_id,
        feedback_hooks={"expected_review": "operator_qc_then_conscious_delivery_choice"},
        sensitivity=req.sensitivity,
        allowed_surfaces=["local"],
    )
    if not result.get("success"):
        raise RuntimeError(f"artifact_record_failed: {result.get('error')} {result.get('detail', '')}")
    return result["data"]


def _artifact_plan(req: TalkingHeadRequest, paths: PlannedPaths, script_text: str) -> list[tuple[str, Path, dict, str]]:
    operation = {
        "operation": "manual_talking_head_worker",
        "mode": "real" if req.real_run else "dry_run",
        "slug": _safe_slug(req.slug),
        "manual_only_no_autonomous_trigger": True,
    }
    script_hash = _sha256_text(script_text)
    source_hash = _sha256_file(req.source_still) if req.source_still.exists() else ""
    command_hash = _sha256_text(req.lipsync_command) if req.lipsync_command else ""

    return [
        (
            "text",
            paths.script,
            {**operation, "stage": "selected_script", "script_sha256": script_hash, "script_chars": len(script_text)},
            "Selected private script text for manual local talking-head generation.",
        ),
        (
            "image",
            paths.source_fullframe,
            {
                **operation,
                "stage": "source_fullframe",
                "source_sha256": source_hash,
                "source_plate_ref": req.source_plate_ref,
                "source_prompt_hash": req.source_prompt_hash,
            },
            "Selected/ref-locked source still preserved as full-frame plate.",
        ),
        (
            "image",
            paths.face_crop,
            {**operation, "stage": "face_crop", "crop_mode": req.crop_mode, "source_sha256": source_hash},
            "Face crop/source plate for local lipsync workflow.",
        ),
        (
            "audio",
            paths.audio,
            {
                **operation,
                "stage": "chatterbox_audio",
                "script_sha256": script_hash,
                "tts_model": req.tts_model,
                "tts_voice": req.tts_voice,
                "tts_format": req.tts_format,
                "tts_base_url_hash": _sha256_text(req.tts_base_url),
            },
            "Chatterbox audio component for manual talking-head generation.",
        ),
        (
            "video",
            paths.video,
            {
                **operation,
                "stage": "comfy_lipsync_video",
                "workflow_family": "ComfyUI InfiniteTalk/WanVideo lipsync",
                "lipsync_command_sha256": command_hash,
            },
            "Manual local ComfyUI lipsync MP4 for later conscious review; not delivered.",
        ),
        (
            "text",
            paths.ffprobe,
            {**operation, "stage": "ffprobe_qc", "video_path_ref": str(paths.video)},
            "ffprobe JSON QC record for the manual talking-head video.",
        ),
        (
            "image",
            paths.contact_sheet,
            {**operation, "stage": "contact_sheet_qc", "video_path_ref": str(paths.video)},
            "Contact sheet QC reference for identity/motion review.",
        ),
    ]


def run_talking_head_worker(
    req: TalkingHeadRequest,
    *,
    capacity_probe: Callable[[], dict] | None = None,
    tts_generator: Callable[[TalkingHeadRequest, str, Path], None] | None = None,
    crop_generator: Callable[[TalkingHeadRequest, PlannedPaths], None] | None = None,
    lipsync_runner: Callable[[TalkingHeadRequest, PlannedPaths], None] | None = None,
    qc_generator: Callable[[PlannedPaths], None] | None = None,
) -> dict:
    """Plan or execute the manual talking-head pipeline and store artifact refs."""
    if not req.thread_id and not req.action_id:
        raise ValueError("thread_id or action_id is required so artifacts attach to Sensorium context")
    if req.crop_mode not in VALID_CROP_MODES:
        raise ValueError(f"crop_mode must be one of {sorted(VALID_CROP_MODES)}")
    if not req.script_file.exists():
        raise FileNotFoundError(f"script_file not found: {req.script_file}")
    if not req.source_still.exists():
        raise FileNotFoundError(f"source_still not found: {req.source_still}")

    paths = _planned_paths(req)
    script_text = req.script_file.read_text(errors="ignore")
    if not script_text.strip():
        raise ValueError("script_file is empty")
    if req.real_run and not req.lipsync_command.strip():
        raise ValueError("--lipsync-command is required for --real-run")

    capacity_record: dict | None = None
    if req.real_run:
        capacity_record = _require_capacity_ok(capacity_probe or _capacity_probe)
        if paths.run_dir.exists() and any(paths.run_dir.iterdir()) and not req.overwrite:
            raise FileExistsError(f"Artifact run directory already exists; pass --overwrite or choose a new slug: {paths.run_dir}")
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        paths.script.write_text(script_text)
        _copy_source(req.source_still, paths.source_fullframe, overwrite=req.overwrite)
        (crop_generator or _make_crop)(req, paths)
        (tts_generator or _generate_chatterbox_audio)(req, script_text, paths.audio)
        (lipsync_runner or _run_lipsync_command)(req, paths)
        (qc_generator or (lambda p: _write_qc(p, overwrite=req.overwrite)))(paths)

    store = SensoriumStore(instance=req.instance, state_dir=req.state_dir)
    store.ensure_dirs()
    records = []
    for kind, path, provenance, why in _artifact_plan(req, paths, script_text):
        if capacity_record:
            provenance = {**provenance, "capacity_status": capacity_record.get("status")}
        if req.real_run:
            provenance = {**provenance, **_file_meta(path)}
        else:
            provenance = {**provenance, "planned_only": True}
        records.append(_store_artifact_record(store, kind=kind, ref_path=path, provenance=provenance, why_created=why, req=req))

    manifest = {
        "ts": utc_now_iso(),
        "mode": "real" if req.real_run else "dry_run",
        "manual_only_no_autonomous_trigger": True,
        "outbound_delivery": False,
        "slug": _safe_slug(req.slug),
        "run_dir": str(paths.run_dir),
        "artifact_ids": [r.get("id") for r in records],
        "paths": {k: str(v) for k, v in paths.__dict__.items()},
        "source_refs": {"thread_id": req.thread_id, "action_id": req.action_id},
    }
    if req.real_run:
        paths.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    return {
        "success": True,
        "mode": manifest["mode"],
        "outbound_delivery": False,
        "run_dir": str(paths.run_dir),
        "artifact_count": len(records),
        "artifact_ids": manifest["artifact_ids"],
        "capacity_status": (capacity_record or {}).get("status"),
        "manifest_path": str(paths.manifest),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual Sensorium talking-head artifact worker")
    parser.add_argument("--instance", default="default")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--action-id", default="")
    parser.add_argument("--script-file", required=True)
    parser.add_argument("--source-still", required=True)
    parser.add_argument("--slug", default=utc_now_iso().replace(":", "").replace("-", "").rstrip("Z"))
    parser.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--real-run", action="store_true", help="Actually generate media; default is dry-run planning only")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--crop-mode", choices=sorted(VALID_CROP_MODES), default="center-square")
    parser.add_argument("--crop-path", default=None)
    parser.add_argument("--lipsync-command", default="", help="Shell command template using {audio} {crop} {fullframe} {video} {workdir}")
    parser.add_argument("--tts-base-url", default=DEFAULT_TTS_BASE_URL)
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    parser.add_argument("--tts-voice", default=DEFAULT_TTS_VOICE)
    parser.add_argument("--tts-format", default="wav")
    parser.add_argument("--tts-speed", type=float, default=1.0)
    parser.add_argument("--source-plate-ref", default="")
    parser.add_argument("--source-prompt-hash", default="")
    parser.add_argument("--intended-handoff-mode", default="present_thread")
    parser.add_argument("--sensitivity", default="private")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    req = TalkingHeadRequest(
        script_file=Path(args.script_file),
        source_still=Path(args.source_still),
        slug=args.slug,
        thread_id=args.thread_id,
        action_id=args.action_id,
        instance=args.instance,
        state_dir=args.state_dir,
        artifact_root=Path(args.artifact_root).expanduser(),
        real_run=bool(args.real_run),
        crop_mode=args.crop_mode,
        crop_path=Path(args.crop_path) if args.crop_path else None,
        lipsync_command=args.lipsync_command,
        tts_base_url=args.tts_base_url,
        tts_model=args.tts_model,
        tts_voice=args.tts_voice,
        tts_format=args.tts_format,
        tts_speed=args.tts_speed,
        overwrite=bool(args.overwrite),
        source_plate_ref=args.source_plate_ref,
        source_prompt_hash=args.source_prompt_hash,
        intended_handoff_mode=args.intended_handoff_mode,
        sensitivity=args.sensitivity,
    )
    try:
        result = run_talking_head_worker(req)
    except Exception as exc:  # CLI should return compact machine-readable failure.
        print(json.dumps({"success": False, "error": str(exc)}, separators=(",", ":")))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
