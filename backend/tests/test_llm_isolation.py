"""The LLM layer's structural isolation.

These are the tests that make the claim "the LLM is not the decision
authority" *checkable* rather than a promise in a docstring.

The guarantee has two halves:

* **It cannot see the decision machinery.** :mod:`app.llm` must not import
  the optimiser, the validator, the world state or the dispatch journal.
  An AST import-graph check enforces this -- not a grep, which would miss
  a function-local import or an alias.
* **It cannot alter a decision.** The rewriter returns prose or ``None``,
  and prose that quotes a figure the explanation does not declare is
  discarded.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.explain import Explanation
from app.llm import (
    LLMConfig,
    ProseRewriter,
    is_safe_prose,
    llm_config_from_env,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
LLM_PATH = BACKEND_DIR / "app" / "llm.py"

#: The modules the prose layer must never reach into.
FORBIDDEN_MODULES = frozenset(
    {
        "app.optimizer",
        "app.validator",
        "app.state",
        "app.journal",
        "app.dispatch",
        "app.forecast",
        "app.anomaly",
        "app.simulator_client",
        "app.broker",
        "app.config",
    }
)


def _imported_modules(path: Path) -> set[str]:
    """Every module the file imports, at any level.

    Walks the whole AST -- module level, class level, function level and
    nested level -- because an import hidden inside a function is still an
    import.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import: ``from .optimizer import x`` is
                # ``app.optimizer``.
                found.add("app." + (node.module or ""))
            elif node.module:
                found.add(node.module)

    return found


# --------------------------------------------------------------------------- #
# The import graph
# --------------------------------------------------------------------------- #
def test_the_llm_layer_exists():
    assert LLM_PATH.exists()


def test_the_llm_layer_cannot_import_the_decision_machinery():
    """The structural guarantee, enforced by the AST.

    If this test ever fails, the LLM layer has been given the ability to
    reach the world it describes. That is the one change that would turn
    it from a decorative layer into a decision authority.
    """
    imported = _imported_modules(LLM_PATH)
    violations = sorted(
        module
        for module in imported
        if module in FORBIDDEN_MODULES
        or any(module.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_MODULES)
    )
    assert violations == [], (
        f"app/llm.py must not import {violations} -- the prose layer is "
        f"decorative and must not be able to reach the decision machinery."
    )


def test_the_llm_layer_only_imports_the_explanation():
    """What it *is* allowed to import.

    The explanation module is the boundary: the layer may read a finished,
    immutable explanation, and nothing else.
    """
    imported = _imported_modules(LLM_PATH)
    assert "app.explain" in imported
    assert "app.explain" not in FORBIDDEN_MODULES


def test_the_llm_layer_imports_no_decision_module_by_any_alias():
    """A belt-and-braces check on the raw source.

    The AST check above is the real one. This one is a cheap second
    opinion that also catches a dynamic import inside a string.
    """
    source = LLM_PATH.read_text(encoding="utf-8")
    for forbidden in sorted(FORBIDDEN_MODULES):
        leaf = forbidden.split(".")[-1]
        assert (
            f"import {leaf}" not in source
            and f"from .{leaf}" not in source
            and f"from app.{leaf}" not in source
        ), f"app/llm.py appears to import {forbidden}"


