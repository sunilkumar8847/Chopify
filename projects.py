"""Shared, resumable local project pipeline for CLI and dashboard."""
import hashlib
import json
import subprocess
import math
import re
import threading
import time
import uuid
from pathlib import Path

import download_and_transcribe as dt
import boundaries
import framing
import applog
import highlights
import hinglish
import llm
import render_clips
import score_clips
import evidence
import visual
import ingest
import sections
import branding
import subtitles
import transcription
from captions import update_transcript

DEFAULT_OPTIONS = {"aspect": "16:9", "style": "default", "tighten": False,
                   # Phase 4: -14 LUFS two-pass normalization for new projects
                   # (saved projects keep the value they were created with).
                   "loudnorm": True, "whisper_model": "medium", "device": "cpu",
                   "min_len": 15, "max_len": 300, "max_clips": None,
                   "min_score": 8.0, "preferred_len": None,
                   # Phase 2: AI highlight judging (falls back to rule scores
                   # when Ollama or the model is missing).
                   "highlights": True, "llm_provider": "ollama",
                   "llm": "qwen2.5:3b",
                   # Phase 3: keep each clip's setup and payoff (auto) or the
                   # discovered boundaries exactly (fixed).
                   "duration_mode": "auto",
                   "search": None, "semantic": None,
                   "embed_model": "nomic-embed-text", "brand": None,
                   "layout": "single", "centers": [0.25, 0.75], "caption": None,
                   # Phase 4: how vertical/square output is framed.
                   "framing": "auto",
                   # Output policy: generated text is Roman-script Hinglish.
                   # True also asks the LLM to polish transliterated spelling
                   # (validated word by word; rules alone are the default).
                   "hinglish_ai": False,
                   # Speed profile and subtitle-first transcripts. These
                   # defaults keep saved projects exactly as they were;
                   # new projects get NEW_PROJECT_DEFAULTS.
                   "profile": "accurate", "subtitles": "off",
                   # Phase 1: "local" (faster-whisper) or an AI Gateway route
                   # such as "groq:whisper-large-v3-turbo".
                   "transcriber": "local"}

# What a project created today starts with (unless the caller chose).
# whisper_model None means "the profile's model"; device auto picks the GPU
# when it works and warns visibly when it falls back to CPU.
NEW_PROJECT_DEFAULTS = {"profile": "balanced", "subtitles": "auto",
                        "device": "auto", "whisper_model": None,
                        # YouTube Shorts: vertical 1080x1920, 15-120 s clips.
                        "aspect": "9:16", "min_len": 15, "max_len": 120}
# Hard limit for vertical Shorts (YouTube allows 180 s; Chopify keeps 120).
SHORTS_MAX_SECONDS = 120


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _retry_sharing(action, attempts=20, delay=0.05):
    """Retry briefly when Windows reports the file is open in another thread.

    Replacing or reading project.json while Studio polls it can raise
    PermissionError for a few milliseconds on Windows.
    """
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    _retry_sharing(lambda: temp.replace(path))


def read_json(path):
    return json.loads(_retry_sharing(
        lambda: Path(path).read_text(encoding="utf-8-sig")))


def file_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return [str(path), stat.st_size, stat.st_mtime_ns]


