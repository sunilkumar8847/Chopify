"""Minimum media for cloud requests: audio pieces and small frames.

Audio is sent as 16 kHz mono FLAC pieces (about 11 MB per 10 minutes, under
Groq's 25 MB free-tier upload limit). Frames are downscaled JPEGs. Temporary
pieces live next to the source audio (on the project's drive) and are deleted
after use.
"""
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

PIECE_SECONDS = 600.0
MAX_PIECE_BYTES = 24 * 1024 * 1024
FRAME_WIDTH = 512


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


@contextmanager
def audio_pieces(path, piece_seconds=None, duration=None):
    """Yield [(offset_seconds, flac_path, seconds)] covering the whole file."""
    piece_seconds = piece_seconds or PIECE_SECONDS
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
            if out.stat().st_size > MAX_PIECE_BYTES and length > 60:
                # unusually dense audio: halve the piece and try again
                out.unlink()
                piece_seconds = length / 2
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
