# Pipeline router: implementation spec (v4.1)

Build a Python package, `pipeline_router`, that decides **which quiz pipeline handles each request**. It decides from the input mode, the file type, the language, the subject, and whether the file is scanned. It runs inside a **standalone AI quiz service** that QuickTest, Quizzier, QuickAns and future projects call over an API.

The package itself is framework-independent: no FastAPI, Celery or Django imports. The service (FastAPI) and its worker (Celery) call it.

---

## 0. Decisions this version assumes

v1 to v4 were each reviewed; this version fixes every point raised in all four reviews (see the change log at the end). Three decisions weren't confirmed yet, so this spec assumes the following. **Change these first if any are wrong:**

| # | Assumption | Consequence if it changes |
|---|---|---|
| A | **Standalone service.** QuickTest is a client like any other project and never shares code or a database with the service | If it's built into QuickTest instead, section 9 changes; the package itself stays the same |
| B | **All common document types are accepted:** PDF, DOCX, DOC, PPTX, PPT, XLSX, XLS, ODT, ODP, ODS, RTF, TXT, MD, CSV, HTML and images | Remove kinds from `ACCEPTED_KINDS` (section 5.1) |
| C | **No plan size limits in the service.** Only safety caps: 50 MB, 400 pages. Plans and credits stay in each client | Projects can still set a stricter `max_file_mb` (section 5.8) |

---

## 1. Goal

Rajkumar's rule: **a Tamil file goes to the Tamil pipeline, and a separate subject can have a separate pipeline.**

To keep the number of pipelines manageable:

- **The input mode is a hard split.** File, pasted text and topic requests only ever match pipelines for their own mode.
- **Within a mode, each pipeline declares what it handles** in a `match` block, and the **most specific match wins**. For example: Tamil biology, then Tamil, then the default.
- **A pipeline must be able to read the file's type.** If the best match can't, the next most specific one that can is used.
- **Ambiguous configs are rejected when the service starts**, never decided by chance at runtime.
- **No AI call is needed to route.** An LLM is only an optional fallback for detecting the subject.

---

## 2. Architecture and flow

```
CLIENT (QuickTest, Quizzier, QuickAns…)                 AI QUIZ SERVICE (standalone)
─────────────────────────────────────────               ────────────────────────────────────────────
user fills the form (questions, difficulty,
types, time limit, optional subject)
client checks its own plan and credit balance
        │                                         PROXY / MIDDLEWARE: bodies over 50 MB refused
        │                                         before the app receives them
        ├── POST /v1/route  (file | text | topic) ───▶  ① identify project (API key), check its rate limits
        │                                               ② INSPECT, in an isolated child process with a
        │                                                  hard kill and a memory limit: safety caps (type
        │                                                  from bytes, zip bombs, encryption, damage, pages,
        │                                                  PDF JavaScript), then probe sampled pages
        │                                               ③ subject: form value, else detect (optional)
        │                                               ④ route: choose the pipeline
        │                                               ⑤ keep the upload and its facts for 30 min
        ◀── 200 {upload_id, decision, estimate} ────────┘
show "Tamil · Biology" with a way to correct it
charge credits (re-check the balance at this moment)
        │
        ├── POST /v1/jobs {upload_id, quiz options,  ─▶  ⑥ same upload and key again? return that job
        │       optional corrections}                   ⑦ corrections re-route from the stored facts
        │       optional Idempotency-Key header            (nothing is inspected again; source "user")
        │                                               ⑧ create the job row, queue the worker
        ◀── 202 {job_id, pipeline} ─────────────────────┘
        │                                               WORKER: claim the job (skip it if it's already
        │                                               running or done), load the pipeline config,
        │                                               apply the quiz options as the job layer, run it.
        │                                               Never re-routes. Save the quiz on the server.
        ├── GET /v1/jobs/{job_id}  (or signed webhook) ─▶  status, then the saved quiz
on failure: refund credits
user takes the quiz (time limit is the client's)
```

- **Two halves:** step ② is `RoutingService.inspect(...)`, which is heavy and runs isolated. Steps ③ and ④ are `RoutingService.route(...)`, which is cheap and runs in the API process. `decide_file(...)`, `decide_text(...)` and `decide_topic(...)` combine both for tests and simple callers.
- **Speed:** digital files take milliseconds. Scans have a 6-second OCR budget (section 5.4). The whole inspection is killed after 10 seconds, and the request gets `too_complex`.
- **Credits:** never touched by the service. The client prices from `decision` and `estimate`, then charges.

---

## 3. Decision rules (summary)

| Question | First choice | Fallback | If still unsure |
|---|---|---|---|
| Input mode | The endpoint field: `file`, `text` or `topic` | — | — |
| File type | Bytes only. The file name may only choose between text subtypes (`txt`, `md`, `csv`), never the family | — | Refuse with `unsupported_type` |
| Is it safe? | Size, zip-bomb, encryption, damage, page and JavaScript checks **before** any content is read | — | Refuse with a specific code |
| Scanned? | Under 50% of sampled pages have real text | — | — |
| Language (text) | Letters counted per Unicode script | Arabic-script markers for Urdu and Persian; kana for Japanese | `None`: a language-free pipeline is used |
| Language (old Tamil fonts) | At least 30% of the sampled text is in a legacy Tamil font | — | — |
| Language (legacy text encodings) | — | — | `None`, flagged as a suspected legacy encoding |
| Language (scans) | Tesseract script detection on at most 2 page images, within the time budget | Render the page with `pdftoppm` if the image can't be opened | `None`, recorded as `"unreadable scan"` or `"timed out"` |
| Subject | Form value, validated against the project's list | Classifier on about 2,000 sampled characters | Confidence under 0.7 or mixed content: `None`, so the language pipeline is used |
| User correction | Values sent to `POST /v1/jobs` override detection (source `"user"`). The stored facts are re-routed; nothing is inspected again | — | — |
| Pipeline | Same input mode, most specific `match`, allowed for the project, has a reader for the type | Higher `priority` breaks ties; then fall back to less specific matches | Default pipeline for that mode, else refuse |

---

## 4. Package layout

```
pipeline_router/
  __init__.py        # re-exports the public API
  caps.py            # SafetyCaps (all limits in one place)
  filetype.py        # sniff(), FileRefused, ACCEPTED_KINDS, zip/OLE/ODF inspection
  textdecode.py      # decode_text(): BOMs, UTF-8, charset-normalizer, legacy-encoding suspicion
  scripts.py         # count_scripts(), dominant_language(), LanguageGuess
  probe.py           # FileFacts, Prober (incl. PDF safety checks), TesseractScriptDetector
  subject.py         # SubjectResolver, KeywordClassifier, LLMClassifier, SubjectGuess
  router.py          # PipelineSpec, RouteInput, load_pipelines(), validate(), PipelineRouter
  projects.py        # ProjectConfig, ProjectLimits, load_projects()
  isolation.py       # inspect_job() entry point and limit_child_memory() for the child processes
  service.py         # RoutingService (inspect, route, decide_*), Decision, Estimate
examples/
  pipelines/*.yaml   # starter set (section 6)
  projects.yaml
  fastapi_app.py     # service endpoints (section 9.3)
  body_limit.py      # ASGI body-size middleware (section 9.2)
  ratelimit.py       # per-project limits in Redis (section 9.5)
  webhooks.py        # signing and verifying (section 9.4)
  worker.py          # Celery task (section 9.6)
tests/               # pytest (section 10)
```

**Dependencies:**

