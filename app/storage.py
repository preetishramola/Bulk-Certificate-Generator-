import os
from pathlib import Path


class CertificateStorage:
    """Stores generated PDFs on local disk under `<root>/<job_id>/<certificate_id>.pdf`.

    The database stores the path *relative* to the root, so the root can be
    moved (or this class swapped for an object-store implementation) without
    rewriting rows.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, job_id: str, certificate_id: str, content: bytes) -> str:
        relative = Path(job_id) / f"{certificate_id}.pdf"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename, so a crash never leaves a half-written PDF.
        tmp = target.with_suffix(".pdf.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        return relative.as_posix()

    def resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("path escapes storage root")
        return path
