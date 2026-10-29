"""PDF compression: Ghostscript when available, PyMuPDF as a pip-only fallback.

Usage: python compress.py INPUT.pdf OUTPUT.pdf light|medium|strong

Runs as its own process so the bot can enforce a timeout by killing it.
Prints "engine=ghostscript" or "engine=pymupdf" on success.

Environment variables:
  GS_PATH         full path to the Ghostscript executable (e.g. gswin64c.exe)
  PDF_ENGINE      auto (default) | ghostscript | pymupdf
"""

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

LEVELS = ("light", "medium", "strong")
TIMEOUT_EXIT_CODE = 124  # the bot treats this as "took too long"

# Ghostscript presets: /printer ~300 dpi, /ebook ~150 dpi, /screen ~72 dpi.
GS_PRESETS = {"light": "/printer", "medium": "/ebook", "strong": "/screen"}

# PyMuPDF: images sharper than dpi_threshold are scaled down to dpi_target and
# re-saved as JPEG at the given quality.
MUPDF_SETTINGS = {
    "light": dict(dpi_threshold=250, dpi_target=200, quality=85),
    "medium": dict(dpi_threshold=170, dpi_target=130, quality=70),
    "strong": dict(dpi_threshold=110, dpi_target=90, quality=50),
}


def find_ghostscript() -> str | None:
    """Locate Ghostscript. On Windows the installer does not add it to PATH,
    so also look in the default install folders."""
    custom = os.getenv("GS_PATH")
    if custom:
        return custom if Path(custom).is_file() else None
    for name in ("gs", "gswin64c", "gswin32c"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        for root in filter(None, roots):
            for exe in ("gswin64c.exe", "gswin32c.exe"):
                matches = sorted(glob.glob(os.path.join(root, "gs", "gs*", "bin", exe)))
                if matches:
                    return matches[-1]  # newest version
    return None


def pymupdf_available() -> bool:
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        return False
    return True


def choose_engine() -> str:
    wanted = os.getenv("PDF_ENGINE", "auto").lower()
    if wanted == "ghostscript":
        if not find_ghostscript():
            raise RuntimeError("PDF_ENGINE=ghostscript but Ghostscript was not found (set GS_PATH)")
        return "ghostscript"
    if wanted == "pymupdf":
        return "pymupdf"
    return "ghostscript" if find_ghostscript() else "pymupdf"


def compress_ghostscript(src: str, dst: str, level: str) -> None:
    gs = find_ghostscript()
    cmd = [
        gs,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={GS_PRESETS[level]}",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-dSAFER",
        f"-sOutputFile={dst}",
        src,
    ]
    # Stop slightly before the bot's own timeout so Ghostscript isn't left running.
    limit = max(30, int(os.getenv("JOB_TIMEOUT_SECONDS", "900")) - 10)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=limit
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(TIMEOUT_EXIT_CODE)
    if result.returncode != 0 or not Path(dst).exists():
        raise RuntimeError(f"Ghostscript failed: {(result.stderr or result.stdout)[-500:]}")


def compress_pymupdf(src: str, dst: str, level: str) -> None:
    import pymupdf

    doc = pymupdf.open(src)
    if doc.needs_pass:
        raise RuntimeError("PDF is password-protected")
    doc.rewrite_images(**MUPDF_SETTINGS[level])
    # Drop unused objects, deduplicate, and compress streams.
    doc.save(dst, garbage=4, deflate=True, clean=True)
    doc.close()


def compress(src: str, dst: str, level: str) -> str:
    engine = choose_engine()
    if engine == "ghostscript":
        compress_ghostscript(src, dst, level)
    else:
        compress_pymupdf(src, dst, level)
    return engine


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[3] not in LEVELS:
        raise SystemExit("usage: compress.py INPUT OUTPUT light|medium|strong")
    try:
        used = compress(sys.argv[1], sys.argv[2], sys.argv[3])
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(f"engine={used}")