def validate_options(options):
    unknown = set(options) - set(DEFAULT_OPTIONS)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    result = {**DEFAULT_OPTIONS, **options}
    if result["aspect"] not in render_clips.ASPECTS or result["style"] not in render_clips.CAPTION_STYLES:
        raise ValueError("Unknown aspect ratio or caption style.")
    if result["layout"] not in {"single", "split"}:
        raise ValueError("Layout must be single or split.")
    if result["layout"] == "split" and result["aspect"] != "9:16":
        raise ValueError("Podcast split-screen requires aspect 9:16.")
    for key in ("min_len", "max_len"):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or not 1 <= result[key] <= 3600:
            raise ValueError(f"{key} must be an integer from 1 to 3600.")
    if result["aspect"] == "9:16" and result["max_len"] > SHORTS_MAX_SECONDS:
        # Hard limit, not a suggestion: Shorts never exceed 120 s.
        result["max_len"] = SHORTS_MAX_SECONDS
    if result["min_len"] > result["max_len"]:
        raise ValueError("Minimum clip length cannot exceed maximum clip length.")
    if result["max_clips"] is not None and (
            isinstance(result["max_clips"], bool) or not isinstance(result["max_clips"], int)
            or not 1 <= result["max_clips"] <= 3600):
        raise ValueError("max_clips must be an integer from 1 to 3600 or null.")
    if result["min_score"] is not None and (not isinstance(result["min_score"], (int, float))
            or not math.isfinite(result["min_score"]) or not 0 <= result["min_score"] <= 10):
        raise ValueError("Minimum score must be between 0 and 10.")
    if result["preferred_len"] is not None:
        try:
            preferred = score_clips.parse_duration(result["preferred_len"])
        except ValueError as exc:
            raise ValueError(f"preferred_len is invalid. {exc}") from exc
        preferred = float(max(result["min_len"], min(result["max_len"], preferred)))
        result["preferred_len"] = int(preferred) if preferred.is_integer() else preferred
    if result["device"] not in {"cpu", "cuda", "auto"}:
        raise ValueError("Device must be cpu, cuda or auto.")
    if result["profile"] is None:
        result["profile"] = DEFAULT_OPTIONS["profile"]
    if result["profile"] not in transcription.PROFILES:
        raise ValueError("profile must be accurate, balanced or fast.")
    if result["subtitles"] is None:
        result["subtitles"] = DEFAULT_OPTIONS["subtitles"]
    if result["subtitles"] not in transcription.SUBTITLE_MODES:
        raise ValueError("subtitles must be auto or off.")
    if result["whisper_model"] == "":
        result["whisper_model"] = None
    if result["transcriber"] is None:
        result["transcriber"] = "local"
    if result["transcriber"] != "local":
        from ai import registry
        from ai.compat import is_cloud_route
        provider, _, model = str(result["transcriber"]).partition(":")
        if (not is_cloud_route(result["transcriber"]) or provider not in registry.REGISTRY
                or not registry.REGISTRY[provider].catalogue_supports("transcribe", model)):
            raise ValueError("transcriber must be local or a provider:model that can transcribe.")
    if result["search"] and result["semantic"]:
        raise ValueError("Choose keyword search or semantic search, not both.")
    centers = result["centers"]
    if not isinstance(centers, (list, tuple)) or len(centers) != 2 or any(
            not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in centers):
        raise ValueError("Provide two speaker centers between 0 and 1.")
    for key in ("whisper_model", "embed_model", "llm", "search", "semantic", "brand"):
        if result[key] is not None and (not isinstance(result[key], str) or len(result[key]) > 2000):
            raise ValueError(f"{key} must be text.")
    for key in ("tighten", "loudnorm"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} must be true or false.")
    if result["highlights"] is None:
        result["highlights"] = DEFAULT_OPTIONS["highlights"]
    if not isinstance(result["highlights"], bool):
        raise ValueError("highlights must be true or false.")
    if result["llm_provider"] is None:
        result["llm_provider"] = DEFAULT_OPTIONS["llm_provider"]
    if result["llm_provider"] not in llm.PROVIDERS:
        raise ValueError(f"Unknown LLM provider: {result['llm_provider']}")
    if result["framing"] is None:
        result["framing"] = DEFAULT_OPTIONS["framing"]
    if result["framing"] not in framing.MODES:
        raise ValueError("framing must be auto, track, center or fit.")
    if result["hinglish_ai"] is None:
        result["hinglish_ai"] = False
    if not isinstance(result["hinglish_ai"], bool):
        raise ValueError("hinglish_ai must be true or false.")
    if result["duration_mode"] is None:
        result["duration_mode"] = DEFAULT_OPTIONS["duration_mode"]
    if result["duration_mode"] not in boundaries.MODES:
        raise ValueError("duration_mode must be auto or fixed.")
    if result["caption"] is not None:
        result["caption"] = branding.validate_caption(result["caption"])
    return result


def score_options(options):
    keys = ("llm", "search", "semantic", "embed_model", "min_score",
            "max_clips", "min_len", "max_len", "preferred_len")
    return {key: options[key] for key in keys}


STAGE_OPERATIONS = {"source": "source.download", "transcribe": "transcribe",
                    "discover": "discover", "select": "select", "render": "render",
                    "preview": "render"}


def text_converter(path, options, log=None):
    """The project's Hinglish converter (rules; plus AI spelling if enabled)."""
    provider = None
    if options.get("hinglish_ai") and options.get("llm"):
        try:
            provider = llm.get_provider(options.get("llm_provider") or "ollama",
                                            options["llm"])
        except ValueError:
            provider = None
    return hinglish.Converter(path, provider, log or (lambda m: print(m, flush=True)))


def apply_text_policy(path, options=None, converter=None):
    """Rewrite generated text in segments.json as Roman Hinglish.

    Hooks (clip titles and file names) and AI judge text are converted; the
    original hook is kept in ``hook_original``. Idempotent and cheap, so it
    also upgrades projects selected before the policy existed, without
    re-running discovery or judging. Returns the number of changed fields.
    """
    path = Path(path)
    segments_path = path / "segments.json"
    if not segments_path.is_file():
        return 0
    segments = read_json(segments_path)
    converter = converter or text_converter(path, options or {})
    changed = 0
    for segment in segments:
        if hinglish.has_devanagari(segment.get("hook")):
            segment.setdefault("hook_original", segment["hook"])
            segment["hook"] = converter.text(segment["hook"])
            changed += 1
        judge = segment.get("judge")
        if isinstance(judge, dict):
            for field in ("summary", "reason"):
                if hinglish.has_devanagari(judge.get(field)):
                    judge[field] = converter.text(judge[field])
                    changed += 1
        if hinglish.has_devanagari(segment.get("reason")):
            segment["reason"] = converter.text(segment["reason"])
            changed += 1
    if changed:
        atomic_json(segments_path, segments)
    # Exported clips' metadata files (titles, descriptions, hashtags) are
    # text only, so they are upgraded in place. Burned-in captions and SRT
    # are not: they must match the video, so "Export again" redoes both.
    for meta_path in sorted((path / "clips").glob("*.meta.json")):
        try:
            meta = read_json(meta_path)
        except (OSError, ValueError):
            continue
        fields = [k for k in ("title", "description") if hinglish.has_devanagari(meta.get(k))]
        tags = meta.get("hashtags") or []
        if fields or any(hinglish.has_devanagari(t) for t in tags):
            for key in fields:
                meta.setdefault(f"{key}_original", meta[key])
                meta[key] = converter.text(meta[key])
            meta["hashtags"] = [t for t in (hinglish.romanize(t) for t in tags) if t.strip("#")]
            atomic_json(meta_path, meta)
            changed += 1
    return changed


