import hashlib
import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from uuid import uuid4


MIME_SUFFIXES = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
}


class LocalContentAddressedStorage:
    """Immutable SHA-256-addressed byte storage rooted in one configured directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write_bytes(self, content_sha256: str, content: bytes, mime_type: str) -> str:
        actual = hashlib.sha256(content).hexdigest()
        if actual != content_sha256:
            raise ValueError("content bytes do not match the supplied SHA-256 identity")
        suffix = MIME_SUFFIXES.get(mime_type, ".bin")
        target = self.root / content_sha256[:2] / content_sha256[2:4] / (
            content_sha256 + suffix
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != content_sha256:
                raise ValueError("content-addressed storage collision or corruption")
            return target.as_uri()

        temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        except FileExistsError:
            if not target.exists():
                raise
        finally:
            if temporary.exists():
                temporary.unlink()
        if hashlib.sha256(target.read_bytes()).hexdigest() != content_sha256:
            raise ValueError("persisted object failed SHA-256 verification")
        return target.as_uri()

    def write_file(
        self,
        content_sha256: str,
        source: str | Path,
        mime_type: str,
        *,
        byte_size: int,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> str:
        """Adopt a verified staged file without materializing it as one bytes object."""
        source_path = Path(source).resolve()
        actual, actual_size = self._file_identity(source_path, chunk_size)
        if actual != content_sha256 or actual_size != byte_size:
            raise ValueError("staged file does not match its supplied content identity")
        suffix = MIME_SUFFIXES.get(mime_type, ".bin")
        target = self.root / content_sha256[:2] / content_sha256[2:4] / (
            content_sha256 + suffix
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_hash, existing_size = self._file_identity(target, chunk_size)
            if existing_hash != content_sha256 or existing_size != byte_size:
                raise ValueError("content-addressed storage collision or corruption")
            return target.as_uri()
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with source_path.open("rb") as input_stream, temporary.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=chunk_size)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            temporary.replace(target)
        except FileExistsError:
            if not target.exists():
                raise
        finally:
            if temporary.exists():
                temporary.unlink()
        persisted_hash, persisted_size = self._file_identity(target, chunk_size)
        if persisted_hash != content_sha256 or persisted_size != byte_size:
            raise ValueError("persisted staged object failed content verification")
        return target.as_uri()

    @staticmethod
    def _file_identity(path: Path, chunk_size: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def read_bytes(self, storage_uri: str) -> bytes:
        parsed = urlparse(storage_uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise ValueError("storage URI does not belong to the local driver")
        path = Path(url2pathname(unquote(parsed.path))).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("storage URI escapes the configured intelligence root") from exc
        return path.read_bytes()
