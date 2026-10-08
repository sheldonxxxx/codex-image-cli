"""Stage output privately and publish atomically without overwriting any path."""

import os
from pathlib import Path
import tempfile

from .errors import CliError


class OutputTarget:
    def __init__(self, path: Path):
        if path.suffix.lower() != ".png":
            raise CliError("--output must end in .png.")
        try:
            self.path = path.expanduser().absolute()
            # lexists also rejects dangling symlinks before any image request is sent.
            if os.path.lexists(self.path):
                raise CliError("Output already exists. Choose a new versioned filename.")
            if not self.path.parent.is_dir():
                raise CliError("Output directory does not exist. Create it before generating.")
            fd, name = tempfile.mkstemp(prefix=".codex-image-", suffix=".png", dir=self.path.parent)
            self.temp_path = Path(name)
            self.stream = os.fdopen(fd, "wb")
            self.keep = False
        except OSError:
            raise CliError("Cannot prepare output in the selected directory; check permissions.",
                           kind="output", exit_code=6) from None

    def __enter__(self):
        return self

    def save(self, data: bytes) -> Path:
        try:
            self.stream.write(data)
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            # link is an atomic no-clobber operation, unlike replace/rename on POSIX.
            os.link(self.temp_path, self.path)
        except OSError:
            self.keep = True
            raise CliError(
                f"The request completed but output could not be published. Recovery file: "
                f"{self.temp_path}. It may be partial if writing failed; do not regenerate "
                "before checking it. The destination was not overwritten.",
                kind="output", exit_code=6,
            ) from None
        return self.path

    def __exit__(self, exc_type, exc, traceback):
        try:
            try:
                self.stream.close()
            except OSError:
                # A failed flush may also fail again on close. Preserve the original
                # curated recovery error rather than hiding it with a raw exception.
                self.keep = True
                if exc_type is None:
                    raise CliError(f"Could not close the staged output. Recovery file: "
                                   f"{self.temp_path}. It may be partial.",
                                   kind="output", exit_code=6) from None
        finally:
            if not self.keep:
                try:
                    self.temp_path.unlink(missing_ok=True)
                except OSError:
                    pass  # Successful final output is already present; no second request.