def legacy_caption_clips(path):
    """Exported clips whose captions predate the Roman Hinglish policy."""
    clips = []
    for srt in sorted((Path(path) / "clips").glob("*.srt")):
        try:
            if hinglish.has_devanagari(srt.read_text(encoding="utf-8")):
                clips.append(srt.with_suffix(".mp4").name)
        except OSError:
            continue
    return clips


def score_keys(transcript_key, data, options):
    """Return (discovery_key, selection_key) for split stage caching."""
    discovery_options = {key: options[key] for key in (
        "search", "semantic", "embed_model", "min_len", "max_len",
        "preferred_len")}
    if transcription.needs_refine(data):
        # Caption transcripts change where clips are refined with Whisper;
        # that must not re-run discovery. Their content is the caption
        # track (digest) plus any manual edits.
        data = {**{k: v for k, v in data.items()
                   if k not in ("words", "segments", "refined")},
                "edits": [r for r in data.get("refined") or [] if r.get("by") == "edit"]}
    discovery_key = digest([transcript_key, data, discovery_options,
                            evidence.EVIDENCE_VERSION,
                            visual.FEATURE_VERSION])
    selection_options = {key: options[key] for key in (
        "min_score", "max_clips", "llm")}
    selection_options["hooks"] = score_clips.HOOK_VERSION
    if options.get("highlights"):
        # Judge results are cached separately, so re-selecting is cheap.
        selection_options.update(
            highlights=[highlights.VERSION, highlights.PROMPT_VERSION],
            llm_provider=options.get("llm_provider"),
            embed_model=options.get("embed_model"))
    if options.get("duration_mode", "auto") == "auto":
        selection_options.update(
            duration_mode=["auto", boundaries.VERSION],
            aspect=options.get("aspect"), min_len=options.get("min_len"),
            max_len=options.get("max_len"),
            preferred_len=options.get("preferred_len"))
    selection_key = digest([discovery_key, selection_options])
    return discovery_key, selection_key


def project_path(root, project_id):
    if not re.fullmatch(r"[a-f0-9]{12}", project_id):
        raise ValueError("Invalid project ID.")
    return Path(root) / project_id


def ai_defaults(options):
    """New projects follow the AI settings' routes (recorded per project, so a
    later change of settings never re-transcribes or re-judges old projects)."""
    options = options or {}
    try:
        from ai import policy
        routes = policy.load().get("routes") or {}
    except Exception:                       # noqa: BLE001 - settings unreadable: local
        return {}
    out = {}
    talk = routes.get("transcribe") or {}
    if "transcriber" not in options and talk.get("provider") not in (None, "local"):
        out["transcriber"] = f"{talk['provider']}:{talk['model']}"
    text = routes.get("generate") or {}
    if ("llm_provider" not in options and "llm" not in options
            and text.get("provider") not in (None, "local")):
        out.update(llm_provider="gateway", llm=f"{text['provider']}:{text['model']}")
    return out


def create_project(root, source, options):
    project_id = uuid.uuid4().hex[:12]
    return init_project(project_path(root, project_id), source, options, project_id)


def init_project(path, source, options, project_id=None):
    if not isinstance(source, str) or not source.strip() or len(source) > 8000:
        raise ValueError("Provide a video path or URL.")
    options = validate_options({**NEW_PROJECT_DEFAULTS, **ai_defaults(options), **{
        k: v for k, v in (options or {}).items()
        if not (k in NEW_PROJECT_DEFAULTS and v is None)}})
    source = source.strip()
    if Path(source).expanduser().is_file():
        source = str(Path(source).expanduser().resolve())
    project_id = project_id or uuid.uuid4().hex[:12]
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    state = {"id": project_id, "source": source.strip(), "options": options,
             "status": "new", "stage": "waiting", "created": time.time(),
             "updated": time.time(), "cache": {}, "renders": {}, "error": None,
             # New projects clean up supporting files once every final clip
             # is uploaded and verified on YouTube (final clips are kept).
             "storage": {"auto_cleanup": True}}
    save_project(path, state)
    return state


