from spec_prism_flow.build.errors import (
    CommandError,
    EmptyImplementationError,
    ManualTestFailed,
    MergeGateFailure,
    OrchestrationError,
    RetryBudgetExhausted,
    ScopeViolation,
)


def test_command_error_message_format():
    err = CommandError(["git", "push"], 1, "  fatal: not a git repository  \n")

    assert str(err) == "`git push` exited 1: fatal: not a git repository"
    assert err.cmd_args == ["git", "push"]
    assert err.returncode == 1


def test_orchestration_error_defaults():
    err = OrchestrationError("something went wrong")

    assert err.exit_code == 1
    assert err.next_command is None
    assert err.phase_number is None
    assert err.phase_name is None


def test_with_context_sets_both_fields_and_returns_self():
    err = OrchestrationError("something went wrong")

    result = err.with_context(phase_number=5, phase_name="foo")

    assert result is err
    assert err.phase_number == 5
    assert err.phase_name == "foo"


def test_with_context_is_chainable_off_a_raised_error():
    try:
        raise EmptyImplementationError().with_context(phase_number=5, phase_name="foo")
    except EmptyImplementationError as err:
        assert err.exit_code == 10
        assert err.phase_number == 5
        assert err.phase_name == "foo"


def test_empty_implementation_error_exit_code_and_no_next_command():
    err = EmptyImplementationError()

    assert err.exit_code == 10
    assert err.next_command is None


def test_scope_violation_exit_code_fields_and_next_command():
    err = ScopeViolation(["evil/App.tsx"], ["src/*.py"], "main")

    assert err.exit_code == 11
    assert err.offending_files == ["evil/App.tsx"]
    assert err.allowed_globs == ["src/*.py"]
    assert err.base_ref == "main"
    assert err.next_command == "git diff --name-only main...HEAD"


def test_merge_gate_failure_exit_code_fields_and_next_command():
    err = MergeGateFailure("uv run pytest", "line 1\nline 2")

    assert err.exit_code == 12
    assert err.gate_name == "uv run pytest"
    assert err.output_tail == "line 1\nline 2"
    assert err.next_command == "re-run 'uv run pytest' in the clone to reproduce"


def test_retry_budget_exhausted_exit_code_and_fields():
    err = RetryBudgetExhausted(3, "https://example.com/pr/1", 2)

    assert err.exit_code == 13
    assert err.cycles == 3
    assert err.pr_url == "https://example.com/pr/1"
    assert err.unresolved_thread_count == 2
    assert err.next_command is None


def test_manual_test_failed_exit_code_and_optional_notes():
    bare = ManualTestFailed()
    noted = ManualTestFailed("checklist item 3 failed")

    assert bare.exit_code == 14
    assert bare.notes is None
    assert str(bare) == "Manual test failed"
    assert noted.exit_code == 14
    assert noted.notes == "checklist item 3 failed"
    assert str(noted) == "Manual test failed: checklist item 3 failed"
    assert noted.next_command is None
