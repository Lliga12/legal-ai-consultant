from core.diagnostics import report_input_error


def test_diagnostics_excludes_exception_message_and_local_secrets(caplog):
    private_value = "PRIVATE-CONTACT-AND-API-KEY"
    try:
        raise ValueError(private_value)
    except ValueError as exc:
        code = report_input_error(exc)
    assert "ValueError" in code
    assert "test_diagnostics_excludes_exception_message_and_local_secrets" in code
    assert "LEGAL_AI_INPUT_ERROR" in caplog.text
    assert private_value not in caplog.text + code
    assert "raise ValueError" not in caplog.text