def read_project(path):
    return read_json(Path(path) / "project.json")


def save_project(path, state):
    state["updated"] = time.time()
    atomic_json(Path(path) / "project.json", state)


_STATE_LOCK = threading.RLock()


def update_project(path, mutate):
    """Read the latest project.json, apply ``mutate(state)``, save, return it.

    Background threads (the live recorder) use this so they only change the
    fields they own instead of overwriting a stale copy of the whole state.
    """
    with _STATE_LOCK:
        state = read_project(path)
        mutate(state)
        save_project(path, state)
        return state


def edit_transcript(path, words):
    path = Path(path)
    old = read_json(path / "transcript.json")
    data = transcription.lock_edits(old, update_transcript(old, words))
    atomic_json(path / "transcript.json", data)
    state = read_project(path)
    # Preserve candidate time ranges for manual editing; changed words invalidate renders.
    state["status"] = "review"
    save_project(path, state)
    return data


def length_problem(segment, options):
    """Why a clip may not be rendered for these options, or None."""
    hard = boundaries.ceiling(options.get("max_len"), options.get("aspect"))
    length = float(segment["end"]) - float(segment["start"])
    if length > hard + 0.05:
        return (f"Clip is {length:.0f}s; the limit for {options.get('aspect')} is "
                f"{hard:.0f}s. Shorten it in the clip editor.")
    return None


def check_lengths(segments, options):
    """Raise when any edited clip is longer than the project's hard limit."""
    for index, segment in enumerate(segments, 1):
        problem = length_problem(segment, options)
        if problem:
            raise ValueError(f"Clip {index}: {problem}")


def validate_segments(segments, duration):
    if not isinstance(segments, list):
        raise ValueError("Candidates must be a list.")
    if not segments:
        return []
    result = []
    for item in segments:
        if not isinstance(item, dict):
            raise ValueError("Each clip must be an object.")
        start, end = float(item["start"]), float(item["end"])
        score = float(item.get("overall", 0))
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
            raise ValueError("Clip times must be inside the source video.")
        if not math.isfinite(score) or not 0 <= score <= 10:
            raise ValueError("Clip score must be between 0 and 10.")
        record = dict(item)
        record.update(start=start, end=end,
                      hook=str(item.get("hook", "clip"))[:200], overall=score)
        result.append(record)
    return result


def observe_scenes(path, state, stage=None):
    """Phase 4 (observation only): label gameplay / breaks in the source video.

    Writes scenes.json and a summary in project.json; nothing is cut. A
    failure never stops the project.
    """
    import features
    if not features.enabled("scene_detection"):
        return None
    try:
        data = read_json(path / "transcript.json")
        video = Path(data.get("video") or "")
        if not video.is_file() or video.suffix.lower() not in ingest.VIDEO_EXTS:
            return None
        import scenes
        key = f"{scenes.VERSION}:{file_identity(video)}"
        if (state.get("cache") or {}).get("scenes") == key and (path / "scenes.json").exists():
            return state.get("scenes")
        if stage:
            stage("scenes")
        result = scenes.analyze(video, out=path / "scenes.json")
        state["scenes"] = {**result["summary"], "mode": result["mode"],
                           "count": len(result["scenes"])}
        state.setdefault("cache", {})["scenes"] = key
        save_project(path, state)
        applog.info("scenes.done", "Scenes classified (observation only)",
                    gameplay_share=result["summary"]["gameplay_share"],
                    scenes=len(result["scenes"]))
        return state["scenes"]
    except Exception as exc:              # noqa: BLE001 - observation must not break a run
        applog.warning("scenes.failed", f"Scene classification skipped: {exc}")
        return None


def run_project(path, action="all", preview=False, pick=None, resume=True, progress=None):
    """Prepare candidates or render saved candidates; checkpoint every successful clip."""
    # Log lines written during this run carry the project id (only this run:
    # a CLI batch runs several projects in one process).
    with applog.bound(project_id=Path(path).resolve().name):
        return _run_project(path, action, preview, pick, resume, progress)


