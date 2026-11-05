"""All safety limits in one place (spec 5.1)."""

from dataclasses import dataclass

MB = 1024 * 1024


@dataclass(frozen=True)
class SafetyCaps:
    max_file_mb: float = 50
    max_pages: int = 400
    max_unpacked_mb: float = 200  # total uncompressed size of a ZIP container
    max_unpacked_ratio: float = 100  # uncompressed / compressed
    max_zip_entries: int = 10_000
    max_entry_mb: float = 100  # real uncompressed size of any single ZIP entry
    max_image_megapixels: float = 50  # files AND images embedded in PDFs
    max_text_kb: int = 500  # pasted text
    probe_timeout_s: float = 10  # whole inspection, hard kill
    probe_memory_mb: int = 1024  # address-space limit of each inspection child
    reject_macros: bool = True
    reject_pdf_js: bool = True