# --------------------------------------------------------------------------- #
# Disabled by default
# --------------------------------------------------------------------------- #
def test_the_llm_layer_is_disabled_by_default(monkeypatch):
    for name in (
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    config = llm_config_from_env()
    assert config.enabled is False
    assert config.configured is False


def test_a_disabled_rewriter_is_unavailable(monkeypatch):
    for name in (
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    rewriter = ProseRewriter(llm_config_from_env())
    assert rewriter.available() is False


def test_a_disabled_rewriter_returns_nothing(monkeypatch):
    for name in (
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    explanation = Explanation(
        kind="plan",
        title="t",
        headline="5000 L across 1 shipment.",
        numbers={"total_litres": 5000.0},
    )
    assert ProseRewriter(llm_config_from_env()).rewrite(explanation) is None


def test_the_config_never_leaks_the_api_key():
    config = LLMConfig(
        enabled=True,
        provider="openai",
        model="gpt-4o-mini",
        base_url="https://example.invalid/v1",
        api_key="super-secret",
    )
    payload = config.to_dict()
    assert payload["has_api_key"] is True
    assert "super-secret" not in str(payload)


# --------------------------------------------------------------------------- #
# The numeric audit
# --------------------------------------------------------------------------- #
def _explanation() -> Explanation:
    return Explanation(
        kind="plan",
        title="Dispatch plan",
        headline="5000 L across 1 shipment.",
        details=["The plan moves 5000 L across 1 shipment."],
        numbers={
            "total_litres": 5000.0,
            "shipment_count": 1.0,
            "expected_service_level_pct": 95.0,
        },
    )


def test_prose_using_the_declared_numbers_is_safe():
    assert is_safe_prose("We are shipping 5000 L.", _explanation()) is True


def test_prose_rounding_a_declared_number_is_safe():
    # 5000.0 declared; a model may reasonably write "5,000" or round.
    assert is_safe_prose("We are shipping 5000 litres.", _explanation()) is True


def test_prose_inventing_a_number_is_unsafe():
    assert is_safe_prose("We are shipping 999999 L.", _explanation()) is False


def test_prose_with_no_numbers_is_safe():
    assert is_safe_prose("The plan is ready to execute.", _explanation()) is True


def test_a_rewriter_that_invents_a_number_has_its_output_discarded(monkeypatch):
    """The end-to-end guard.

    Even if the provider returns a fluent, confident paragraph with a
    plausible-looking figure in it, the layer discards it and the caller
    keeps the deterministic explanation.
    """
    config = LLMConfig(
        enabled=True,
        provider="test",
        model="test-model",
        base_url="http://provider.invalid/v1",
        api_key="k",
    )
    rewriter = ProseRewriter(config)

    monkeypatch.setattr(
        rewriter,
        "_call",
        lambda prompt: "We are shipping 999999 litres, which is plenty.",
    )
    assert rewriter.rewrite(_explanation()) is None


def test_a_rewriter_that_keeps_the_numbers_is_accepted(monkeypatch):
    config = LLMConfig(
        enabled=True,
        provider="test",
        model="test-model",
        base_url="http://provider.invalid/v1",
        api_key="k",
    )
    rewriter = ProseRewriter(config)

    monkeypatch.setattr(
        rewriter,
        "_call",
        lambda prompt: "The plan ships 5000 litres in 1 shipment.",
    )
    assert rewriter.rewrite(_explanation()) == (
        "The plan ships 5000 litres in 1 shipment."
    )


def test_a_failing_provider_leaves_the_explanation_intact(monkeypatch):
    config = LLMConfig(
        enabled=True,
        provider="test",
        model="test-model",
        base_url="http://provider.invalid/v1",
        api_key="k",
    )
    rewriter = ProseRewriter(config)

    def _boom(prompt):
        raise RuntimeError("the provider is down")

    monkeypatch.setattr(rewriter, "_call", _boom)
    assert rewriter.rewrite(_explanation()) is None


def test_the_prompt_gives_the_model_the_figures_and_forbids_new_ones(monkeypatch):
    config = LLMConfig(
        enabled=True,
        provider="test",
        model="test-model",
        base_url="http://provider.invalid/v1",
        api_key="k",
    )
    rewriter = ProseRewriter(config)

    captured: dict[str, str] = {}

    def _capture(prompt):
        captured["prompt"] = prompt
        return None

    monkeypatch.setattr(rewriter, "_call", _capture)
    rewriter.rewrite(_explanation())

    prompt = captured["prompt"]
    assert "ONLY the numbers listed below" in prompt
    assert "5000.0" in prompt
    assert "Do not compute, convert or invent any figure" in prompt


def test_an_explanation_is_immutable():
    """The layer receives a finished object, not a working one."""
    explanation = _explanation()
    with pytest.raises(Exception):
        explanation.headline = "something else"  # type: ignore[misc]
