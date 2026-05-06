#!/usr/bin/env python3

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

import requests

from .config import OUTPUT_DIR
from .drm import decrypt_segments, get_keys_cached
from .http_client import make_headers_for_ua
from .logging_config import get_logger, setup_logging
from .mpd_parser import parse_mpd, list_video_representations, list_audio_representations
from .muxer import mux_segments
from .recorder import record_stream
from .segment_selector import get_dvr_window, get_segment_numbers_in_range
from .state import stop_event, progress
from .sunnxt_api import fetch_sunnxt_mpd_info, fetch_live_channels
from .sunnxt_config import ASYNC_MAX_WORKERS
from .utils import to_ist, parse_user_time_ist

logger = get_logger(__name__)

# Global thread pool for running sync functions without blocking the event loop
_executor = ThreadPoolExecutor(max_workers=ASYNC_MAX_WORKERS)
# Semaphore to limit concurrent submissions (default = ASYNC_MAX_WORKERS)
_async_semaphore = asyncio.Semaphore(ASYNC_MAX_WORKERS)

# ----------------------------------------------------------------------
# Channel list caching
# ----------------------------------------------------------------------
_CHANNEL_CACHE: List[Dict[str, str]] = []
_CHANNEL_CACHE_TIME = 0.0
_CHANNEL_CACHE_TTL = 3600  # 1 hour


def _refresh_channel_cache() -> None:
    global _CHANNEL_CACHE, _CHANNEL_CACHE_TIME
    _CHANNEL_CACHE = fetch_live_channels()
    _CHANNEL_CACHE_TIME = time.time()
    logger.info("Channel cache refreshed: %d channels", len(_CHANNEL_CACHE))


def list_channels(force_refresh: bool = False) -> List[Dict[str, str]]:
    if force_refresh or not _CHANNEL_CACHE or time.time() - _CHANNEL_CACHE_TIME > _CHANNEL_CACHE_TTL:
        _refresh_channel_cache()
    seen = set()
    unique = []
    for ch in _CHANNEL_CACHE:
        if ch["_id"] not in seen:
            seen.add(ch["_id"])
            unique.append(ch)
    return unique


def get_channel_id(channel_name: str) -> str:
    channels = list_channels()
    name_lower = channel_name.strip().lower()
    for ch in channels:
        if ch["title"].lower() == name_lower:
            return ch["_id"]
    raise ValueError(f"Channel '{channel_name}' not found. Use list_channels() to see available names.")


def get_channel_name(media_id: str) -> str:
    channels = list_channels()
    for ch in channels:
        if ch["_id"] == media_id:
            return ch["title"]
    raise ValueError(f"Channel ID '{media_id}' not found.")


# ----------------------------------------------------------------------
# Codec helpers
# ----------------------------------------------------------------------
def _codec_to_short(codec: str) -> str:
    codec = codec.lower()
    if "avc" in codec or "h264" in codec:
        return "H264"
    if "hevc" in codec or "h265" in codec:
        return "H265"
    if "vp9" in codec:
        return "VP9"
    if "av1" in codec:
        return "AV1"
    return codec.upper()


def _audio_codec_to_short(codec: str) -> str:
    codec = codec.lower()
    if "aac" in codec:
        return "AAC"
    if "ac-3" in codec or "ac3" in codec:
        return "AC3"
    if "ec-3" in codec or "eac3" in codec:
        return "EAC3"
    if "mp4a" in codec:
        return "AAC"
    return codec.upper()


def _is_dolby_vision(info: Dict) -> bool:
    codec = info.get("v_codec", "").lower()
    return "dvh1" in codec or "dvhe" in codec


# ----------------------------------------------------------------------
# Quality listing with caching
# ----------------------------------------------------------------------
_QUALITY_CACHE: Dict[str, Tuple[List[Dict], List[Dict], float]] = {}  # media_id -> (video, audio, timestamp)
_QUALITY_CACHE_TTL = 600  # 10 minutes


def _fetch_qualities(media_id: str) -> Tuple[List[Dict], List[Dict]]:
    """Fetch MPD and extract video & audio representations."""
    mpd_url, _, _ = fetch_sunnxt_mpd_info(media_id)
    if not mpd_url:
        raise RuntimeError(f"Cannot fetch MPD for {media_id}")
    resp = requests.get(mpd_url, headers=make_headers_for_ua(0), timeout=20)
    resp.raise_for_status()
    mpd_text = resp.text
    video = list_video_representations(mpd_text)
    audio = list_audio_representations(mpd_text)
    return video, audio


