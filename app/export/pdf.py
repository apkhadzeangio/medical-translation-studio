"""PDF export: the DOCX converted by LibreOffice, so both formats always show the same content."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class PdfUnavailable(RuntimeError):
    """LibreOffice is not installed in this environment."""


def docx_to_pdf(docx: bytes, *, timeout: float = 600) -> bytes:
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if binary is None:
        raise PdfUnavailable("PDF export needs LibreOffice (soffice) on the server")
    with tempfile.TemporaryDirectory(prefix="mts-pdf-") as tmp:
        work = Path(tmp)
        (work / "book.docx").write_bytes(docx)
        # a private profile directory lets several conversions run at once
        result = subprocess.run(
            [binary, "--headless", f"-env:UserInstallation=file://{work}/profile", "--convert-to", "pdf",
             "--outdir", str(work), str(work / "book.docx")],
            capture_output=True, timeout=timeout, check=False)
        output = work / "book.pdf"
        if result.returncode != 0 or not output.exists():
            raise RuntimeError(f"LibreOffice failed: {result.stderr.decode(errors='replace')[:500]}")
        return output.read_bytes()