- **Package:** `pypdf` with `cryptography` (install as `pypdf[crypto]`; without it, AES-encrypted PDFs, including the owner-password-only ones that are accepted, can't be opened), `python-docx`, `python-pptx`, `openpyxl`, `olefile`, `charset-normalizer`, `PyYAML`, `Pillow`, `pytesseract` and `pebble` (process pool with per-task timeouts that kill the child).
- **Service:** `fastapi`, `celery` and `redis` (broker and rate-limit counters).
- **System:** `tesseract-ocr` with the `osd` data file, and `poppler-utils` for `pdftoppm` (optional, used for unreadable scan images).
- **Worker image only:** LibreOffice (headless) to convert DOC, PPT, XLS, ODP and ODS for full reading.
- **Optional:** an LLM, injected as a plain function.

---

## 5. Modules

### 5.1 `caps.py` and `filetype.py`: safe, real file type from bytes

```python
@dataclass(frozen=True)
class SafetyCaps:
    max_file_mb: float = 50
    max_pages: int = 400
    max_unpacked_mb: float = 200        # total uncompressed size of a ZIP container
    max_unpacked_ratio: float = 100     # uncompressed ÷ compressed
    max_zip_entries: int = 10_000
    max_entry_mb: float = 100           # real uncompressed size of any single ZIP entry
    max_image_megapixels: float = 50    # files AND images embedded in PDFs
    max_text_kb: int = 500              # pasted text
    probe_timeout_s: float = 10         # whole inspection, hard kill
    probe_memory_mb: int = 1024         # address-space limit of each inspection child
    reject_macros: bool = True
    reject_pdf_js: bool = True

class FileRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(code, message)
        self.code = code        # stable, returned to the client
        self.message = message  # user-facing, says what to do

    def __reduce__(self):       # must survive pickling back from the inspection child
        return (FileRefused, (self.code, self.message))

ACCEPTED_KINDS = ("pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "odp", "ods",
                  "rtf", "txt", "md", "csv", "html", "image")

def sniff(data: bytes, caps: SafetyCaps, filename_hint: str | None = None) -> str
```

`sniff` checks container structure **without reading content**:

| Bytes | Result |
|---|---|
| Empty | `empty_file` |
| Over `max_file_mb` (checked first, by length) | `too_large` |
| `%PDF-` | `pdf`. Encryption, damage, pages and JavaScript are checked in the probe, which opens the PDF once |
| `FF D8 FF`, `89 50 4E 47 0D 0A 1A 0A`, `RIFF….WEBP`, `II*\0` / `MM\0*` (TIFF) | `image`. Open the header with Pillow (lazy). Over `max_image_megapixels`: `image_too_large`. Unreadable: `damaged_file` |
| `PK\x03\x04` (ZIP) | **First the zip-bomb check** (below), then by contents: `word/document.xml` → `docx`; `ppt/presentation.xml` → `pptx`; `xl/workbook.xml` → `xlsx`; a `mimetype` entry of `application/vnd.oasis.opendocument.text`, `.presentation` or `.spreadsheet` → `odt`, `odp`, `ods`, but `encrypted` if `META-INF/manifest.xml` (read through the capped `read_entry()`) contains `manifest:encryption-data`; anything else → `unsupported_type`; bad ZIP → `damaged_file` |
| `D0 CF 11 E0 A1 B1 1A E1` (OLE, via `olefile`) | `EncryptedPackage` stream → `encrypted` (a password-protected DOCX, XLSX or PPTX). `WordDocument` → `doc`, but `encrypted` if the `fEncrypted` flag is set (bit `0x0100` of the 16-bit flags at offset `0x0A` of that stream). `Workbook` or `Book` → `xls`, but `encrypted` if a `FILEPASS` record (type `0x002F`) follows the first `BOF` record. `PowerPoint Document` → `ppt`, but `encrypted` if an `EncryptedSummary` stream exists. Anything else → `unsupported_type` |
| `{\rtf` | `rtf` |
| Decodes as text (section 5.2) with under 1% control characters | `html` if, after removing a BOM and leading whitespace, it starts with `<!doctype html` or `<html`. Otherwise `csv` only if the **column rule** passes: `csv.Sniffer().sniff(sample, delimiters=",;\t\|")` finds a delimiter, there are at least 5 non-empty lines, and at least 90% of them split into the same number of columns, which is 2 or more. Plain prose with commas fails this rule. A `.csv` hint alone isn't enough; the column rule must pass too. Then `md` if the hint says `.md`; else `txt` |
| Anything else | `unsupported_type` |

**Zip-bomb check** (all ZIP kinds, before any entry is read):

- Refuse if any of these is true:
  - There are more than `max_zip_entries` entries.
  - The sum of `file_size` across entries is over `max_unpacked_mb`.
  - That sum ÷ `len(data)` is over `max_unpacked_ratio`.
  - Any single entry has a ratio over 1,000.
- Code: `zip_bomb`. Message: "This file expands to far more than its size, so it can't be processed safely."

**Then verify, don't trust.** The sizes above are what the file declares about itself, and a hostile ZIP can lie in its directory. So before any library (python-docx, python-pptx, openpyxl) opens the container:

- Stream-decompress every entry once with `z.open(name)` and `read(64 * 1024)` in a loop, discarding the data and counting real bytes.
- Refuse with `zip_bomb` as soon as an entry passes its declared size or `max_entry_mb`, or the running total passes `max_unpacked_mb`.
- Entries the probe reads itself (such as `content.xml`) go through the same capped helper, `read_entry(z, name, max_bytes)`.
- This all runs inside the inspection child (section 9.3), whose memory limit is the last line of defence.

**Macros** (when `reject_macros`):

- ZIP: any `*/vbaProject.bin` entry.
- ODF: any entry under `Basic/`.
- OLE: a `Macros` or `_VBA_PROJECT_CUR` storage.
- Code: `macros`. Message: "Files with macros aren't accepted. Save it without macros (for example as .docx or PDF)."

### 5.2 `textdecode.py`: text files in any common encoding

```python
@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    legacy_encoding_suspected: bool

def decode_text(data: bytes) -> DecodedText | None   # None means it's binary
```

1. **BOM present:** decode accordingly (UTF-8, UTF-16 LE/BE, UTF-32 LE/BE) and strip it.
2. **Otherwise strict UTF-8.** A multi-byte character cut off at the end of the 8 KB sniff sample doesn't count as binary.
3. **Otherwise `charset_normalizer.from_bytes(data).best()`.** No result means binary, so return `None`.
4. **Legacy encoding suspicion:** if the chosen encoding is a single-byte code page (such as `cp1252` or `latin-1`) and at least 25% of letters are in U+0080–U+00FF, set `legacy_encoding_suspected=True`. TSCII Tamil text decodes like this. The probe then reports `language=None`, `language_source="suspected legacy encoding"`, so a language-free pipeline is used. Section 12 covers adding a TSCII decoder.

### 5.3 `scripts.py`: language from characters

- Count only Unicode categories `L*` and `M*` (Indic vowel signs are marks). Ignore digits, spaces and punctuation.
- Script table:

  | Code | Range | Language | Approximate? |
  |---|---|---|---|
  | `Taml` | U+0B80–0BFF | `ta` | no |
  | `Deva` | U+0900–097F | `hi` | **yes** (also Marathi, Nepali, Sanskrit) |
  | `Beng` | U+0980–09FF | `bn` | yes (also Assamese) |
  | `Guru` | U+0A00–0A7F | `pa` | no |
  | `Gujr` | U+0A80–0AFF | `gu` | no |
  | `Orya` | U+0B00–0B7F | `or` | no |
  | `Telu` | U+0C00–0C7F | `te` | no |
  | `Knda` | U+0C80–0CFF | `kn` | no |
  | `Mlym` | U+0D00–0D7F | `ml` | no |
  | `Sinh` | U+0D80–0DFF | `si` | no |
  | `Arab` | U+0600–06FF, U+0750–077F | `ar`, `ur` or `fa` (rule below) | yes |
  | `Hebr` | U+0590–05FF | `he` | no |
  | `Grek` | U+0370–03FF | `el` | no |
  | `Cyrl` | U+0400–04FF | `ru` | **yes** (also Ukrainian, Bulgarian, Serbian…) |
  | `Thai` | U+0E00–0E7F | `th` | no |
  | `Hang` | U+AC00–D7AF, U+1100–11FF | `ko` | no |
  | `Kana` | U+3040–30FF | `ja` | no |
  | `Hani` | U+4E00–9FFF | `zh`, or `ja` if kana is present | yes |
  | `Latn` | letters below U+0250 | `en` | **yes** (all Latin-script languages) |

- **Arabic-script rule:**
  - Any Urdu-only letter (ٹ U+0679, ڈ U+0688, ڑ U+0691, ں U+06BA, ے U+06D2) present: `ur`.
  - Else any of پ چ ژ گ ی (U+067E, U+0686, U+0698, U+06AF, U+06CC) present: `fa`.
  - Else `ar`.
- **CJK rule:** `Hani` and `Kana` are counted together as one CJK share. If kana is at least 5% of the CJK letters, the language is `ja`, otherwise `zh`.
- **`dominant_language(text, min_share=0.30) -> LanguageGuess(language, script, confidence, approximate, letters, shares)`:**
  - Fewer than 50 letters: `language=None`.
  - If the largest non-Latin script has at least 30% of letters, it wins. Tamil textbooks are full of English terms, so a simple majority would get them wrong.
  - Otherwise the most common script wins.
  - Scripts not in the table are not counted. Their text gets `None` and the language-free pipeline. This is intended; say so in the docstring.

### 5.4 `probe.py`: cheap facts, plus PDF safety

```python
@dataclass(frozen=True)
class FileFacts:
    kind: str | None                   # None for topic requests
    pages: int                         # pages, slides, or sheets×rows/50; estimated for text
    has_text_layer: bool
    text_page_share: float
    language: str | None
    script: str | None
    language_confidence: float
    language_approximate: bool
    language_source: str               # "text" | "request" | "legacy font" | "ocr script detection" | "suspected legacy encoding"
                                       # | "unreadable scan" | "timed out" | "none"
    legacy_font_share: float = 0.0     # share of sampled text in legacy Tamil fonts
    sample_text: str = ""              # about 2,000 chars from spread pages, legacy-font text excluded
    probe_ms: int = 0

    @property
    def scanned(self) -> bool:
        return not self.has_text_layer

    def to_dict(self) -> dict: ...                   # JSON types only: stored with the upload
    @classmethod
    def from_dict(cls, d: dict) -> "FileFacts": ...  # used to re-route corrections without re-inspecting
```

```python
class Prober:
    def __init__(self, caps: SafetyCaps, image_script_detector=None, sample_pages: int = 4,
                 min_chars_per_page: int = 40, max_scan_images: int = 2,
                 scan_time_budget_s: float = 6.0): ...
    def probe(self, data: bytes, kind: str) -> FileFacts: ...
    def text_facts(self, text: str) -> FileFacts: ...      # pasted text (TEXT mode)
    def topic_facts(self, topic: str, language: str | None = None) -> FileFacts: ...  # TOPIC mode
```

**Page sampling:**

- `sample_indices(n, k)` returns up to `k` page numbers spread evenly through the document, skipping page 0 (the cover) when `n > 2`.
- Never read the whole document.

**PDF:** open once with `PdfReader(io.BytesIO(data), strict=False)`, then do the safety checks in this order, **before extracting any text**:

1. **Damaged:** an exception on open or on reading the page count raises `damaged_file`, with the message "This PDF is damaged and can't be read. Try saving or exporting it again." **Exception:** pypdf's `DependencyError` (a missing crypto library) is a server fault, never `damaged_file`. Let it propagate as a 500 and log it. A startup self-test opens a small AES-encrypted sample PDF so a missing `cryptography` package is caught at deploy time.
2. **Encrypted:** if `reader.is_encrypted`, try `reader.decrypt("")`.
   - If that fails, raise `encrypted`, with the message "This PDF is password-protected. Remove the password and upload it again."
   - If it succeeds (only an owner password: print or copy restrictions), continue.
3. **Pages:** more than `caps.max_pages` raises `too_many_pages`.
4. **JavaScript** (when `reject_pdf_js`), raising `pdf_javascript`:
   - The catalog `/Names` has `/JavaScript`.
   - The catalog `/OpenAction` is a JavaScript action (`/S /JavaScript` or a `/JS` key).
   - The catalog or a sampled page has `/AA`.
   - Or, as a fallback, the raw bytes contain `/JavaScript`.

Then the content:

- **Text and fonts together:** use `page.extract_text(visitor_text=...)` on the sampled pages to count characters per font (`font_dict["/BaseFont"]`, subset prefix `ABCDEF+` removed).
  - A page "has text" if it has at least `min_chars_per_page` non-whitespace characters.
- **Legacy Tamil fonts:**
  - `legacy_font_share` is the characters in fonts matching the legacy pattern ÷ all characters.
  - At **0.30 or more**: `language="ta"`, `script="Taml"`, `language_source="legacy font"`, `confidence = min(0.95, 0.5 + share / 2)`.
  - Below 0.30: keep the share for the record, **exclude that text** (it's gibberish), and detect the language from the rest.
  - Pattern (case-insensitive, one constant, extend it from real uploads): `bamini|baamini|vanavil|tscii|tsc_|elango|kalaham|shree-?tam|\btam-|\btab-`.
- **Text layer:** if at least 50% of sampled pages have text, take the language from `dominant_language()` on the joined non-legacy text.
- **Otherwise the file is scanned** (scan handling below).

**Scans:** PDFs without a text layer, and images. Within `scan_time_budget_s`, measured from the start of the probe. This budget covers OCR and `pdftoppm`, which have their own timeouts. Python code such as pypdf parsing can't be interrupted from inside, so the hard kill after `probe_timeout_s` (section 9.3) is what bounds the whole inspection:

1. Take embedded images from the sampled pages, at most `max_scan_images`. For image files, use the file itself, or up to 2 TIFF frames.
   - **Check size before decoding.** Walk the page's `/Resources/XObject` entries with `/Subtype /Image` and read `/Width` × `/Height` from the dictionary. Skip any image over `max_image_megapixels` without decoding it (record it; don't refuse the file). Decode only the remaining images, by name (`page.images[name]`).
   - **Pillow guard:** set `Image.MAX_IMAGE_PIXELS` to the same cap and turn `DecompressionBombWarning` into an error. Treat `DecompressionBombError` like an unreadable image.
2. If Pillow can't open an image (JBIG2 and CCITT are common in scans), render that page with `pdftoppm -r 150 -f N -l N -png` using a subprocess timeout equal to the remaining budget. If `pdftoppm` is missing or fails, skip that image.
3. Run the detector on each image with the remaining budget as its timeout. Keep the highest-confidence script.
4. Record why nothing was found:
   - No image could be read: `language_source="unreadable scan"`.
   - The budget ran out: `"timed out"`.
   - The detector was unsure: `"none"`.

**Other types** (all on sampled parts only):

| Kind | How the probe gets text and pages |
|---|---|
| `docx` | `python-docx`. Characters per run font (falling back to the paragraph style, then `Normal`), so the same legacy-font share rule applies. `pages` is estimated as words ÷ 400 |
| `pptx` | `python-pptx`. Text frames of up to 4 sampled slides; `pages` = slide count |
| `xlsx` | `openpyxl` with `read_only=True`. The first 200 non-empty cells of up to 2 sheets. `pages` = `ws.max_row` ÷ 50 when openpyxl reports it; read-only mode often reports `None` or a wrong value when the file has no dimension record. In that case use the rows seen while sampling ÷ 50. At least 1 |
| `odt`, `odp`, `ods` | Read `content.xml` through `read_entry()` (capped) and strip the tags. `pages` is estimated as characters ÷ 3,000 |
| `rtf` | Strip control words and groups (`\word`, `{\*…}`), decode `\'hh` and `\uN` escapes |
| `doc`, `xls`, `ppt` | Quick string scan of the main stream: UTF-16LE runs of 4+ letters, plus cp1252 runs. Good enough for counting scripts. Full reading happens in the worker after LibreOffice conversion |
| `txt`, `md`, `csv`, `html` | `decode_text()`. For HTML, drop `<script>` and `<style>` blocks and all tags. `pages` is estimated as characters ÷ 3,000 |
| `image` | Treated as a scan |

- **`sample_text`:** whitespace collapsed, at most 2,000 characters, taken as 4 slices from the start, middle and end, joined with `" … "`.
- **`probe_ms`:** the total probe time.

**`TesseractScriptDetector(min_confidence=0.4)`:**

- `detect(image: bytes, timeout_s: float) -> tuple[str, float] | None`.
- Uses `pytesseract.image_to_osd(img, output_type=Output.DICT, timeout=timeout_s)`.
- Maps the `script` name to a code: `Latin`, `Tamil`, `Devanagari`, `Telugu`, `Kannada`, `Malayalam`, `Bengali`, `Oriya`, `Sinhala`, `Arabic`, `Gujarati`, `Gurmukhi`, `Cyrillic`, `Greek`, `Hebrew`, `Thai`, `Hangul`, `Han`, `Japanese`.
- Confidence is `min(1, script_conf / 5)`. Below `min_confidence`, return `None`.
- **Never raises:** return `None` on any error or timeout.
- Before running it, downscale images to at most 2,000 px on the longest side.

### 5.5 `subject.py`: the form first, detection only as a fallback

```python
@dataclass(frozen=True)
class SubjectGuess:
    subject: str | None
    confidence: float
    source: str              # "form" | "user" | "classifier" | "below threshold" | "none"
    detected: str | None = None
```

`SubjectResolver(classifier=None, min_confidence=0.7)` with `resolve(form_subject, sample_text, allowed_subjects, source="form")`:

1. **Form value present:**
   - Lowercase and trim it.
   - If it isn't in `allowed_subjects`, raise `UnknownSubject`. The service returns 400 `unknown_subject`.
   - Otherwise return it with confidence 1.0 and the given `source` (`"form"`, or `"user"` for a correction).
2. **No classifier, or empty sample text:** `(None, 0.0, "none")`.
3. **Classify:**
   - Result not in the list: `(None, conf, "none")`.
   - Confidence below `min_confidence`: `(None, conf, "below threshold", detected=<guess>)`.
   - Otherwise: `(subject, conf, "classifier")`.

Two classifiers, both implementing `classify(text, subjects) -> (subject | None, confidence)`:

- **`KeywordClassifier`** (offline, free):
  - Latin words match whole words, case-insensitive, with an optional plural `s`. Other scripts match as plain substrings.
  - Below 3 hits in total: `(None, 0)`.
  - Otherwise confidence is the top subject's hits ÷ all hits.
  - Ship starter lists, including subject names in Tamil (இயற்பியல், வேதியியல், உயிரியல், தாவரவியல், விலங்கியல், கணிதம்) and Hindi (भौतिकी, रसायन, जीव विज्ञान, गणित). Grow them from real textbooks.
- **`LLMClassifier(complete: Callable[[str], str])`**:
  - `complete` is the existing Gemini, Groq or DeepSeek call, injected.
  - Prompt: pick one subject from the list, `"mixed"` or `"unknown"`, and reply with JSON only, like `{"subject": "biology", "confidence": 0.9}`.
  - Send at most 2,500 characters.
  - Parse the first `{...}` in the reply, which tolerates code fences.
  - Any failure, `mixed`, `unknown` or an unlisted subject returns `(None, 0)`.
  - Clamp the confidence to the range 0 to 1.

### 5.6 `router.py`: input split, match blocks, readers, validation

```python
MATCH_KEYS = ("input", "language", "subject", "scanned")
INPUTS = ("file", "text", "topic")

@dataclass(frozen=True)
class PipelineSpec:
    name: str
    input: str                                    # from match["input"], default "file"
    match: dict = field(default_factory=dict)     # WITHOUT the input key
    priority: int = 0
    readers: frozenset = field(default_factory=frozenset)  # kinds this pipeline can read (file input only)
    config: dict = field(default_factory=dict, compare=False)
    source: str = ""

    @property
    def specificity(self) -> int:
        return len(self.match)

    def matches(self, facts: dict) -> bool:
        return all(facts.get(k) == v for k, v in self.match.items())

@dataclass(frozen=True)
class RouteInput:
    input: str
    kind: str | None          # file kind, for the reader check; None for text and topic
    language: str | None
    subject: str | None
    scanned: bool = False

    def as_facts(self) -> dict:   # matching works on this dict, never on the dataclass
        return {"language": self.language, "subject": self.subject, "scanned": self.scanned}
```

**`load_pipelines(directory)`:**

- Reads every `*.yaml` / `*.yml` file.
- The top-level routing fields are `name` (defaults to the file name), `match` (defaults to `{}`) and `priority` (defaults to 0).
- `input` is taken out of `match` (default `"file"`).
- `readers` is the set of keys under the config's `readers:` mapping.
- **Everything except the routing fields is the pipeline config**, so the builder's exported YAML can be pasted below them unchanged.

**`validate(specs) -> list[str]`:** raises `ConfigError` listing *all* problems, and returns warnings that don't stop startup.

- Errors:
  - Duplicate names.
  - Unknown match keys.
  - `input` not in `INPUTS`.
  - `scanned` not a boolean.
  - **A file pipeline with no `readers`.**
- **Ambiguity, checked only within the same input mode:** two pipelines whose matches don't conflict on any shared key, with the same specificity and the same priority, are an error.
  - Example: `{language: ta, subject: biology}` and `{language: ta, scanned: true}` both fit a scanned Tamil biology file.
  - The message names both pipelines and the combined case, and suggests a pipeline for that exact case or a higher priority.
  - Identical matches with the same priority are also an error.
- Warnings:
  - An input mode with no default pipeline (empty `match`).
  - Any kind in `ACCEPTED_KINDS` that no default file pipeline can read.
  - A `text` or `topic` pipeline that matches on `scanned`. It's always false for those modes, so `scanned: true` never matches and `scanned: false` adds nothing.

**`PipelineRouter(specs)`:**

- Validates the specs on construction.
- `from_directory(path)` loads and validates a directory.
- `get(name)` returns one spec.
- `choose(ri, allowed: frozenset | None = None) -> RouteResult`:
  1. Keep the specs with `spec.input == ri.input` that match `ri.as_facts()`.
  2. For each one, in order of `(specificity, priority)` from highest, skip it if it's not in `allowed` (reason `"not allowed for this project"`) or, for file input, if `ri.kind` isn't in `spec.readers` (reason `"no reader for <kind>"`).
  3. The first one not skipped is chosen.
  4. If none remain:
     - Raise `NoPipeline("no_reader")` if every fitting pipeline was skipped for a missing reader.
     - Raise `NoPipeline("not_allowed")` if every one was skipped for not being allowed, or for a mix of both reasons.
     - Raise `NoPipeline("no_match")` if nothing fitted at all.

```python
@dataclass(frozen=True)
class RouteResult:
    spec: PipelineSpec
    fitted: list[str]                 # every pipeline that matched, best first
    skipped: list[dict]               # [{"pipeline": "tamil_biology", "why": "no reader for pptx"}]
```

### 5.7 Text and topic modes

- **`text` pipelines** start at chunking, with no reading. Their language and subject come from the pasted text and the form, exactly like files, so `{input: text, language: ta}` is possible.
- **`topic` pipelines** use the question bank first, then generate. The language comes from the request when given; otherwise it's detected from the topic text if it's 50+ characters, else `None`.
- Because input is a hard split, **pasted Tamil biology text can never reach `tamil_biology`**, which is a file pipeline. This fixes the most serious bug in v1.

### 5.8 `projects.py`: which client may use what

```yaml
# projects.yaml (API keys are NOT stored here: they're hashed in the service database)
quicktest:
  allowed: "*"                                   # or a list of pipeline names
  subjects: [physics, chemistry, biology, mathematics]
  max_file_mb: 20                                # optional, must be ≤ the safety cap
  webhook: https://quicktest.example/hooks/quiz  # optional; its signing secret is stored encrypted in the database
  limits:                                        # optional; defaults in section 9.5
    route_per_minute: 60
    jobs_per_minute: 20
    jobs_at_once: 10
    jobs_per_day: 2000
    pages_per_day: 50000
quickans:
  allowed: [text_default, topic_bank, default]
```

`ProjectConfig(key, allowed: frozenset | None, subjects: tuple, max_file_mb: float | None, webhook: str | None, limits: ProjectLimits)`.

`load_projects(path, router)` validates at startup:

- Every allowed name must exist.
- `max_file_mb` must be no more than `caps.max_file_mb`.
- Subjects must be lowercase identifiers.

### 5.9 `service.py`: the calls the API makes

```python
@dataclass(frozen=True)
class Estimate:
    pages: int
    scanned: bool
    reading: str             # "text" | "ocr" | "vision" (scans and legacy encodings need more than text)
    question_count: int | None

@dataclass(frozen=True)
class Decision:
    project: str
    pipeline: str
    input: str
    kind: str | None
    pages: int
    scanned: bool
    language: str | None
    language_source: str
    language_confidence: float
    language_approximate: bool
    legacy_font_share: float
    subject: str | None
    subject_source: str
    subject_confidence: float
    subject_detected: str | None
    fitted: list[str]
    skipped: list[dict]
    reason: str
    probe_ms: int

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

class RoutingService:
    def __init__(self, router, prober, subjects, caps): ...

    # Heavy: runs in the inspection child (section 9.3). Arguments and result are plain, picklable values.
    def inspect(self, data: bytes, max_file_mb: float | None = None,
                filename_hint: str | None = None) -> FileFacts

    # Cheap: runs in the API process.
    def route(self, facts: FileFacts, input: str, project: ProjectConfig,
              form_subject: str | None = None, force_language: str | None = None,
              force_subject: str | None = None) -> Decision

    # Conveniences for tests and simple callers: inspect + route in one call, without isolation.
    def decide_file(self, data, project, form_subject=None, filename_hint=None,
                    force_language=None, force_subject=None) -> Decision
    def decide_text(self, text, project, form_subject=None, force_language=None, force_subject=None) -> Decision
    def decide_topic(self, topic, project, form_subject=None, language=None) -> Decision
```

**`inspect` order:**

1. Size limit (`max_file_mb`, else `caps.max_file_mb`). Over it: `too_large`, with a message stating both sizes.
2. `sniff()`, including the zip-bomb verification.
3. `probe()`, including the PDF safety checks.

**`route` order:**

1. Subject, checked against `project.subjects`.
2. `choose(..., allowed=project.allowed)`.

**Pasted text:** `prober.text_facts(text)` is cheap and runs in the API process. Text over `max_text_kb` is refused with `too_large`.

**Topics:** `prober.topic_facts(topic, language)` returns `FileFacts` with `kind=None`, `pages=0` and `has_text_layer=True`, and with `sample_text` set to the topic.

- If `language` is given, it's used with `language_source="request"`.
- Otherwise it's detected from the topic when the topic has 50+ characters (`language_source="text"`).
- Otherwise it's `None`.

So every mode goes through the same `route(facts, input, …)`, and `decide_topic` is just `topic_facts` followed by `route`.

**Corrections:** call `route()` again with the stored `FileFacts` and `force_language` / `force_subject`. Nothing is inspected again. The forced values' source is recorded as `"user"`.

**`reason` wording:**

- Default pipeline: `"<name> is the default <input> pipeline; nothing more specific fitted"`.
- Otherwise: `"<name> is the most specific <input> pipeline for language ta, subject biology"`.
- If anything was skipped, append it: `"; skipped tamil_biology (no reader for pptx)"`.

---

## 6. Pipeline config format

One YAML file per pipeline. The routing fields go on top; the builder's exported settings go below them unchanged.

```yaml
# pipelines/tamil_biology.yaml
name: tamil_biology
match: {language: ta, subject: biology}       # input defaults to file
priority: 0
# ---- pipeline config (paste from the builder) ----
readers:
  pdf: {impl: pymupdf}
  docx: {impl: python_docx}
  image: {impl: cached_ocr}
roles:
  ocr: {impl: paddleocr, langs: [ta, en]}
quiz:
  types: [mcq_single, assertion_reason]
```

Starter set (must load with **no errors and no warnings**):

| File | `match` | Readers | Purpose |
|---|---|---|---|
| `default.yaml` | `{}` (file) | All kinds in `ACCEPTED_KINDS` | Any file nothing more specific fits |
| `tamil_default.yaml` | `{language: ta}` | pdf, docx, doc, pptx, odt, rtf, txt, md, html, image | Any Tamil file |
| `tamil_biology.yaml` | `{language: ta, subject: biology}` | pdf, docx, image | Tamil biology documents |
| `hindi_default.yaml` | `{language: hi}` | pdf, docx, image, txt | Any Hindi file |
| `text_default.yaml` | `{input: text}` | — | Pasted text: starts at chunking |
| `tamil_text.yaml` | `{input: text, language: ta}` | — | Pasted Tamil text |
| `topic_bank.yaml` | `{input: topic}` | — | Question bank first, then generate |

No priorities are needed: the input split removes the ties v1 needed them for.

Readers in the worker (for reference): PDF via PyMuPDF, DOCX via python-docx, PPTX via python-pptx, XLSX via openpyxl, ODT/ODP/ODS via odfpy, and RTF, TXT, MD, CSV and HTML parsed directly. DOC, PPT and XLS are converted with LibreOffice first.

---

## 7. Decision record

Store `decision.to_dict()` in the service's `jobs` table (JSON column) and return it from `/v1/route`. Example:

```json
{
  "project": "quicktest",
  "pipeline": "tamil_default",
  "input": "file",
  "kind": "pptx",
  "pages": 32,
  "scanned": false,
  "language": "ta",
  "language_source": "text",
  "language_confidence": 0.61,
  "language_approximate": false,
  "legacy_font_share": 0.0,
  "subject": "biology",
  "subject_source": "form",
  "subject_confidence": 1.0,
  "subject_detected": "biology",
  "fitted": ["tamil_biology", "tamil_default", "default"],
  "skipped": [{"pipeline": "tamil_biology", "why": "no reader for pptx"}],
  "reason": "tamil_default is the most specific file pipeline for language ta; skipped tamil_biology (no reader for pptx)",
  "probe_ms": 84
}
```

---

## 8. Errors and HTTP responses

Response body for every refusal: `{"error": "<code>", "message": "<user-facing message>"}`. Messages say what to do next.

| Code | HTTP | When |
|---|---|---|
| `unknown_project` | 401 | Missing or wrong API key |
| `empty_file` | 400 | Empty upload |
| `too_large` | 413 | Over the project's limit or the 50 MB safety cap (bodies over the cap are stopped before the app reads them), or pasted text over 500 KB |
| `rate_limited` | 429 | Over one of the project's limits (section 9.5). Includes `Retry-After` |
| `idempotency_mismatch` | 422 | The same `Idempotency-Key` was sent again with different quiz options or corrections |
| `busy` | 503 | Every inspection slot stayed busy for 2 seconds, or queueing a job failed. Includes `Retry-After: 5` |
| `too_complex` | 422 | Inspection was stopped at the 10-second or memory limit. Message: "This file is too complex to check quickly. Try a smaller file or export it again as PDF." |
| `unsupported_type` | 415 | Not one of the accepted kinds, judged by bytes |
| `damaged_file` | 400 | Broken PDF, ZIP or image |
| `encrypted` | 400 | Password-protected PDF or Office file |
| `macros` | 400 | Office or ODF file with macros |
| `zip_bomb` | 400 | ZIP container over the size, ratio or entry limits |
| `too_many_pages` | 400 | Over 400 pages |
| `pdf_javascript` | 400 | PDF contains JavaScript |
| `image_too_large` | 400 | Over 50 megapixels |
| `unknown_subject` | 400 | Subject not in the project's list |
| `no_reader` | 422 | No fitting pipeline can read this type |
| `not_allowed` | 403 | Fitting pipelines exist, but none is allowed for this project |
| `no_match` | 422 | Nothing fits (only possible without a default pipeline) |
| `upload_expired` | 410 | `upload_id` older than 30 minutes |
| `ConfigError` | — | Startup only: the service refuses to start with an invalid or ambiguous config |

---

## 9. Service, clients and worker

### 9.1 Startup (once per process)

```python
caps = SafetyCaps()
router = PipelineRouter.from_directory(settings.PIPELINES_DIR)        # fails fast on ConfigError
projects = load_projects(settings.PROJECTS_FILE, router)
routing = RoutingService(
    router,
    prober=Prober(caps, image_script_detector=TesseractScriptDetector()),
    subjects=SubjectResolver(classifier=KeywordClassifier()),          # or LLMClassifier(gemini_complete)
    caps=caps,
)
inspect_pool = pebble.ProcessPool(
    max_workers=settings.PROBE_CONCURRENCY,        # e.g. 4 per API process
    max_tasks=200,                                 # recycle each child after 200 files
    initializer=limit_child_memory, initargs=(caps.probe_memory_mb,),
)
inspect_slots = asyncio.Semaphore(settings.PROBE_CONCURRENCY)
limiter = RateLimiter(redis, projects)                                 # section 9.5
```

`isolation.py`:

- **`limit_child_memory(mb)`** calls `resource.setrlimit(resource.RLIMIT_AS, (mb * 2**20, mb * 2**20))` in each child.
  - The limit covers address space, not just memory in use. Tesseract and `pdftoppm` inherit it, and some libraries reserve large address ranges up front. **Test OCR on a real scan under the limit**, and raise `probe_memory_mb` if Tesseract fails. Under a too-tight limit the detector returns `None`, so routing quietly loses scan language detection.
- **Memory sizing:** the container needs room for `uvicorn workers × PROBE_CONCURRENCY × probe_memory_mb`, plus the API processes themselves. For example, 2 workers × 4 children × 1 GB is 8 GB at worst. Reduce `PROBE_CONCURRENCY` or the number of uvicorn workers if the container is smaller. The worst case only happens if every slot holds a hostile file at once.
- **`inspect_job(data, max_file_mb, filename_hint)`** is a module-level function, so it can be pickled. On first use in each child, it builds its own `Prober` and calls `RoutingService.inspect`. It returns `FileFacts` or raises `FileRefused`.

### 9.2 Request body limit (before FastAPI sees the body)

Starlette receives and stores the whole multipart body before the endpoint runs, so `await file.read(limit)` can't protect the server. Use two layers:

1. **Proxy:** set `client_max_body_size 51m;` in nginx, or the equivalent on the DigitalOcean load balancer or App Platform. That's the 50 MB cap plus 1 MB for multipart overhead. Test it in staging.
2. **`BodySizeLimit` ASGI middleware**, added as the outermost middleware:
   - Its limit is the **cap plus 1 MB**, the same as the proxy, so a 50 MB file with its multipart wrapping still gets through. The exact file size is checked afterwards in `inspect()`.
   - If `Content-Length` is over the limit, return 413 `too_large` before reading anything.
   - Otherwise wrap `receive`, count the bytes of each `http.request` message, and send 413 as soon as the running total passes the limit. This covers chunked uploads that have no `Content-Length`.

Per-project limits (such as 20 MB) are then checked in `inspect()` on the bytes that were received. *Confirm in your FastAPI and Starlette versions that multipart is stored before the endpoint runs; the middleware is needed either way.*

### 9.3 Endpoints

**`POST /v1/route`** (multipart; exactly one of `file`, `text` or `topic`; optional `subject`, `language`):

1. Find the project by the hashed API key. Not found: 401 `unknown_project`.
2. `limiter.check(project, "route")`. Over the limit: 429 `rate_limited`.
3. **Files:**
   - `data = await file.read()`. This is already bounded by 9.2.
   - Wait at most 2 seconds for a slot: `await asyncio.wait_for(inspect_slots.acquire(), 2)`. On timeout, return 503 `busy` with `Retry-After: 5`.
   - `future = inspect_pool.schedule(inspect_job, args=(data, project.max_file_mb, file.filename), timeout=caps.probe_timeout_s)`, then `facts = await asyncio.wrap_future(future)`. Release the slot in `finally`.
   - Errors:
     - `FileRefused`: section 8.
     - `TimeoutError`: pebble has already killed the child. Return 422 `too_complex`.
     - `ProcessExpired`, `MemoryError` or any other child failure: 422 `too_complex`, and log it.
4. **Text:**
   - `facts = routing.prober.text_facts(text)`, in-process.
   - Over `max_text_kb`: 413 `too_large`.
5. **Topic:** `facts = routing.prober.topic_facts(topic, language)`, in-process.
6. `decision = routing.route(facts, input, project, subject)`.
7. Store the upload bytes under `uploads/{upload_id}` in object storage, and save `facts.to_dict()`, the decision and `expires_at = now() + 30 minutes` in an `uploads` table.
   - Bucket lifecycle rules work in days, not minutes, so the **sweeper** (section 9.6) deletes expired uploads every minute.
   - A 1-day lifecycle rule on the `uploads/` prefix is the backstop if the sweeper stops.
   - Once a job is created, its upload is kept with the job instead; the retention period is in section 12.
8. Return `200 {upload_id, decision, estimate}`. Map `UnknownSubject` and `NoPipeline` to section 8.

**`POST /v1/jobs`** (JSON: `upload_id`, `quiz: {count, difficulty, types}`, optional `language` and `subject` corrections, optional `client_ref`; optional `Idempotency-Key` header):

1. Project, then `limiter.check(project, "job")`, which includes the jobs-at-once limit. Over a limit: 429 `rate_limited`.
2. Load the upload. Gone: 410 `upload_expired`.
3. **Idempotency:**
   - The key is the `Idempotency-Key` header, or the `upload_id` if there's no header.
   - The `jobs` table has a unique constraint on `(project, upload_id, idempotency_key)`.
   - The row also stores `request_hash`, a SHA-256 of the canonical JSON of the request body (quiz options and corrections).
   - Insert inside a transaction. If the constraint is hit, read the existing job:
     - **Same `request_hash`:** return it with 200, the same body as the first 202. No second job is ever created.
     - **Different `request_hash`:** return 422 `idempotency_mismatch`. The same key was reused with different options, which is a client bug that should show up rather than silently return the old job.
   - To make two quizzes from one upload, send two different keys.
4. **Corrections:** `routing.route(FileFacts.from_dict(stored_facts), input, project, subject, force_language=…, force_subject=…)`. Nothing is inspected again.
5. **Validate the quiz options** against the pipeline:
   - `count` from 1 to 500.
   - Types from the supported list.
   - Difficulty: one level is converted to a mix (`easy` → `{easy: 0.6, medium: 0.3, hard: 0.1}`, `medium` → `{easy: 0.2, medium: 0.6, hard: 0.2}`, `hard` → `{easy: 0.1, medium: 0.3, hard: 0.6}`), or a full mix that adds up to 1 is accepted.
6. Create the `jobs` row with status `queued` and `enqueued_at = now()`. Queue `generate_quiz.delay(job_id)` **after the row is committed**. If the API process dies between the two steps, the sweeper re-queues the job (section 9.6). If queueing fails, delete the row and return 503 `busy`, so the client can retry with the same key.
7. Return `202 {job_id, pipeline, status: "queued"}`.

**`GET /v1/jobs/{job_id}`:**

- Returns the status (`queued`, `running`, `succeeded` or `failed`).
- On success it includes the saved quiz.
- On failure it includes `{error, message}`.
- Results are kept for N days (section 12).

### 9.4 Webhooks (signed)

- **Secret:** each project has its own signing secret. It's shown once at setup and stored encrypted, since HMAC needs the raw secret. Rotation allows two active secrets for 24 hours.
- **When a job finishes,** POST the same body as `GET /v1/jobs/{job_id}`, with these headers:
  - `X-Quiz-Event-Id: <uuid>`
  - `X-Quiz-Timestamp: <unix seconds>`
  - `X-Quiz-Signature: v1=<hex HMAC-SHA256(secret, timestamp + "." + raw_body)>`
- **The client verifies** with a constant-time comparison, rejects timestamps older than 5 minutes, and ignores event IDs it has already seen. This is the same rule as your payment webhooks.
- **Delivery:** retry after 1 minute, 5 minutes, 30 minutes, 2 hours and 12 hours, then stop. `GET /v1/jobs/{job_id}` stays the source of truth.
- **Every attempt is signed again with a fresh `X-Quiz-Timestamp`**, so retries hours later still pass the client's 5-minute check. The `X-Quiz-Event-Id` and the body stay exactly the same on every attempt, so the client can ignore duplicates.

### 9.5 Rate limits and quotas (per project)

The service doesn't handle credits, so these limits are what stop a runaway client or a leaked API key from running up model costs. Counters live in Redis. Each project can override the defaults under `limits:` (section 5.8).

| Limit | Default |
|---|---|
| `/v1/route` requests per minute | 60 |
| `/v1/jobs` created per minute | 20 |
| Jobs queued or running at once | 10 |
| Jobs per day | 2,000 |
| Pages inspected per day | 50,000 |

- Over any limit: 429 `rate_limited` with `Retry-After`.
- Daily counters reset at midnight UTC.
- A **global ceiling across all projects** (jobs per day) caps the total model bill even if several keys leak at once.

### 9.6 Worker (Celery)

```python
@app.task(bind=True, acks_late=True, soft_time_limit=900, time_limit=960)
def generate_quiz(self, job_id: str): ...
```

- **Claim the job atomically before doing anything:**

  ```sql
  UPDATE jobs SET status = 'running', started_at = now(), heartbeat_at = now(), attempts = attempts + 1
  WHERE id = :id
    AND attempts < :max_attempts                                   -- 3
    AND (status = 'queued' OR (status = 'running' AND heartbeat_at < now() - interval '5 minutes'))
  ```

  If no row was updated, return immediately. The job is running elsewhere, already finished, or out of attempts, so a redelivered message never runs a job twice.
- **Heartbeat from a background thread.** Start a daemon thread right after the claim. It updates `heartbeat_at` every 60 seconds until the task finishes, and the main code stops it in `finally`.
  - A single slow step (a long Gemini call or a LibreOffice conversion) doesn't block it, so a job that's still working is never reclaimed.
  - If the worker process dies or is killed by the 16-minute hard time limit, the thread dies with it. The heartbeat then goes stale and the job can be reclaimed.
  - The thread updates only `heartbeat_at`, and only `WHERE id = :id AND status = 'running'`, so it can't revive a finished job.
  - **The thread opens its own database connection** and closes it when it stops. Database connections can't be shared between threads (Django's connections are per thread, and a psycopg connection mustn't be used by two threads at once), so it never touches the task's connection.
- **Attempt limit:** `max_attempts = 3`. A file that crashes the worker every time (for example by running out of memory) is reclaimed at most twice, then the sweeper marks it `failed` with `error = "worker_crashed"`.
- **Broker visibility timeout must be longer than the longest job** (the QUIC-55 lesson): `broker_transport_options = {"visibility_timeout": 3600}`, against a 16-minute hard time limit.
- **Run the job:**
  - Load `router.get(job.pipeline).config`.
  - Apply the quiz options as the **job layer** (`quiz.count`, `quiz.difficulty`, `quiz.types`) using the pipeline's override policy.
  - **Never re-route.** If a "digital" file turns out to be mostly scanned, the pipeline's reading ladder handles it.
  - Convert DOC, PPT, XLS, ODP and ODS with LibreOffice (headless, with a timeout) before reading.
- **Finish:**
  - Save the quiz on the server, mark the job `succeeded`, then send the webhook.
  - On error, mark it `failed` with a code and message, and log the decision with it.
- **Task arguments are only `job_id`.** Everything else is read from the job row, so future changes never break tasks already in the queue.

**Sweeper** (Celery beat, every minute). Every step is safe to repeat, because the claim is atomic:

1. **Lost tasks, with backoff.** A job can sit in `queued` for a long time simply because every worker is busy (a normal backlog), not because its message was lost. Re-queueing it every minute would flood the queue exactly when it's already slow. So:
   - Each job has `requeue_count` (starting at 0).
   - Re-queue only when `now() - enqueued_at` is over `2 minutes × 2^requeue_count`: that's 2, then 4, then 8 minutes.
   - Then set `enqueued_at = now()`, increment `requeue_count`, and queue `generate_quiz` again.
   - **At most 3 re-queues per job.** After that, leave the job `queued` (its message may still arrive) and raise an alert. A job queued that long means a broker problem or a backlog someone should look at.
   - Even in a long backlog, each job gets at most 3 extra messages, and the atomic claim makes the extras harmless.
2. **Dead workers:** for jobs `running` with a heartbeat over 5 minutes old:
   - `attempts < 3`: re-queue the job (the claim then takes it over).
   - Otherwise: mark it `failed` with `worker_crashed`, and send the webhook so the client refunds.
3. **Expired uploads:** delete uploads past `expires_at` that have no job, from both storage and the table.

### 9.7 What each client does (QuickTest first)

1. Check its own plan and credit balance (unchanged).
2. Call `POST /v1/route`. Show the detected language and subject, with a way to correct them.
3. Price from `decision` and `estimate`: pages, scanned or not, reading method, question count. **Charge credits, re-checking the balance at the moment of charging.**
4. Call `POST /v1/jobs` **with an `Idempotency-Key`** (for example the client's own quiz request ID). Retry with the same key on timeouts. If it finally fails, refund.
5. Poll `GET /v1/jobs/{id}`, or receive the webhook and **verify its signature**. If the job fails, refund.
6. Save the quiz in its own database with its own approval rules (QUIC-121). The **time limit stays in the client**; it controls taking the quiz, not generating it.

**QuickTest migration:**

- Put the new path behind a feature flag. The current in-process Celery path keeps working until the flag is on for all users.
- If QuickTest's existing Celery task signature changes at any point, accept both the old and new arguments for one release, so tasks queued during a deploy don't fail.

---

## 10. Tests (pytest)

Build fixtures inside the tests:

- PDFs: with `reportlab`, plus `pypdf` for encryption and JavaScript.
- Office and ODF files: with `python-docx`, `python-pptx`, `openpyxl` and `zipfile`.
- OLE files: from small checked-in samples.
- **Zip bombs:** use small caps in the test, for example a 1 MB unpack cap with 5 MB of zeros, so the tests stay fast.

| Area | Case | Expected |
|---|---|---|
| import | `import pipeline_router` | No error (covers mutable defaults in dataclasses) |
| filetype | PDF, PNG, JPEG, WEBP, TIFF headers | `pdf`, `image` ×4 |
| filetype | DOCX, PPTX, XLSX built in the test | `docx`, `pptx`, `xlsx` |
| filetype | ODT, ODP, ODS (zip with `mimetype` first) | `odt`, `odp`, `ods` |
| filetype | `{\rtf1…` | `rtf` |
| filetype | OLE samples: Word, Excel, PowerPoint | `doc`, `xls`, `ppt` |
| filetype | OLE with `EncryptedPackage` stream | `encrypted` |
| filetype | DOCX zip with `word/vbaProject.bin`; ODT with `Basic/` entry | `macros` ×2 |
| filetype | ZIP expanding beyond the test cap; ratio over 100; 10,001 entries | `zip_bomb` ×3, and **python-docx is never called** (assert with a mock) |
| filetype | Empty; random binary; damaged ZIP | `empty_file`; `unsupported_type`; `damaged_file` |
| filetype | 60-megapixel PNG header | `image_too_large` |
| filetype | `.xlsx` bytes uploaded as `notes.pdf` | `xlsx` (the name is ignored) |
| text | UTF-8; UTF-16 with BOM; cp1252 with accents | Text, decoded correctly |
| text | UTF-8 BOM followed by `<!doctype html>` | `html` |
| text | Tamil UTF-8 cut mid-character at 8 KB | Text, not binary |
| text | CSV with a consistent delimiter; `notes.md` hint on plain text | `csv`; `md` |
| text | Bytes mostly in 0x80–0xFF decoded as cp1252 (TSCII-like) | `legacy_encoding_suspected`, `language=None` |
| scripts | Tamil; Tamil with English at about 35% Tamil; English with about 10% Tamil | `ta`; `ta`; `en` |
| scripts | Hindi; Odia; Sinhala | `hi` (approximate); `or`; `si` |
| scripts | Urdu with ے; Persian with گ and no Urdu letters; Arabic | `ur`; `fa`; `ar` |
| scripts | Russian; Japanese with kana; Chinese; Korean | `ru` (approximate); `ja`; `zh`; `ko` |
| scripts | Under 50 letters; Ethiopic text | `None`; `None` |
| PDF safety | User-password PDF | `encrypted` |
| PDF safety | Owner-password-only PDF | Accepted |
| PDF safety | Truncated or garbage PDF | `damaged_file` (never an unhandled exception) |
| PDF safety | 401 pages | `too_many_pages` |
| PDF safety | `writer.add_js(...)`; OpenAction JavaScript | `pdf_javascript` ×2 |
| probe | Text PDF with 6 pages | `has_text_layer=True`, `en`, cover not sampled, at most 4 pages read |
| probe | English PDF with a single heading in a font named `Bamini` | `en`, `legacy_font_share` > 0 and < 0.3 |
| probe | PDF mostly in a font named `Bamini` | `ta`, `language_source="legacy font"` |
| probe | Scanned PDF with a fake detector returning `("Taml", 0.9)` | `ta`, `"ocr script detection"`, detector called at most 2 times |
| probe | Scanned PDF whose images Pillow can't open, and `pdftoppm` unavailable | `language=None`, `"unreadable scan"` |
| probe | Fake detector that sleeps longer than the budget | Returns within budget + 0.5 s, `"timed out"` |
| probe | PPTX with Tamil slides; XLSX with English cells; ODT; RTF with `\uN` escapes | Correct language each time |
| probe | Long document `sample_text` | At most about 2,000 chars, includes text from the end |
| subject | Form `"Biology"`; form `"history"` | `("biology", "form")`; `UnknownSubject` |
| subject | Biology-heavy text; evenly mixed text | `"classifier"`; `None` with `"below threshold"` and `detected` set |
| subject | LLM reply in ```json fences; `{"subject": "mixed"}`; invalid JSON | Parsed; `(None, 0)`; `(None, 0)` |
| router | Starter set (section 6) | Loads with no errors and no warnings |
| router | **Pasted Tamil text, subject biology** | `tamil_text` (never `tamil_biology`) |
| router | **Tamil topic with subject biology** | `topic_bank` |
| router | Tamil biology PDF | `tamil_biology`, fitted `[tamil_biology, tamil_default, default]` |
| router | Tamil biology PPTX | `tamil_default`, skipped `tamil_biology` (no reader for pptx) |
| router | Tamil physics PDF; Hindi PDF; unknown language | `tamil_default`; `hindi_default`; `default` |
| router | Project allowed only `[default]`, Tamil PDF | `default`, skipped `tamil_default` (not allowed) |
| router | Project allowed only `[tamil_biology]`, English PDF | `NoPipeline("not_allowed")` → 403 |
| router | `{language: ta, subject: biology}` + `{language: ta, scanned: true}`, same priority | `ConfigError` naming both |
| router | Same pair, one with `priority: 1` | Loads; the higher priority wins for a scanned Tamil biology file |
| router | Same matches but different input modes | Loads (no ambiguity across modes) |
| router | Duplicate names; key `lang`; `scanned: "yes"`; file pipeline without readers | One `ConfigError` listing all four |
| service | English PDF, no subject | `pipeline="default"`, `to_dict()` is JSON-serialisable |
| service | 25 MB file, project cap 20 MB | `too_large`, message has both sizes |
| service | `force_language="ta"` on an English-detected file | `language="ta"`, `language_source="user"` |
| filetype | ZIP whose directory declares 1 KB for an entry that really unpacks to 5 MB (test cap 1 MB; patch the declared sizes in both headers) | `zip_bomb`, and python-docx is never called |
| filetype | Prose with commas and full stops, no hint; the same prose with a `.csv` hint | `txt` ×2 |
| filetype | Real CSV with 3 columns over 10 lines | `csv` |
| filetype | `.doc` with the `fEncrypted` bit; `.xls` with a `FILEPASS` record; `.ppt` with `EncryptedSummary` (checked-in samples) | `encrypted` ×3 |
| filetype | `FileRefused("encrypted", "…")` through `pickle.dumps` / `loads` | Same code and message |
| probe | Scanned PDF with an image XObject declaring 20,000 × 20,000 | Skipped without decoding (assert the decoder isn't called); `"unreadable scan"` |
| probe | XLSX in read-only mode with no dimension record | `pages` ≥ 1, from the rows seen |
| router | Text pipeline matching `scanned: true` | Loads with a warning |
| service | Correction with `force_subject` on stored facts | New decision, and the prober is never called (mock) |
| api | `Content-Length` of 60 MB | 413 from the middleware before any body is read |
| api | Chunked upload without `Content-Length`, over the cap | 413 once the cap is passed; at most cap + one chunk received |
| api | Inspection that sleeps 30 seconds (fake `inspect_job`) | 422 `too_complex` after about 10 seconds; the child process no longer exists |
| api | Inspection that allocates past the memory limit | 422 `too_complex`; the API process is unaffected |
| api | All slots held, a new upload arrives | 503 `busy` within about 2 seconds, with `Retry-After` |
| api | 10 concurrent scan uploads | At most `PROBE_CONCURRENCY` inspections at once; the event loop stays responsive |
| api | `POST /v1/jobs` twice with the same upload and no key | Same `job_id`, one row, one task queued |
| api | Two concurrent `POST /v1/jobs` with the same `Idempotency-Key` | One job (unique constraint), both responses carry the same `job_id` |
| api | Two different `Idempotency-Key` values for one upload | Two jobs |
| api | 61st `/v1/route` call within a minute | 429 `rate_limited` with `Retry-After` |
| api | 11th job queued while 10 are running | 429 `rate_limited` |
| webhook | Valid signature; body changed by one byte; timestamp 6 minutes old; repeated event ID | Accepted; rejected; rejected; ignored |
| worker | Job with a pipeline name | Config loaded, quiz options applied as the job layer, quiz saved, no re-route |
| worker | The same job delivered twice (simulated redelivery) | The second delivery exits without work |
| worker | Job stuck in `running` with a heartbeat 6 minutes old | Reclaimed and run |
| worker | A step that blocks for 7 minutes (fake slow Gemini call) | The heartbeat thread keeps `heartbeat_at` fresh; the sweeper never reclaims the job |
| worker | Worker killed mid-job | The heartbeat stops; after 5 minutes the sweeper re-queues; `attempts` = 2 |
| worker | A job that crashes the worker every time | Runs at most 3 times, then `failed` with `worker_crashed`, and the webhook is sent |
| worker | Job row committed but never queued (simulated API crash) | The sweeper queues it after 2 minutes; it runs once |
| worker | Heartbeat thread after the job finished | Doesn't change `status`; stops in `finally` |
| api | Same `Idempotency-Key`, different `quiz.count` | 422 `idempotency_mismatch`; no new job |
| api | 50 MB file in a multipart body (about 50.1 MB on the wire) | Accepted by the middleware; the file size is checked in `inspect()` |
| sweeper | Upload 31 minutes old without a job | Deleted from storage and the table |
| sweeper | 100 jobs `queued` for 30 minutes behind a busy worker pool | Each job re-queued at most 3 times (at 2, 6 and 14 minutes), then an alert; no job runs twice |
| sweeper | A job whose message was really lost | Re-queued after 2 minutes and runs once |
| worker | Heartbeat thread during a job | Uses its own connection (assert it isn't the task's connection); the connection is closed after the job |
| webhook | First attempt fails; retry 2 hours later | New timestamp and a valid signature; same event ID and body |
| filetype | ODT saved with a password (`manifest:encryption-data` in `META-INF/manifest.xml`) | `encrypted` |
| probe | Owner-password-only PDF with AES encryption | Accepted (needs `cryptography`) |
| startup | `cryptography` not installed | The self-test fails at startup with a clear message, instead of refusing good PDFs as `damaged_file` |
| service | Topic request with `language="ta"` | `topic_facts` gives `language_source="request"`; routed to `topic_bank` through `route()` |
| api | `pebble` future awaited with `asyncio.wrap_future` | Returns the result; timeouts arrive as `TimeoutError` |

---

## 11. Acceptance criteria

- [ ] No decision anywhere uses the file name, except choosing between `txt`, `md` and `csv` (and only when the content allows it).
- [ ] Bodies over the cap are refused before the app receives them.
- [ ] Every safety check runs before any library opens the content: size, zip bomb (declared **and** real sizes), macros, encryption (including old Office RC4), damage, pages, JavaScript and image size.
- [ ] Inspection runs in a child process with a hard kill and a memory limit. A hostile file costs at most 10 seconds of one slot, and other requests wait at most 2 seconds for a slot.
- [ ] Input modes never cross: text and topic requests can't reach file pipelines.
- [ ] The chosen pipeline can always read the file's type; anything skipped is recorded.
- [ ] The service refuses to start with an invalid or ambiguous config, and the error explains the fix.
- [ ] Retrying `POST /v1/jobs` never creates a second job, and a redelivered task never runs a job twice.
- [ ] Every project has rate limits and quotas, plus a global ceiling.
- [ ] Webhooks are signed and timestamped; every retry is re-signed with the same event ID.
- [ ] No job is ever lost or run more than `max_attempts` times: a sweeper re-queues lost tasks (with backoff, at most 3 times each) and reclaims dead workers, and the heartbeat comes from a background thread with its own database connection.
- [ ] Same idempotency key with different options returns `idempotency_mismatch`.
- [ ] Expired uploads are deleted within minutes, by the service itself.
- [ ] Every job stores its decision record; worker tasks take only `job_id`.
- [ ] The service never handles credits. Clients charge after `/v1/route`, re-check the balance at charge time, and refund on failure.
- [ ] All tests in section 10 pass.

---

## 12. Open decisions (confirm with Rajkumar)

1. **Section 0 assumptions A–C:** standalone, all document types, 50 MB / 400-page safety caps.
2. **Does the form ask for the subject?** If yes, the classifier is only a fallback. If no, choose `LLMClassifier` for accuracy or `KeywordClassifier` for zero cost.
3. **Subject confidence threshold:** 0.7 to start. Adjust it from the decision records.
4. **Which language and subject pairs get their own pipeline:** only where reading or question settings really differ.
5. **Legacy Tamil:**
   - Collect real font names from uploads and extend the pattern.
   - Add a TSCII and Bamini to Unicode converter in the Tamil pipeline.
   - Possibly add a TSCII decoder so suspected legacy text files route as Tamil.
6. **Approximate languages:** is `hi` good enough for all Devanagari, `ru` for all Cyrillic, and `en` for all Latin? If not, add a language model for those scripts only.
7. **How long the service keeps finished quizzes**, and how long it keeps uploads after a job (privacy).
8. **Scan budget:** 6 seconds of OCR, a 10-second hard limit and 4 inspection slots per API process to start. Tune them from `probe_ms` in production.
9. **Rate-limit defaults** (section 9.5): set per project before any client other than QuickTest goes live.
10. **Job time limits:** a 16-minute hard limit per job and a 1-hour broker visibility timeout. Check them against your longest real jobs.
11. **Memory:** `probe_memory_mb` (1 GB to start) after testing Tesseract under the limit, and `PROBE_CONCURRENCY` × uvicorn workers against the container size.
12. **Encryption markers:** the `.doc`, `.xls` and `.ppt` checks (`fEncrypted` at offset `0x0A`, bit `0x0100`; `FILEPASS` `0x002F`; the `EncryptedSummary` stream) follow [MS-DOC], [MS-XLS] and [MS-PPT]. Confirm them with real encrypted sample files from Word, Excel and PowerPoint before relying on them; the tests use those samples.

---

## Change log

### v4 → v4.1 (fourth review)

| Review point | Fix | Section |
|---|---|---|
| The sweeper floods the queue during a backlog | `requeue_count` with growing gaps (2, 4, 8 minutes), at most 3 re-queues, then an alert | 9.6, 10 |
| Heartbeat thread and database connections | The thread opens and closes its own connection | 9.6, 10 |
| `language_source` list missing `"request"` | Added to the `FileFacts` comment | 5.4 |

### v3 → v4 (third review)

| Review point | Fix | Section |
|---|---|---|
| Must 1. A blocked heartbeat lets a running job be reclaimed | Heartbeat from a background thread; hard time limit kills the process if it truly hangs | 9.6, 10 |
| Must 2. Job committed but never queued | Sweeper re-queues `queued` jobs older than 2 minutes; `enqueued_at` column | 9.3, 9.6, 10 |
| Must 3. Webhook retries fail the 5-minute check | Each attempt re-signed with a fresh timestamp; same event ID and body | 9.4, 10 |
| Must 4. `cryptography` missing | `pypdf[crypto]`; `DependencyError` is a 500, never `damaged_file`; startup self-test | 4, 5.4, 10 |
| No attempt limit | `max_attempts = 3` in the claim; the sweeper marks `worker_crashed` | 9.6, 10 |
| Same key, different options | `request_hash`; 422 `idempotency_mismatch` | 8, 9.3, 10 |
| Encrypted ODF files | `manifest:encryption-data` → `encrypted` | 5.1, 10 |
| Multipart overhead | Middleware limit is the cap + 1 MB, the same as the proxy | 9.2, 10 |
| 30-minute uploads vs day-based lifecycle rules | `expires_at` plus sweeper deletion; 1-day lifecycle as a backstop | 9.3, 9.6, 10 |
| Memory sizing; `RLIMIT_AS` and Tesseract | Sizing formula, and a test of OCR under the limit | 9.1, 12 |
| Topic mode had no facts | `topic_facts()`; every mode uses `route(facts, …)` | 5.4, 5.9, 9.3, 10 |
| Encryption byte values unverified; `pebble` + `wrap_future` | Confirm with real sample files; a test for the future | 10, 12 |

### v2 → v3 (second review)

| Review point | Fix | Section |
|---|---|---|
| Must 1. Tamil PPTX contradicts the starter set | `pptx` added to `tamil_default`'s readers | 6, 7, 10 |
| Must 2. The 50 MB check runs after Starlette has the whole body | Proxy limit plus a streaming body-size middleware | 9.2, 10 |
| Must 3. Duplicate jobs on retry | One job per upload and idempotency key, enforced by a unique constraint | 9.3, 10 |
| Must 4. Threads can't be killed | Inspection in a `pebble` process pool: hard kill at 10 s, memory limit, 2 s slot wait then 503 | 2, 9.1, 9.3, 10 |
| No per-project limits | Rate limits, quotas and a global ceiling in Redis | 5.8, 9.5, 10 |
| ZIP directories can lie | Stream-decompress every entry and count real bytes; capped `read_entry()` | 5.1, 10 |
| Images inside PDFs skip the pixel cap | Check `/Width` × `/Height` before decoding; Pillow bomb guard | 5.4, 10 |
| Unsigned webhooks | HMAC-SHA256 over timestamp and body, event IDs, retries | 9.4, 10 |
| `acks_late` redelivery | Atomic claim with heartbeat, visibility timeout longer than the job time limit | 9.6, 10 |
| `csv.Sniffer` on prose | Limited delimiters and a column-consistency rule; the hint alone isn't enough | 5.1, 10 |
| RC4-encrypted DOC, XLS, PPT | `fEncrypted` flag, `FILEPASS` record, `EncryptedSummary` stream | 5.1, 10 |
| Corrections re-probe the file | `FileFacts` stored with the upload; `route()` re-runs on it | 5.4, 5.9, 9.3, 10 |
| `scanned` on text pipelines | Validator warning | 5.6, 10 |
| XLSX row count in read-only mode | Fall back to the rows seen, at least 1 | 5.4, 10 |

### v1 → v2 (first review)

| Review point | Fix | Section |
|---|---|---|
| A. Built into QuickTest | Standalone service with its own API; QuickTest is a client | 0, 2, 9 |
| B. Too few types | All common types accepted; pipelines declare readers | 0, 5.1, 5.4, 6 |
| C. 2 MB plan limit | Safety caps only; optional stricter per-project limit | 0, 5.1, 5.8 |
| 1. Text and topic routed to file pipelines | `input` is a hard split; default `input: file` | 5.6, 5.7, 10 |
| 2. Encrypted or damaged PDFs give a 500 | `encrypted` and `damaged_file`; owner-only passwords accepted | 5.4, 8, 10 |
| 3. Zip bomb in the API | Size, ratio and entry checks before any entry is read | 5.1, 10 |
| 4. No page cap or PDF JavaScript check | `too_many_pages` and `pdf_javascript` | 5.4, 8 |
| 5. Django instead of FastAPI; Tesseract on the request | FastAPI endpoints, thread pool, concurrency limit, time budget | 5.4, 9.2 |
| 6. `config: dict = {}`; `.get` on a dataclass | `field(default_factory=…)`; matching via `as_facts()` | 5.6 |
| 7. UTF-8 only; BOM breaks the HTML check | BOMs, charset-normalizer, legacy-encoding flag | 5.1, 5.2 |
| 8. Legacy font rule too eager | 30% share rule; legacy text excluded from detection | 5.4 |
| 9. Script gaps | Odia, Sinhala, Urdu, Persian, CJK, Cyrillic, Greek, Hebrew and Thai added; approximate languages flagged | 5.3 |
| 10. Scan timing; JBIG2 and CCITT | 6-second budget, `pdftoppm` fallback, recorded outcomes | 5.4 |
| 11. Celery signature during deploys | Tasks take only `job_id`; old QuickTest signature kept for one release | 9.3, 9.4 |
| 12. Credits re-check | Balance re-checked at charge time, in the client | 9.4 |