def get_video_qualities(media_id: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Return list of video quality options (id, bandwidth, width, height, codecs, frameRate)."""
    now = time.time()
    if not force_refresh and media_id in _QUALITY_CACHE:
        video, _, ts = _QUALITY_CACHE[media_id]
        if now - ts < _QUALITY_CACHE_TTL:
            return video
    video, audio = _fetch_qualities(media_id)
    _QUALITY_CACHE[media_id] = (video, audio, now)
    return video


def get_audio_qualities(media_id: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Return list of audio quality options (id, bandwidth, codecs, lang, label, channels, samplerate, role)."""
    now = time.time()
    if not force_refresh and media_id in _QUALITY_CACHE:
        _, audio, ts = _QUALITY_CACHE[media_id]
        if now - ts < _QUALITY_CACHE_TTL:
            return audio
    video, audio = _fetch_qualities(media_id)
    _QUALITY_CACHE[media_id] = (video, audio, now)
    return audio


def get_best_video_quality(media_id: str) -> str:
    """Return representation ID of highest bandwidth video."""
    quals = get_video_qualities(media_id)
    if not quals:
        return "best"
    best = max(quals, key=lambda x: x.get("bandwidth", 0))
    return best["id"]


def get_best_audio_quality(media_id: str, preferred_lang: str = "") -> str:
    """Return representation ID of highest bandwidth audio, optionally filtered by language."""
    quals = get_audio_qualities(media_id)
    if not quals:
        return "best"
    if preferred_lang:
        lang_matches = [q for q in quals if q.get("lang", "").lower() == preferred_lang.lower()]
        if lang_matches:
            quals = lang_matches
    best = max(quals, key=lambda x: x.get("bandwidth", 0))
    return best["id"]


# ----------------------------------------------------------------------
# Metadata without segment download (optimized)
# ----------------------------------------------------------------------
def get_mpd_info_only(
    media_id: str,
    audio_quality: str = "best",
    audio_lang: str = "",
    cached_info: Optional[Dict] = None,
) -> Tuple[str, Dict]:
    """Fetch MPD URL and parsed info without triggering recording."""
    if cached_info and cached_info.get("_mpd_url") and cached_info.get("_parsed"):
        return cached_info["_mpd_url"], cached_info["_parsed"]
    mpd_url, lic_token, content_id = fetch_sunnxt_mpd_info(media_id)
    if not mpd_url:
        raise RuntimeError(f"Cannot fetch MPD for media_id {media_id}")

    resp = requests.get(mpd_url, headers=make_headers_for_ua(0), timeout=20)
    resp.raise_for_status()
    mpd_text = resp.text

    info = parse_mpd(
        mpd_text, mpd_url=mpd_url,
        audio_quality=audio_quality,
        audio_lang=audio_lang,
    )
    if not info or not info.get("v_time_to_num"):
        raise RuntimeError("MPD parse failed or no segments found.")
    info["_mpd_url"] = mpd_url
    return mpd_url, info


def get_recording_metadata(
    media_id: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    video_quality: str = "best",
    audio_quality: str = "best",
    audio_lang: str = "",
    info: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Return metadata for naming, without downloading segments."""
    if info is None:
        _, info = get_mpd_info_only(media_id, audio_quality=audio_quality, audio_lang=audio_lang)
    else:
        if "_mpd_url" not in info:
            _, info = get_mpd_info_only(media_id, audio_quality=audio_quality, audio_lang=audio_lang)

    channel_name = get_channel_name(media_id)

    start_ist = to_ist(start_dt) if start_dt else None
    end_ist = to_ist(end_dt) if end_dt else None

    height = info.get("v_height", 0)
    if height >= 2160:
        resolution = "2160p"
    elif height >= 1080:
        resolution = "1080p"
    elif height >= 720:
        resolution = "720p"
    elif height > 0:
        resolution = f"{height}p"
    else:
        resolution = "Unknown"

    video_codec = info.get("v_codec", "")
    video_codec_short = _codec_to_short(video_codec)

    audio_codec = info.get("a_codec", "")
    audio_codec_short = _audio_codec_to_short(audio_codec) if audio_codec else "AAC"
    audio_channels = info.get("a_channels", 0)
    if audio_channels == 2:
        audio_ch_str = "2.0"
    elif audio_channels == 6:
        audio_ch_str = "5.1"
    elif audio_channels == 8:
        audio_ch_str = "7.1"
    else:
        audio_ch_str = f"{audio_channels}.0" if audio_channels else "2.0"

    dolby_vision = _is_dolby_vision(info)

    duration_sec = (end_dt - start_dt).total_seconds() if (start_dt and end_dt) else None

    return {
        "channel_id": media_id,
        "channel_name": channel_name,
        "start_utc": start_dt,
        "end_utc": end_dt,
        "start_ist": start_ist,
        "end_ist": end_ist,
        "resolution": resolution,
        "height": height,
        "video_codec": video_codec,
        "video_codec_short": video_codec_short,
        "audio_codec": audio_codec,
        "audio_codec_short": audio_codec_short,
        "audio_channels": audio_channels,
        "audio_ch_str": audio_ch_str,
        "dolby_vision": dolby_vision,
        "duration_seconds": duration_sec,
        "is_drm": info.get("is_drm", False),
    }


# ----------------------------------------------------------------------
# Core adapter functions
# ----------------------------------------------------------------------
def init_adapter(verbose: bool = False, output_dir: Optional[Path] = None):
    setup_logging(verbose=verbose)
    if output_dir:
        os.environ["SUNNXT_OUTPUT_DIR"] = str(output_dir)
    OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
    logger.info("SunNXT adapter initialised. Output dir: %s", OUTPUT_DIR)


def stop_recording():
    stop_event.set()
    logger.info("Stop signal sent to recorder.")


def cleanup_temp_files(output_dir: Optional[Path] = None) -> int:
    """Remove temporary files from a specific directory (or global OUTPUT_DIR if None)."""
    if output_dir is None:
        output_dir = OUTPUT_DIR
    patterns = [
        "seg_*.m4s", "aud_*.m4s",
        "*.m4s", "*.tmp", "*.lst",
        "video_raw.mp4", "audio_raw.mp4",
        "video_merged.mp4", "audio_merged.mp4",
        "init.mp4", "init_dec.mp4",
        "init_audio.mp4", "init_audio_dec.mp4",
        "*_chunk_*.mp4", "repaired_*.mp4",
        "*_joined.mp4",
    ]
    n = 0
    for pat in patterns:
        for f in output_dir.glob(pat):
            try:
                f.unlink()
                n += 1
            except Exception:
                pass
    if n:
        logger.info("Auto-cleanup removed %d temporary file(s) from %s.", n, output_dir)
    return n


def get_mpd_and_info(media_id: str, audio_quality: str = "best", audio_lang: str = "") -> Tuple[str, dict]:
    """Legacy function – now calls get_mpd_info_only."""
    return get_mpd_info_only(media_id, audio_quality, audio_lang)


def get_dvr_window_info(media_id: str) -> Dict[str, Optional[datetime]]:
    try:
        _, info = get_mpd_info_only(media_id)
        earliest, latest = get_dvr_window(info)
        return {
            "earliest": earliest,
            "latest": latest,
            "tsbd_seconds": info.get("tsbd", 0),
        }
    except Exception as e:
        logger.error("DVR window error: %s", e)
        return {"earliest": None, "latest": None, "tsbd_seconds": 0}


def get_segments_in_time_range(media_id: str, start_dt: datetime, end_dt: datetime) -> List[int]:
    _, info = get_mpd_info_only(media_id)
    return get_segment_numbers_in_range(info, start_dt, end_dt)


def record(
    media_id: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    video_quality: str = "best",
    audio_quality: str = "best",
    audio_lang: str = "",
    output_filename: Optional[str] = None,
    cleanup: bool = True,
) -> Path:
    """Single‑task recording (backward compatible)."""
    mpd_url, info = get_mpd_info_only(media_id, audio_quality=audio_quality, audio_lang=audio_lang)

    timed_mode = (start_dt is not None and end_dt is not None)
    if timed_mode:
        if end_dt <= start_dt:
            error_msg = "End time must be after start time"
            progress.set_error(error_msg)
            raise ValueError(error_msg)
        segs_in_range = get_segment_numbers_in_range(info, start_dt, end_dt)
        if not segs_in_range:
            error_msg = "No segments found in the requested time range."
            progress.set_error(error_msg)
            raise RuntimeError(error_msg)
        logger.info("Time range mode: %d target segments", len(segs_in_range))
    else:
        logger.info("Live catchup mode (default depth).")

    if not output_filename:
        if timed_mode:
            start_ist = to_ist(start_dt)
            end_ist = to_ist(end_dt)
            output_filename = f"SunTV_{start_ist.strftime('%Y%m%d_%H%M')}_to_{end_ist.strftime('%H%M')}.mp4"
        else:
            output_filename = f"SunTV_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}.mp4"
    if not output_filename.endswith(".mp4"):
        output_filename += ".mp4"

    logger.info("Recording %s -> %s", media_id, output_filename)
    downloaded, final_info = record_stream(
        mpd_url, start_dt, end_dt, timed_mode,
        video_quality=video_quality,
        audio_quality=audio_quality,
        audio_lang=audio_lang,
        task_ctx=None,   # use global context
    )
    if not downloaded:
        error_msg = "No segments downloaded."
        progress.set_error(error_msg)
        raise RuntimeError(error_msg)

    has_audio = bool(final_info.get("a_media_template"))
    is_drm = final_info.get("is_drm", False)
    enc_scheme = final_info.get("enc_scheme", "")

    if is_drm:
        _, lic_token, content_id = fetch_sunnxt_mpd_info(media_id)
        if not lic_token or not content_id:
            error_msg = "Missing license token or content ID for DRM decryption."
            progress.set_error(error_msg)
            raise RuntimeError(error_msg)

        pssh_b64 = final_info.get("pssh_b64", "")
        a_pssh_b64 = final_info.get("a_pssh_b64", "")
        a_kid = final_info.get("a_kid", "")
        v_kid = (final_info.get("kid") or "").lower().replace("-", "")

        keys = get_keys_cached(
            lic_token, content_id,
            pssh_b64=pssh_b64,
            channel_id=str(content_id),
            kid=v_kid,
            output_dir=OUTPUT_DIR,
        )
        if a_pssh_b64 and a_pssh_b64 != pssh_b64:
            a_keys = get_keys_cached(
                lic_token, content_id,
                pssh_b64=a_pssh_b64,
                channel_id=str(content_id),
                kid=(a_kid or v_kid).lower().replace("-", ""),
                output_dir=OUTPUT_DIR,
            )
            seen = {k.split(":", 1)[0].lower() for k in keys if ":" in k}
            for k in a_keys:
                if ":" in k and k.split(":", 1)[0].lower() not in seen:
                    keys.append(k)

        if not keys:
            error_msg = "No decryption keys obtained and no manual keys provided."
            progress.set_error(error_msg)
            raise RuntimeError(error_msg)

        logger.info("Decrypting with %d key(s)...", len(keys))
        ok = decrypt_segments(keys, downloaded, has_audio, final_info, enc_scheme=enc_scheme)
        if not ok:
            error_msg = "Decryption had errors – check logs."
            progress.set_error(error_msg)
            raise RuntimeError(error_msg)

    logger.info("Muxing %d segments -> %s", len(downloaded), output_filename)
    mux_ok = mux_segments(output_filename, downloaded, has_audio, is_drm, final_info)
    if not mux_ok:
        error_msg = "Muxing failed."
        progress.set_error(error_msg)
        raise RuntimeError(error_msg)

    out_path = OUTPUT_DIR / output_filename
    if not out_path.exists() or out_path.stat().st_size < 65536:
        error_msg = "Output file too small – muxing likely failed."
        progress.set_error(error_msg)
        raise RuntimeError(error_msg)

    if cleanup:
        cleanup_temp_files(OUTPUT_DIR)

    logger.info("Recording completed: %s (%.1f MB)", out_path, out_path.stat().st_size / (1024 * 1024))
    return out_path


def record_with_task(
    task_ctx: "TaskContext",
    media_id: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    video_quality: str = "best",
    audio_quality: str = "best",
    audio_lang: str = "",
    output_filename: Optional[str] = None,
    cleanup: bool = True,
) -> Path:
    """
    Multi‑task recording using a TaskContext (isolated output dir, stop_event, progress).
    The TaskContext must have attributes:
        .id (str)
        .output_dir (Path)
        .stop_event (threading.Event)
        .progress (RecordingProgress)
        .rate_delay (float)
        .rate_lock (threading.Lock)
    """
    mpd_url, info = get_mpd_info_only(media_id, audio_quality=audio_quality, audio_lang=audio_lang)

    timed_mode = (start_dt is not None and end_dt is not None)
    if timed_mode:
        if end_dt <= start_dt:
            task_ctx.progress.set_error("End time must be after start time")
            raise ValueError("End time must be after start time")
        segs_in_range = get_segment_numbers_in_range(info, start_dt, end_dt)
        if not segs_in_range:
            task_ctx.progress.set_error("No segments found in the requested time range.")
            raise RuntimeError("No segments found in the requested time range.")
        logger.info("Time range mode: %d target segments", len(segs_in_range))
    else:
        logger.info("Live catchup mode (default depth).")

    if not output_filename:
        if timed_mode:
            start_ist = to_ist(start_dt)
            end_ist = to_ist(end_dt)
            output_filename = f"SunTV_{start_ist.strftime('%Y%m%d_%H%M')}_to_{end_ist.strftime('%H%M')}.mp4"
        else:
            output_filename = f"SunTV_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}.mp4"
    if not output_filename.endswith(".mp4"):
        output_filename += ".mp4"

    logger.info("Recording %s -> %s (task %s)", media_id, output_filename, task_ctx.id)

    downloaded, final_info = record_stream(
        mpd_url, start_dt, end_dt, timed_mode,
        video_quality=video_quality,
        audio_quality=audio_quality,
        audio_lang=audio_lang,
        task_ctx=task_ctx,
    )
    if not downloaded:
        task_ctx.progress.set_error("No segments downloaded.")
        raise RuntimeError("No segments downloaded.")

    has_audio = bool(final_info.get("a_media_template"))
    is_drm = final_info.get("is_drm", False)
    enc_scheme = final_info.get("enc_scheme", "")

    if is_drm:
        _, lic_token, content_id = fetch_sunnxt_mpd_info(media_id)
        if not lic_token or not content_id:
            task_ctx.progress.set_error("Missing license token or content ID")
            raise RuntimeError("Missing license token or content ID for DRM decryption.")

        pssh_b64 = final_info.get("pssh_b64", "")
        a_pssh_b64 = final_info.get("a_pssh_b64", "")
        a_kid = final_info.get("a_kid", "")
        v_kid = (final_info.get("kid") or "").lower().replace("-", "")

        keys = get_keys_cached(
            lic_token, content_id,
            pssh_b64=pssh_b64,
            channel_id=str(content_id),
            kid=v_kid,
            output_dir=task_ctx.output_dir,
        )
        if a_pssh_b64 and a_pssh_b64 != pssh_b64:
            a_keys = get_keys_cached(
                lic_token, content_id,
                pssh_b64=a_pssh_b64,
                channel_id=str(content_id),
                kid=(a_kid or v_kid).lower().replace("-", ""),
                output_dir=task_ctx.output_dir,
            )
            seen = {k.split(":", 1)[0].lower() for k in keys if ":" in k}
            for k in a_keys:
                if ":" in k and k.split(":", 1)[0].lower() not in seen:
                    keys.append(k)

        if not keys:
            task_ctx.progress.set_error("No decryption keys obtained")
            raise RuntimeError("No decryption keys obtained and no manual keys provided.")

        logger.info("Decrypting with %d key(s)...", len(keys))
        ok = decrypt_segments(
            keys, downloaded, has_audio, final_info,
            enc_scheme=enc_scheme,
            task_ctx=task_ctx,
        )
        if not ok:
            task_ctx.progress.set_error("Decryption had errors")
            raise RuntimeError("Decryption had errors – check logs.")

    logger.info("Muxing %d segments -> %s", len(downloaded), output_filename)
    mux_ok = mux_segments(
        output_filename, downloaded, has_audio, is_drm, final_info,
        task_ctx=task_ctx,
    )
    if not mux_ok:
        task_ctx.progress.set_error("Muxing failed")
        raise RuntimeError("Muxing failed.")

    out_path = task_ctx.output_dir / output_filename
    if not out_path.exists() or out_path.stat().st_size < 65536:
        task_ctx.progress.set_error("Output file too small")
        raise RuntimeError("Output file too small – muxing likely failed.")

    if cleanup:
        cleanup_temp_files(task_ctx.output_dir)

    logger.info("Recording completed: %s (%.1f MB)", out_path, out_path.stat().st_size / (1024 * 1024))
    return out_path


def prune_old_recordings(older_than_days: int = 7):
    cutoff = datetime.now().astimezone() - timedelta(days=older_than_days)
    removed = 0
    for f in OUTPUT_DIR.glob("*.mp4"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime).astimezone() < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("Pruned %d old recordings.", removed)
    return removed


# ----------------------------------------------------------------------
# Progress status for frontend (Telegram bot) – returns global progress
# For multi‑task, the frontend should poll each task's progress separately.
# ----------------------------------------------------------------------
def get_recording_status() -> dict:
    """
    Return current recording progress for frontend polling.
    Note: This returns the global (single‑task) progress. For multi‑task,
    the frontend should store and query each task's own progress object.
    """
    return progress.get_status()


# =========================================================================
# ASYNC WRAPPERS for Telegram bot integration (with semaphore)
# =========================================================================

async def async_list_channels(force_refresh: bool = False) -> List[Dict[str, str]]:
    """Async version of list_channels."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, list_channels, force_refresh)


async def async_get_channel_id(channel_name: str) -> str:
    """Async version of get_channel_id."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, get_channel_id, channel_name)


async def async_get_channel_name(media_id: str) -> str:
    """Async version of get_channel_name."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, get_channel_name, media_id)


async def async_get_mpd_info_only(
    media_id: str,
    audio_quality: str = "best",
    audio_lang: str = "",
    cached_info: Optional[Dict] = None,
) -> Tuple[str, Dict]:
    """Async version of get_mpd_info_only."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            get_mpd_info_only,
            media_id,
            audio_quality,
            audio_lang,
            cached_info,
        )


async def async_get_video_qualities(media_id: str, force_refresh: bool = False) -> List[Dict]:
    """Async version of get_video_qualities."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            get_video_qualities,
            media_id,
            force_refresh,
        )


async def async_get_audio_qualities(media_id: str, force_refresh: bool = False) -> List[Dict]:
    """Async version of get_audio_qualities."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            get_audio_qualities,
            media_id,
            force_refresh,
        )


async def async_get_recording_metadata(
    media_id: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    video_quality: str = "best",
    audio_quality: str = "best",
    audio_lang: str = "",
    info: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Async version of get_recording_metadata."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            get_recording_metadata,
            media_id,
            start_dt,
            end_dt,
            video_quality,
            audio_quality,
            audio_lang,
            info,
        )


async def async_get_dvr_window_info(media_id: str) -> Dict[str, Optional[datetime]]:
    """Async version of get_dvr_window_info."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, get_dvr_window_info, media_id)


async def async_get_segments_in_time_range(media_id: str, start_dt: datetime, end_dt: datetime) -> List[int]:
    """Async version of get_segments_in_time_range."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, get_segments_in_time_range, media_id, start_dt, end_dt)


async def async_record_with_task(
    task_ctx: "TaskContext",
    media_id: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    video_quality: str = "best",
    audio_quality: str = "best",
    audio_lang: str = "",
    output_filename: Optional[str] = None,
    cleanup: bool = True,
) -> Path:
    """Async version of record_with_task."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _executor,
            record_with_task,
            task_ctx,
            media_id,
            start_dt,
            end_dt,
            video_quality,
            audio_quality,
            audio_lang,
            output_filename,
            cleanup,
        )


async def async_stop_recording(task_ctx: "TaskContext") -> None:
    """Signal a specific recording task to stop (async friendly)."""
    task_ctx.stop_event.set()
    await asyncio.sleep(0)


async def async_cleanup_temp_files(output_dir: Optional[Path] = None) -> int:
    """Async version of cleanup_temp_files."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, cleanup_temp_files, output_dir)


async def async_prune_old_recordings(older_than_days: int = 7) -> int:
    """Async version of prune_old_recordings."""
    async with _async_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, prune_old_recordings, older_than_days)


def get_global_progress():
    """Return global progress object for single‑task mode."""
    return progress