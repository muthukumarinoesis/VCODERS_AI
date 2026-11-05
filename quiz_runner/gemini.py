"""Gemini through the google-genai SDK. The runner only needs `complete()`,
so tests (and other providers) can pass any object with that method."""

DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiClient:
    def __init__(
        self, api_key: str, model: str = DEFAULT_MODEL, timeout_s: float = 120, temperature: float = 0.4
    ):
        from google import genai
        from google.genai import types

        self._types = types
        self.model, self.temperature = model, temperature
        self.client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000))
        )

    def complete(self, prompt: str, images=(), json: bool = False, model: str | None = None) -> str:
        t = self._types
        contents = [prompt] + [t.Part.from_bytes(data=img, mime_type="image/png") for img in images]
        response = self.client.models.generate_content(
            model=model or self.model,
            contents=contents,
            config=t.GenerateContentConfig(
                temperature=self.temperature,
                response_mime_type="application/json" if json else "text/plain",
            ),
        )
        _raise_if_blocked(response)
        return response.text or ""


UNSAFE = "This material can't be used to make a quiz: it was flagged as unsafe content."
BLOCKED_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}


def _raise_if_blocked(response) -> None:
    """Gemini's safety filters: a blocked prompt or answer fails the job with
    unsafe_content instead of being retried."""
    from .errors import RunnerError

    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        raise RunnerError("unsafe_content", UNSAFE)
    for candidate in getattr(response, "candidates", None) or ():
        reason = getattr(candidate, "finish_reason", None)
        if reason is not None and getattr(reason, "name", str(reason)) in BLOCKED_FINISH:
            raise RunnerError("unsafe_content", UNSAFE)