def _run_project(path, action, preview, pick, resume, progress):
    path = Path(path).resolve()
    state = read_project(path)
    options = validate_options(state["options"])
    if action not in {"all", "prepare", "render"}:
        raise ValueError("Unknown project action.")
    current = {"name": None, "started": time.time()}
    applog.info("job.run", f"{action}{' preview' if preview else ''} started in worker",
                action=action)

    def stage(name):
        finish_stage()
        current["name"], current["started"] = name, time.time()
        applog.info(STAGE_OPERATIONS.get(name.split()[0], "stage"),
                    f"{name} started", stage=name, event="started")
        state.update(status="running", stage=name, error=None,
                     progress={"stage": name, "done": 0, "total": 0,
                               "detail": ""})
        save_project(path, state)
        print(f"[{state['id']}] {name}", flush=True)
        if progress:
            progress(name)

    def finish_stage():
        if current["name"]:
            applog.info(STAGE_OPERATIONS.get(current["name"].split()[0], "stage"),
                        f"{current['name']} completed", stage=current["name"],
                        event="completed",
                        seconds=round(time.time() - current["started"], 2))
        current["name"] = None

    def transcribe_progress(done, total):
        state["progress"] = {"stage": "transcribe", "done": int(done),
                             "total": int(total),
                             "detail": f"chunk {done}/{total}"}
        save_project(path, state)

    last_report = {"at": 0.0}

    def audio_progress(done, total):
        # Seconds of audio transcribed; saved at most every 5 seconds.
        now = time.time()
        if now - last_report["at"] < 5 and done < total:
            return
        last_report["at"] = now
        started = (state.get("transcription") or {}).get("started") or now
        detail = f"{_clock(done)} of {_clock(total)} transcribed"
        if done > 0 and now - started > 20:
            left = (now - started) / done * (total - done)
            detail += f" · about {max(1, round(left / 60))} min left"
        state["progress"] = {"stage": "transcribe", "done": int(done),
                             "total": int(total), "detail": detail}
        save_project(path, state)

    try:
        if action in {"all", "prepare"}:
            stage("source")
            source_input = state["source"]
            local = Path(source_input).expanduser()
            is_url = ingest.is_url(source_input)
            if not is_url and not local.is_file():
                raise ValueError(
                    f"Video file does not exist or URL is invalid: {source_input}")
            if not is_url and local.suffix.lower() not in ingest.VIDEO_EXTS:
                raise ValueError(
                    "Use a video file: MP4, MKV, WebM, MOV, M4V or AVI.")
            identity = file_identity(local) if local.is_file() else source_input
            source_key = digest(identity)
            cached_meta = (resume
                           and state["cache"].get("source_meta") == source_key
                           and isinstance(state.get("source_meta"), dict)
                           # live/upcoming status changes; always re-check it
                           and state["source_meta"].get("live_status")
                           not in ingest.TRANSIENT_LIVE)
            if cached_meta:
                meta = state["source_meta"]
            else:
                meta = ingest.probe_source(source_input)
                meta.pop("formats", None)
                meta = {**meta, "long": ingest.is_long_source(meta)}
                state["source_meta"] = meta
                state["cache"]["source_meta"] = source_key
                save_project(path, state)
            if is_url and meta.get("live_status") in ingest.BLOCKED_LIVE:
                # Downloading a live URL would record until the stream ends,
                # silently and with no way to stop it. Recording is separate.
                raise ValueError(ingest.live_block_message(meta))
            long = bool(meta.get("long", ingest.is_long_source(meta)))

            source = Path(state.get("video", "__missing__"))
            media_kind = str(meta.get("media_kind") or "")
            render_from_sections = bool(meta.get("render_from_sections", False))
            if (not resume or state["cache"].get("source") != source_key
                    or not source.is_file()):
                if is_url and long:
                    ingest.check_disk(path, None)
                    source, media_kind = ingest.download_discovery(
                        source_input, path, meta)
                    render_from_sections = True
                elif is_url:
                    ingest.check_disk(path, meta.get("estimated_size"))
                    source = dt.resolve_source(source_input, path)
                    media_kind = "video"
                    render_from_sections = False
                else:
                    source = dt.resolve_source(source_input, path)
                    media_kind = "video"
                    render_from_sections = False
                if not media_kind:
                    media_kind = ("audio"
                                  if source.suffix.lower() in ingest.AUDIO_EXTS
                                  else "video")
                state["video"] = str(source)
                state["cache"]["source"] = source_key
                state["cache"]["source_media"] = digest(file_identity(source))
                meta = {**meta, "long": long, "media_kind": media_kind,
                        "render_from_sections": render_from_sections}
                if is_url:
                    meta["discovery"] = str(source)
                state["source_meta"] = meta
                save_project(path, state)
            else:
                if not media_kind:
                    media_kind = ("audio"
                                  if source.suffix.lower() in ingest.AUDIO_EXTS
                                  else "video")
                render_from_sections = bool(
                    render_from_sections and is_url and long)
                meta = {**meta, "long": long, "media_kind": media_kind,
                        "render_from_sections": render_from_sections}
                if is_url:
                    meta["discovery"] = str(source)
                state["source_meta"] = meta

            stage("transcribe")
            settings = transcription.plan(options)
            transcript_key = digest([
                dt.TRANSCRIPT_VERSION, file_identity(source),
                settings["model"], options["device"], bool(long),
                media_kind, bool(render_from_sections), source_input,
                *transcription.cache_key_parts(settings)])
            transcript_path = path / "transcript.json"
            if (not resume or state["cache"].get("transcript") != transcript_key
                    or not transcript_path.exists()):
                duration = meta.get("duration")
                if duration is None:
                    try:
                        duration = ingest.probe_source(
                            str(source)).get("duration")
                    except (OSError, ValueError):
                        duration = None
                media_only = (is_url and long and media_kind == "video"
                              and meta.get("has_audio") is False)
                if media_only and not duration:
                    raise ValueError(
                        "Cannot determine the duration of the silent source.")
                source_identity = file_identity(source)
                if media_only:
                    dt.write_media_transcript(
                        source, path, duration, source=source_input,
                        media_kind=media_kind,
                        render_from_sections=render_from_sections,
                        identity=source_identity)
                else:
                    runtime = transcription.Runtime(settings)
                    state["transcription"] = {
                        **runtime.info(), "source": "whisper", "started": time.time(),
                        "estimate": transcription.estimate(
                            settings, duration, runtime.device == "cuda", is_url)}
                    save_project(path, state)
                    captions, refused = None, None
                    if is_url and settings["captions"] != "off":
                        stage("transcribe captions")
                        captions, refused = caption_transcript(
                            path, state, source, source_input, meta, duration,
                            runtime)
                        stage("transcribe")
                    if captions is not None:
                        captions.update({
                            "video": str(source), "transcript_version": dt.TRANSCRIPT_VERSION,
                            "chunked": False, "chunks": 0, "media_kind": media_kind,
                            "render_from_sections": bool(render_from_sections),
                            "source": str(source_input), "source_duration": captions["duration"],
                            "source_identity": source_identity})
                        atomic_json(transcript_path, captions)
                        state["transcription"].update(
                            source=captions["transcript_source"],
                            track=captions["captions"]["track"],
                            agreement=captions["captions"]["verification"]["similarity"])
                    else:
                        if refused:
                            print(f"Captions not used: {refused}. Transcribing with "
                                  "Whisper instead.", flush=True)
                            applog.warning("captions.refused", refused)
                            state["transcription"]["captions_refused"] = refused
                        runtime.release()          # dt.transcribe loads its own
                        dt.transcribe(
                            source, path, settings["model"], runtime.device,
                            runtime.compute,
                            duration=duration if long else None,
                            chunked=bool(long), source=source_input,
                            media_kind=media_kind,
                            render_from_sections=render_from_sections,
                            identity=source_identity,
                            progress=transcribe_progress,
                            decode=settings["decode"],
                            audio_progress=audio_progress)
                    runtime.release()
                    elapsed = time.time() - state["transcription"]["started"]
                    state["transcription"]["seconds"] = round(elapsed, 1)
                    if duration:
                        state["transcription"]["realtime_factor"] = round(
                            elapsed / float(duration), 3)
                    if (is_url and long and media_kind == "audio"
                            and not read_json(transcript_path).get("words")):
                        visual = ingest.download_visual_discovery(
                            source_input, path)
                        data = read_json(transcript_path)
                        data.update(video=str(visual), media_kind="video")
                        atomic_json(transcript_path, data)
                        source = visual
                        media_kind = "video"
                        source_identity = file_identity(source)
                        state["video"] = str(source)
                        state["source_meta"] = {
                            **state.get("source_meta", {}),
                            "media_kind": "video",
                            "discovery": str(visual)}
                        state["cache"]["source_media"] = digest(source_identity)
                        transcript_key = digest([
                            dt.TRANSCRIPT_VERSION, source_identity,
                            settings["model"], options["device"],
                            bool(long), media_kind,
                            bool(render_from_sections), source_input,
                            *transcription.cache_key_parts(settings)])
                        print("No speech; switching discovery to the "
                              "low-resolution visual video.", flush=True)
                state["cache"]["transcript"] = transcript_key
                state["source_identity"] = source_identity
                state["progress"] = {"stage": "transcribe", "done": 1,
                                     "total": 1, "detail": "complete"}
                save_project(path, state)
            stage("discover")
            data = read_json(transcript_path)
            discovery_key, selection_key = score_keys(transcript_key, data, options)
            candidates_path = path / "candidates.json"
            selection_path = path / "selection.json"
            segments_path = path / "segments.json"
            if options["search"] or options["semantic"]:
                combined_key = digest([discovery_key, selection_key])
                if (not resume or state["cache"].get("score") != combined_key
                        or not segments_path.exists()):
                    score_clips.run(path, **score_options(options))
                    state["cache"]["score"] = combined_key
                    save_project(path, state)
            else:
                if (not resume or state["cache"].get("discovery") != discovery_key
                        or not candidates_path.exists()):
                    score_clips.discover(
                        path, min_len=options["min_len"], max_len=options["max_len"],
                        preferred_len=options["preferred_len"])
                    state["cache"]["discovery"] = discovery_key
                    save_project(path, state)

                stage("select")
                if (not resume or state["cache"].get("selection") != selection_key
                        or not segments_path.exists() or not selection_path.exists()):
                    score_clips.select(
                        path,
                        min_score=options["min_score"] if options["min_score"] is not None else 8.0,
                        max_clips=options["max_clips"],
                        preferred_len=options["preferred_len"], llm=options["llm"],
                        highlights=options["highlights"],
                        llm_provider=options["llm_provider"],
                        embed_model=options["embed_model"],
                        duration_mode=options["duration_mode"],
                        min_len=options["min_len"], max_len=options["max_len"],
                        aspect=options["aspect"])
                    state["cache"]["selection"] = selection_key
                    save_project(path, state)

            state["selection"] = read_json(selection_path)
            apply_text_policy(path, options)
            observe_scenes(path, state, stage)
        if action == "prepare":
            state.update(status="review", stage="ready for review")
        else:
            stage("render preview" if preview else "render")
            data = read_json(path / "transcript.json")
            source = Path(data["video"]).resolve()
            section_source = str(data.get("source") or state["source"])
            if state.get("source_identity") and state["source_identity"] != file_identity(source):
                raise ValueError("Source video changed since transcription. Find clips again before rendering.")
            segments = validate_segments(read_json(path / "segments.json"), data["duration"])
            if not segments:
                stats = state.get("selection") or {}
                low = stats.get("score_min")
                high = stats.get("score_max")
                score = stats.get("min_score", options["min_score"] or 8.0)
                range_text = (f"Candidate scores {low:.1f}-{high:.1f}. "
                              if low is not None else "No candidates were found. ")
                raise ValueError(f"{range_text}No clips reached {score:.1f}. "
                                 f"Lower the minimum score or run this project with "
                                 f"--action prepare to review the range.")
            # Assign stable IDs before subset selection so names do not shift.
            segments = [{**s, "id": f"{i:03}"} for i, s in enumerate(segments, 1)]
            segments = score_clips.pick_segments(segments, pick)
            if transcription.should_refine(data, transcription.plan(options)):
                # Caption transcripts: exact Whisper word timing for just the
                # clips being rendered (cached per range).
                stage("transcribe clips")
                runtime = transcription.Runtime(transcription.plan(options))
                state.setdefault("transcription", {}).update(refine=runtime.info())
                def refine_progress(done, total):
                    state["progress"] = {"stage": "transcribe clips", "done": done,
                                         "total": total, "detail": f"clip range {done}/{total}"}
                    save_project(path, state)
                data, refined = transcription.refine(
                    path, data, [(float(s["start"]) - 2.0, float(s["end"]) + 2.0)
                                 for s in segments],
                    runtime, save=lambda d: atomic_json(path / "transcript.json", d),
                    progress=refine_progress)
                runtime.release()
                stage("render preview" if preview else "render")
            # Captions and SRT use the Roman Hinglish display transcript;
            # transcript.json keeps Whisper's original words.
            caption_words = hinglish.display_words(
                path, data, ranges=[(float(s["start"]) - 2.0, float(s["end"]) + 2.0)
                                    for s in segments],
                converter=text_converter(path, options))
            out = path / ("previews" if preview else "clips")
            out.mkdir(exist_ok=True)
            brand = branding.load_brand(options["brand"]) if options["brand"] else None
            logo_identity = file_identity(brand["logo"]) if brand and brand["logo"] else None
            render_from_sections = bool(
                (state.get("source_meta") or {}).get("render_from_sections")
                or data.get("render_from_sections"))
            cached_full = (sections.find_full_source(path)
                           if render_from_sections else None)
            if cached_full is not None:
                print(f"Using cached full source: {cached_full.name}", flush=True)
                render_from_sections = False
                source = cached_full
            section_plan = {}
            if render_from_sections:
                groups = sections.plan_sections(
                    segments, data.get("duration") or 0)
                for group_index, group in enumerate(groups, 1):
                    for clip_index in group["clips"]:
                        section_plan[clip_index] = (group_index, group)
                W, H = None, None
            else:
                W, H = render_clips.ffprobe_dims(source)
            loaded_sections = {}
            failed_sections = {}
            render_errors = []
            state["render_errors"] = render_errors
            outputs = []
            for i, segment in enumerate(segments, 1):
                stage(f"{'preview' if preview else 'render'} {i}/{len(segments)}")
                too_long = length_problem(segment, options)
                if too_long:              # never export over the hard limit
                    print(f"Clip {segment['id']}: {too_long}", flush=True)
                    render_errors.append({"clip": segment["id"], "error": too_long})
                    save_project(path, state)
                    continue
                render_source = source
                source_offset = 0.0
                section_key = None
                group_index = None
                if render_from_sections:
                    group_index, group = section_plan[i - 1]
                    source_offset = float(group["start"])
                    section_key = digest([
                        section_source, group["start"], group["end"],
                        sections.SECTION_VERSION])
                key = digest([segment, data["words"], file_identity(source),
                              options, brand, logo_identity, preview,
                              source_offset, section_key,
                              hinglish.POLICY_VERSION, render_clips.RENDER_VERSION])
                cached = state["renders"].get(key)
                expected_suffixes = [".mp4", ".srt"] if preview else [".mp4", ".srt", ".png", ".meta.json"]
                if resume and cached and all(Path(cached).with_suffix(ext).is_file() for ext in expected_suffixes):
                    outputs.append(cached)
                    continue
                if render_from_sections:
                    if group_index in failed_sections:
                        render_errors.append(
                            {"clip": segment["id"],
                             "error": failed_sections[group_index]})
                        continue
                    loaded = loaded_sections.get(group_index)
                    if loaded is None:
                        try:
                            render_source = sections.ensure_section(
                                section_source, path, group_index,
                                group["start"], group["end"])
                            W, H = render_clips.ffprobe_dims(render_source)
                            loaded_sections[group_index] = (
                                render_source, W, H)
                        except ingest.SectionDownloadTimeout as exc:
                            print(f"Section download stalled: {exc}", flush=True)
                            problem = ingest.full_download_problem(
                                path, (state.get("source_meta") or {}).get("estimated_size"))
                            if problem:
                                # Never fill the disk with a multi-hour video
                                # for a few clips: say why and stop here.
                                message = (f"{exc} Not falling back to the whole "
                                           f"video: {problem}. The other clips "
                                           "continue; retry this one later.")
                                print(message, flush=True)
                                applog.warning("render.section", message,
                                               clip=segment["id"])
                                failed_sections[group_index] = message
                                render_errors.append({"clip": segment["id"],
                                                      "error": message})
                                save_project(path, state)
                                continue
                            print("Downloading the full source once and "
                                  "rendering from it instead.", flush=True)
                            source = sections.ensure_full_source(
                                section_source, path)
                            render_from_sections = False
                            source_offset = 0.0
                            section_key = None
                            W, H = render_clips.ffprobe_dims(source)
                            render_source = source
                            key = digest([
                                segment, data["words"],
                                file_identity(source), options, brand,
                                logo_identity, preview, source_offset,
                                section_key, hinglish.POLICY_VERSION, render_clips.RENDER_VERSION])
                        except (Exception, SystemExit) as exc:
                            failed_sections[group_index] = str(exc)
                            render_errors.append(
                                {"clip": segment["id"], "error": str(exc)})
                            save_project(path, state)
                            continue
                    else:
                        render_source, W, H = loaded
                try:
                    output = render_clips.render({**segment, "id": segment["id"] + "-" + key[:8]}, caption_words, render_source, W, H, path,
                        options["aspect"], out_dir=out, style=options["style"],
                        do_tighten=options["tighten"], loudnorm=options["loudnorm"], preview=preview,
                        brand=brand, layout=options["layout"], centers=options["centers"],
                        caption=options["caption"], source_offset=source_offset,
                        framing_mode=options["framing"])
                except (Exception, SystemExit) as exc:
                    if not render_from_sections:
                        raise
                    render_errors.append(
                        {"clip": segment["id"], "error": str(exc)})
                    save_project(path, state)
                    continue
                state["renders"][key] = str(output)
                outputs.append(str(output))
                save_project(path, state)
            state["last_outputs"] = outputs
            if render_errors:
                message = f"{len(render_errors)} clip(s) failed to render."
                if not outputs:
                    raise ValueError(message + " Sections stay cached for retry.")
                state.update(status="failed", stage="render incomplete",
                             error=message + " Successful clips were kept.")
                save_project(path, state)
                return state
            if render_from_sections and not preview:
                sections.cleanup_sections(path)
            state.update(status="review" if preview else "complete", stage="preview ready" if preview else "complete")
        save_project(path, state)
        finish_stage()
        applog.info("job.run", f"{action} finished: {state['status']}",
                    outputs=len(state.get("last_outputs") or []) or None)
        return state
    except (Exception, SystemExit) as exc:
        applog.error(STAGE_OPERATIONS.get(str(current["name"] or "").split(" ")[0], "stage"),
                     f"{current['name'] or 'job'} failed", exc=exc,
                     stage=current["name"], event="failed",
                     seconds=round(time.time() - current["started"], 2))
        state.update(status="failed", error=str(exc), stage=state.get("stage", "failed"))
        save_project(path, state)
        raise


