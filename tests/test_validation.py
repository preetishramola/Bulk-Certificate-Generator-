from app.validation import validate_recipients


def test_valid_recipients_are_normalised():
    valid, rejected = validate_recipients([{"name": "  Ada Lovelace ", "email": "ada@example.com", "grade": " A "}])

    assert rejected == []
    assert valid[0].index == 0
    assert valid[0].recipient.name == "Ada Lovelace"
    assert valid[0].recipient.grade == "A"


def test_each_invalid_recipient_is_reported_with_its_index_and_reason():
    raw = [
        {"name": "Ada", "email": "ada@example.com"},
        {"name": "", "email": "x@example.com"},
        {"name": "Bob", "email": "not-an-email"},
        {"email": "missing-name@example.com"},
        {"name": "12345", "email": "digits@example.com"},
        {"name": "Eve", "email": "eve@example.com", "role": "admin"},
        "just a string",
        {"name": "Zoë 李", "email": "zoe@example.com"},
    ]

    valid, rejected = validate_recipients(raw)

    assert [v.index for v in valid] == [0]
    by_index = {r.index: " | ".join(r.errors) for r in rejected}
    assert set(by_index) == {1, 2, 3, 4, 5, 6, 7}
    assert "name" in by_index[1]
    assert "email" in by_index[2]
    assert "name: Field required" in by_index[3]
    assert "at least one letter" in by_index[4]
    assert "role: Extra inputs are not permitted" in by_index[5]
    assert "valid dictionary" in by_index[6]
    assert "cannot render" in by_index[7] and "李" in by_index[7]
    assert rejected[-1].raw == raw[7]  # raw input preserved for the report


def test_latin_accents_are_accepted():
    valid, rejected = validate_recipients([{"name": "José Müller-Øster", "email": "jose@example.com"}])
    assert rejected == [] and len(valid) == 1


def test_duplicate_emails_are_rejected_case_insensitively():
    valid, rejected = validate_recipients(
        [
            {"name": "Ada", "email": "ada@example.com"},
            {"name": "Ada Again", "email": "ADA@example.com"},
        ]
    )

    assert [v.index for v in valid] == [0]
    assert rejected[0].index == 1
    assert rejected[0].errors == ["email: duplicate of recipient at index 0"]
