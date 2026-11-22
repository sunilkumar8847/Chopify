"""Minimum media for cloud requests: audio pieces and small frames.

Audio is sent as 16 kHz mono FLAC pieces (about 11 MB per 10 minutes, under
Groq's 25 MB free-tier upload limit). Every piece's size is checked: a piece
over the limit is split in half (down to 60 s); a piece that still does not
fit is refused, never uploaded. Frames are downscaled JPEGs. Temporary
pieces live next to the source audio (on the project's drive) and are deleted
after use.
"""
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ai.base import PermanentError

PIECE_SECONDS = 600.0
# Groq free plan: 25 MB per file (developer plan 100 MB). Decimal megabytes
# with a margin, so "25 MB" read either way is never exceeded.
MAX_PIECE_BYTES = 24_000_000
MIN_PIECE_SECONDS = 60.0


class PieceTooLarge(PermanentError):
    """A piece of audio cannot be made small enough for the upload limit."""


FRAME_WIDTH = 512


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


@contextmanager
def audio_pieces(path, piece_seconds=None, duration=None, max_bytes=None):
    """Yield [(offset_seconds, flac_path, seconds)] covering the whole file.

    Every piece is at most ``max_bytes`` (default MAX_PIECE_BYTES) on disk.
    """
    piece_seconds = piece_seconds or PIECE_SECONDS
    max_bytes = max_bytes or MAX_PIECE_BYTES
    path = Path(path)
    total = float(duration) if duration else probe_duration(path)
    folder = Path(tempfile.mkdtemp(prefix=".ai-pieces-", dir=str(path.parent)))
    try:
        pieces = []
        start, index = 0.0, 0
        while start < total - 0.05:
            length = min(piece_seconds, total - start)
            out = folder / f"piece{index:03d}.flac"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}",
                            "-t", f"{length:.3f}", "-i", str(path), "-vn", "-ac", "1",
                            "-ar", "16000", "-c:a", "flac", str(out)], check=True)
            size = out.stat().st_size
            if size > max_bytes:
                out.unlink()
                if length <= MIN_PIECE_SECONDS:
                    raise PieceTooLarge(
                        f"A {length:.0f}s piece of audio is {size / 1e6:.1f} MB, over the "
                        f"{max_bytes / 1e6:.0f} MB upload limit. Nothing was uploaded.")
                # unusually dense audio: halve the piece and try again
                piece_seconds = max(MIN_PIECE_SECONDS, length / 2)
                continue
            pieces.append((start, out, length))
            start += length
            index += 1
        yield pieces
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def jpeg_frame(image_path, width=FRAME_WIDTH):
    """Downscaled JPEG bytes of one image/frame (no full-resolution uploads)."""
    with tempfile.TemporaryDirectory(dir=str(Path(image_path).parent)) as temp:
        out = Path(temp) / "frame.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(image_path), "-frames:v", "1",
                        "-vf", f"scale='min({width},iw)':-2", "-q:v", "5", str(out)], check=True)
        return out.read_bytes()
