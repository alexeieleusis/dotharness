from unittest.mock import Mock

import click
import pytest

from spec_prism_flow.build import manual_test
from spec_prism_flow.build.errors import ManualTestFailed
from spec_prism_flow.build.manual_test import ManualTestOutcome


def test_prompt_skips_and_returns_passed_on_empty_checklist(monkeypatch):
    confirm_mock = Mock()
    prompt_mock = Mock()
    monkeypatch.setattr(click, "confirm", confirm_mock)
    monkeypatch.setattr(click, "prompt", prompt_mock)

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", [])

    assert outcome == ManualTestOutcome(passed=True, retry=False, notes=None)
    confirm_mock.assert_not_called()
    prompt_mock.assert_not_called()


def test_prompt_skips_and_returns_passed_on_empty_checklist_even_when_strict(monkeypatch):
    confirm_mock = Mock()
    prompt_mock = Mock()
    monkeypatch.setattr(click, "confirm", confirm_mock)
    monkeypatch.setattr(click, "prompt", prompt_mock)

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", [], strict=True)

    assert outcome == ManualTestOutcome(passed=True, retry=False, notes=None)
    confirm_mock.assert_not_called()
    prompt_mock.assert_not_called()


def test_prompt_returns_passed_outcome_when_all_items_pass(monkeypatch):
    monkeypatch.setattr(click, "confirm", Mock(return_value=True))

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", ["step one", "step two"])

    assert outcome == ManualTestOutcome(passed=True, retry=False, notes=None)


def test_prompt_returns_retry_outcome_on_fail_then_retry(monkeypatch):
    monkeypatch.setattr(click, "confirm", Mock(side_effect=[False, True]))
    monkeypatch.setattr(click, "prompt", Mock(return_value="the button did nothing"))

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", ["step one"])

    assert outcome == ManualTestOutcome(passed=False, retry=True, notes="the button did nothing")


def test_prompt_returns_failed_non_raising_outcome_on_fail_then_decline_non_strict(monkeypatch):
    monkeypatch.setattr(click, "confirm", Mock(side_effect=[False, False]))
    monkeypatch.setattr(click, "prompt", Mock(return_value=""))

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", ["step one"])

    assert outcome == ManualTestOutcome(passed=False, retry=False, notes=None)


def test_prompt_default_strict_is_false(monkeypatch):
    monkeypatch.setattr(click, "confirm", Mock(side_effect=[False, False]))
    monkeypatch.setattr(click, "prompt", Mock(return_value="notes"))

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", ["step one"])

    assert outcome.passed is False
    assert outcome.retry is False


def test_prompt_raises_manual_test_failed_on_fail_then_decline_strict(monkeypatch):
    monkeypatch.setattr(click, "confirm", Mock(side_effect=[False, False]))
    monkeypatch.setattr(click, "prompt", Mock(return_value="notes here"))

    with pytest.raises(ManualTestFailed) as exc_info:
        manual_test.prompt(7, "build-agent-runner-leaf", ["step one"], strict=True)

    assert exc_info.value.notes == "notes here"


def test_prompt_does_not_raise_on_pass_even_when_strict(monkeypatch):
    monkeypatch.setattr(click, "confirm", Mock(return_value=True))

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", ["step one"], strict=True)

    assert outcome == ManualTestOutcome(passed=True, retry=False, notes=None)


def test_prompt_does_not_raise_on_fail_then_retry_even_when_strict(monkeypatch):
    monkeypatch.setattr(click, "confirm", Mock(side_effect=[False, True]))
    monkeypatch.setattr(click, "prompt", Mock(return_value=""))

    outcome = manual_test.prompt(7, "build-agent-runner-leaf", ["step one"], strict=True)

    assert outcome == ManualTestOutcome(passed=False, retry=True, notes=None)


def test_prompt_uses_the_documented_confirm_prompts_and_defaults(monkeypatch):
    confirm_mock = Mock(side_effect=[False, False])
    monkeypatch.setattr(click, "confirm", confirm_mock)
    monkeypatch.setattr(click, "prompt", Mock(return_value=""))

    manual_test.prompt(7, "build-agent-runner-leaf", ["step one"])

    first_call, second_call = confirm_mock.call_args_list
    assert first_call.args[0] == "Did every item pass?"
    assert first_call.kwargs.get("default") is False
    assert second_call.args[0] == "Spend one more review cycle and retry, instead of escalating now?"
    assert second_call.kwargs.get("default") is False


def test_prompt_echoes_every_checklist_item(monkeypatch):
    echo_mock = Mock()
    monkeypatch.setattr(click, "echo", echo_mock)
    monkeypatch.setattr(click, "confirm", Mock(return_value=True))

    manual_test.prompt(7, "build-agent-runner-leaf", ["step one", "step two"])

    echoed = "\n".join(call.args[0] for call in echo_mock.call_args_list)
    assert "step one" in echoed
    assert "step two" in echoed
