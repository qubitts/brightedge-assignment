from dataclasses import dataclass, field
from typing import Optional

from src.config import settings
from src.parser.html_parser import ParseResult

BLOCKING_PHRASES = [
    "access denied",
    "robot check",
    "please verify",
    "captcha",
    "blocked",
    "security check",
    "are you a robot",
    "attention required",
    "just a moment",
    "bot protection",
    "403 forbidden",
    "pardon our interruption",
]


@dataclass
class ContentValidation:
    is_valid: bool
    reason: Optional[str] = None
    signals: dict = field(default_factory=dict)


def validate_content(parse_result: ParseResult) -> ContentValidation:
    """Check if parsed content is valid or blocked by bot detection."""
    body = parse_result.body
    title = parse_result.title or ""

    # Check 1: Blocking phrases in body text or title (check before length
    # so that short bot-detection pages are reported as BLOCKED, not EMPTY)
    text_lower = (title + " " + body).lower()
    for phrase in BLOCKING_PHRASES:
        if phrase in text_lower:
            return ContentValidation(
                is_valid=False,
                reason="BLOCKED",
                signals={"blocking_phrase": phrase},
            )

    # Check 2: Body length
    if len(body) < settings.MIN_CONTENT_LENGTH:
        return ContentValidation(
            is_valid=False,
            reason="EMPTY_CONTENT",
            signals={"text_length": len(body)},
        )

    return ContentValidation(is_valid=True, signals={"text_length": len(body)})
