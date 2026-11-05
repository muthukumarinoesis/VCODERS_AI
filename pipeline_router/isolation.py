"""Entry points for the inspection child processes (spec 9.1)."""

import resource

from .caps import SafetyCaps
from .probe import Prober, TesseractScriptDetector

_service = None


def limit_child_memory(mb: int) -> None:
    """Address-space limit; Tesseract and pdftoppm inherit it."""
    limit = mb * 2**20
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def inspect_job(data: bytes, max_file_mb: float | None, filename_hint: str | None):
    """Module-level so it can be pickled. Returns FileFacts or raises FileRefused."""
    global _service
    if _service is None:
        from .service import RoutingService

        # shortcut: the child always uses the default SafetyCaps; pass caps in
        # (e.g. via the pool initializer) if the service ever changes them.
        caps = SafetyCaps()
        prober = Prober(caps, image_script_detector=TesseractScriptDetector())
        _service = RoutingService(None, prober, None, caps)
    return _service.inspect(data, max_file_mb, filename_hint)
