"""Quiz options: validated by the API, applied by the worker as the job layer."""

import copy

from quiz_runner.questions import SUPPORTED_TYPES

DIFFICULTY_MIX = {
    "easy": {"easy": 0.6, "medium": 0.3, "hard": 0.1},
    "medium": {"easy": 0.2, "medium": 0.6, "hard": 0.2},
    "hard": {"easy": 0.1, "medium": 0.3, "hard": 0.6},
}


class BadOptions(ValueError):
    code = "invalid_options"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def validate(quiz: dict, pipeline_config: dict) -> dict:
    """Normalised options or BadOptions. Types must be in the pipeline's
    quiz.types when it lists them, else in SUPPORTED_TYPES."""
    count = quiz.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 500:
        raise BadOptions("count must be a whole number from 1 to 500.")
    allowed = (pipeline_config.get("quiz") or {}).get("types") or SUPPORTED_TYPES
    types = quiz.get("types") or list(allowed)[:1]
    bad = [t for t in types if t not in allowed]
    if bad:
        raise BadOptions(f"Question types {', '.join(bad)} aren't available here. Use: {', '.join(allowed)}.")
    difficulty = quiz.get("difficulty", "medium")
    if isinstance(difficulty, str):
        if difficulty not in DIFFICULTY_MIX:
            raise BadOptions("difficulty must be easy, medium, hard or a mix.")
        mix = DIFFICULTY_MIX[difficulty]
    elif isinstance(difficulty, dict):
        if not difficulty or set(difficulty) - set(DIFFICULTY_MIX):
            raise BadOptions("A difficulty mix may only use easy, medium and hard.")
        if any(not isinstance(v, (int, float)) or v < 0 for v in difficulty.values()):
            raise BadOptions("Difficulty shares must be 0 or more.")
        if abs(sum(difficulty.values()) - 1) > 1e-6:
            raise BadOptions("A difficulty mix must add up to 1.")
        mix = {k: float(difficulty.get(k, 0)) for k in DIFFICULTY_MIX}
    else:
        raise BadOptions("difficulty must be easy, medium, hard or a mix.")
    options = {"count": count, "types": list(types), "difficulty": mix}
    focus = quiz.get("focus")
    if focus is not None:
        if not isinstance(focus, str) or not focus.strip() or len(focus) > 200:
            raise BadOptions(
                "focus must be some text of at most 200 characters, like 'chapter 3: photosynthesis'."
            )
        options["focus"] = " ".join(focus.split())
    return options


def apply_job_layer(config: dict, options: dict) -> dict:
    """The pipeline config with the job's quiz options on top. Keys listed in
    the pipeline's quiz.locked keep the pipeline's value."""
    merged = copy.deepcopy(config)
    quiz = merged.setdefault("quiz", {})
    locked = set(quiz.get("locked") or ())
    quiz.update({k: v for k, v in options.items() if k not in locked})
    return merged