def _clock(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02}:{rest % 60:02}" if hours else f"{rest // 60}:{rest % 60:02}"


def caption_transcript(path, state, source, url, meta, duration, runtime):
    """Caption transcript dict for a URL, or (None, reason why not)."""
    tracks = meta.get("captions")
    if not tracks:
        return None, "YouTube reported no caption tracks for this video"
    try:
        local = float(ingest.probe_source(str(source)).get("duration") or 0) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        local = None
    return transcription.caption_transcript(
        path, url, source, local or duration, tracks, runtime)


def run_batch(sources, root, options, runner=None, **kwargs):
    if not isinstance(sources, list) or not sources or any(not isinstance(s, str) for s in sources):
        raise ValueError("Batch input must be a non-empty JSON list of video paths or URLs.")
    results = []
    import compliance
    for source in sources:
        if compliance.blocked(source):
            results.append({"id": None, "source": source, "status": "failed",
                            "error": compliance.MESSAGE})
            continue
        state = create_project(root, source, options)
        path = project_path(root, state["id"])
        try:
            result = (runner or run_project)(path, **kwargs)
            results.append({"id": state["id"], "source": source, "status": result["status"]})
        except (Exception, SystemExit) as exc:
            results.append({"id": state["id"], "source": source, "status": "failed", "error": str(exc)})
    return results
