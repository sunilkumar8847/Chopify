"""One place that builds yt-dlp commands.

YouTube extraction needs a JavaScript runtime (yt-dlp's "EJS"); yt-dlp only
enables Deno by default. When Deno is missing but Node.js is installed,
``--js-runtimes node`` is added so every format is available.
"""
import functools
import shutil
import sys


@functools.lru_cache(maxsize=1)
def runtime_args():
    if shutil.which("deno"):
        return []
    if shutil.which("node"):
        return ["--js-runtimes", "node"]
    return []


def base():
    """``[python, -m, yt_dlp, ...runtime flags]`` - append the rest."""
    return [sys.executable, "-m", "yt_dlp", *runtime_args()]
