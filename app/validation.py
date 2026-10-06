from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas import RecipientIn


@dataclass(frozen=True)
class ValidRecipient:
    index: int
    recipient: RecipientIn


@dataclass(frozen=True)
class RejectedRecipient:
    index: int
    raw: Any
    errors: list[str]


def _format_errors(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"])
        messages.append(f"{field}: {err['msg']}" if field else err["msg"])
    return messages


def validate_recipients(raw_recipients: list[Any]) -> tuple[list[ValidRecipient], list[RejectedRecipient]]:
    """
    Validate each recipient independently.

    Rules beyond the field-level schema:
    - an email may appear only once per job (case-insensitive); later duplicates are rejected.
    """
    valid: list[ValidRecipient] = []
    rejected: list[RejectedRecipient] = []
    first_index_by_email: dict[str, int] = {}

    for index, raw in enumerate(raw_recipients):
        try:
            recipient = RecipientIn.model_validate(raw)
        except ValidationError as exc:
            rejected.append(RejectedRecipient(index, raw, _format_errors(exc)))
            continue

        email_key = recipient.email.lower()
        if email_key in first_index_by_email:
            rejected.append(
                RejectedRecipient(
                    index, raw, [f"email: duplicate of recipient at index {first_index_by_email[email_key]}"]
                )
            )
            continue

        first_index_by_email[email_key] = index
        valid.append(ValidRecipient(index, recipient))

    return valid, rejected
