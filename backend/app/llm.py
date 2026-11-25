"""An optional, strictly decorative prose layer.

**This module is not the decision authority.** It cannot be.

The structural guarantees, in order of strength:

1. **It cannot import the decision machinery.** :mod:`app.llm` may not
   import :mod:`app.optimizer`, :mod:`app.validator`, :mod:`app.state` or
   :mod:`app.journal`. ``tests/test_llm_isolation.py`` enforces this with an
   AST import-graph check. The layer has no access to the world it would be
   describing.
2. **It receives a finished, immutable
   :class:`~app.explain.Explanation`.** By the time the rewriter runs, every
   decision has been made and every number fixed.
3. **It may only return prose.** :meth:`ProseRewriter.rewrite` returns a
   string or ``None``. There is no path from this module back to a plan.
4. **Unsafe prose is discarded.** Any number in the returned prose that is
   not in the explanation's ``numbers`` payload causes the rewrite to be
   thrown away and the original deterministic explanation to be used
   instead. A model that invents a figure is worse than no model.

``LLM_ENABLED`` defaults to **false**. The whole platform works, and
explains itself, with no language model in the loop at all.

The provider is called over plain HTTP with ``httpx``, which is already a
dependency. There is no new dependency.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from .explain import Explanation, numbers_in

LOGGER = logging.getLogger(__name__)

__all__ = [
    "LLMConfig",
    "ProseRewriter",
    "llm_config_from_env",
    "is_safe_prose",
]


#: The tolerance for matching a prose number against the declared numbers.
#: Generous enough to allow a model to round 1234.5 to "1,235" or to
#: express a percentage the explanation already states as a fraction.
NUMERIC_TOLERANCE = 0.51


@dataclass(frozen=True)
class LLMConfig:
    """How to reach the optional provider."""

    enabled: bool = False
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 4.0
    max_tokens: int = 400

    @property
    def configured(self) -> bool:
        """Is there enough here to make a call?"""
        return bool(self.enabled and self.base_url and self.model)

    def to_dict(self) -> dict[str, Any]:
        """A description of the configuration.

        The API key is **never** included -- only whether one is present.
        """
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key),
            "timeout": self.timeout,
        }


def llm_config_from_env() -> LLMConfig:
    """Read the LLM configuration from the environment.

    ``LLM_ENABLED`` defaults to **false** -- the platform is fully
    functional without a language model.
    """
    enabled = os.environ.get("LLM_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    try:
        timeout = float(os.environ.get("LLM_TIMEOUT", "4.0") or 4.0)
    except ValueError:
        timeout = 4.0
    try:
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "400") or 400)
    except ValueError:
        max_tokens = 400

    return LLMConfig(
        enabled=enabled,
        provider=os.environ.get("LLM_PROVIDER", "").strip(),
        model=os.environ.get("LLM_MODEL", "").strip(),
        base_url=os.environ.get("LLM_BASE_URL", "").strip(),
        api_key=os.environ.get("LLM_API_KEY", "").strip(),
        timeout=timeout,
        max_tokens=max_tokens,
    )


def is_safe_prose(
    prose: str, explanation: Explanation, *, tolerance: float = NUMERIC_TOLERANCE
) -> bool:
    """Does the prose avoid inventing figures?

    A prose number is acceptable when it matches a declared number in the
    explanation's ``numbers`` payload, within ``tolerance``. A model that
    rounds, reformats or restates a known figure is fine; a model that
    invents one is not.
    """
    declared: list[float] = []
    for value in explanation.numbers.values():
        try:
            declared.append(float(value))
        except (TypeError, ValueError):
            continue

    for number in numbers_in(prose):
        if not any(abs(number - d) <= tolerance for d in declared):
            return False
    return True


class ProseRewriter:
    """Rewrite an explanation's prose. Nothing else.

    The rewriter is deliberately thin. It builds a prompt from the
    explanation, asks a provider for a paraphrase, checks the result, and
    returns it -- or returns ``None`` and lets the caller keep the
    deterministic text.
    """

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or llm_config_from_env()

    def available(self) -> bool:
        """Can this rewriter actually run?"""
        return self.config.configured

    def rewrite(self, explanation: Explanation) -> Optional[str]:
        """Return a paraphrase of the explanation, or ``None``.

        ``None`` means "use the original". That happens when the layer is
        disabled, when the provider is unreachable, or when the provider's
        prose fails the numeric audit.
        """
        if not self.available():
            return None

        prompt = self._build_prompt(explanation)
        try:
            raw = self._call(prompt)
        except Exception as exc:
            LOGGER.warning("The prose layer failed: %s", exc)
            return None

        if not raw:
            return None

        prose = raw.strip()
        if not is_safe_prose(prose, explanation):
            LOGGER.warning(
                "Discarding prose that quotes a figure the explanation does "
                "not declare"
            )
            return None

        return prose

    # -- internals ---------------------------------------------------------- #
    def _build_prompt(self, explanation: Explanation) -> str:
        """The prompt.

        The explanation's *numbers* are given verbatim so the model has the
        figures to hand, and the instruction forbids inventing any. The
        model is not asked to compute anything.
        """
        facts = json.dumps(explanation.numbers, indent=2, sort_keys=True)
        return (
            "Rewrite the following operational summary in clear, plain "
            "English for a fuel-supply operator.\n\n"
            "Rules:\n"
            "- Use ONLY the numbers listed below. Do not compute, convert or "
            "invent any figure.\n"
            "- Do not add a recommendation that is not in the summary.\n"
            "- Keep it to at most three sentences.\n"
            "- Return only the rewritten summary, with no preamble.\n\n"
            f"Numbers you may use:\n{facts}\n\n"
            f"Summary to rewrite:\n{explanation.prose()}\n"
        )

    def _call(self, prompt: str) -> Optional[str]:
        """Call the provider and return the text it produced."""
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config.max_tokens,
            "temperature": 0.0,
        }

        with httpx.Client(timeout=self.config.timeout) as client:
            response = client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        choices = body.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        return message.get("content")
