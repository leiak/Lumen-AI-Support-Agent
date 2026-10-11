"""Process-wide runtime LLM provider chain (Tier 3 Task 3.4).

The chain is a list of ``(provider_name, model)`` tuples plus a
parallel ``enabled`` flag list. It is the source of truth for the
fallback resolver built by
:func:`llm_client.tenant_resolver.build_tenant_resolver` — replacing
the env-only ``LLM_FALLBACK_CHAIN`` read it used to do on every
gateway construction.

Lifecycle
---------

1. **Cold start**: ``initial_steps()`` is called once from
   ``build_tenant_resolver`` with the env-parsed chain. All steps
   are ``enabled=True``.
3. **Mutation**: ``replace_steps()`` validates the incoming steps
   against the registered global providers (raises on any unknown
   provider), then swaps atomically under the lock. Concurrent readers
   see either the old or the new chain — never a torn list.
4. **Read**: ``active_chain()`` returns the (provider, model) pairs
   that are enabled, in order, for the gateway to wire up as
   ``default_fallback_chain``.

Why mutable at runtime
----------------------

Operators occasionally need to disable a flaky provider *now* (e.g.
a vendor rate-limit incident) without waiting for a config deploy.
This gives them a knob. The chain is global — per-tenant routing
remains controlled by ``TenantLLMConfig`` (M4.C) — so this only
affects the *default* fallback for tenants without an explicit
BYOK config.

Thread safety
-------------

``asyncio.Lock`` because the only consumers are async (FastAPI
endpoints + gateway construction). Synchronous callers (e.g.
maintenance scripts) can use ``initial_steps()`` / ``snapshot()``
without locking — those methods don't race with replace.

PII discipline
--------------

The chain carries provider + model names only. API keys live on
``BaseProvider`` and never enter the chain.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class ChainStep:
    """One row in the runtime chain (provider, model, enabled)."""

    provider: str
    model: str
    enabled: bool = True

    def as_tuple(self) -> tuple[str, str]:
        """Return ``(provider, model)`` — used by the gateway builder."""
        return (self.provider, self.model)


class RuntimeChain:
    """Mutable process-level chain of ``(provider, model)`` steps."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._steps: list[ChainStep] = []

    # ---- read ---------------------------------------------------------

    def snapshot(self) -> list[ChainStep]:
        """Return a copy of the current chain (no lock — read-only)."""
        return list(self._steps)

    def active_chain(self) -> list[tuple[str, str]]:
        """Return enabled steps in order — gateway builder input."""
        return [s.as_tuple() for s in self._steps if s.enabled]

    # ---- write --------------------------------------------------------

    def initial_steps(self, steps: list[tuple[str, str]]) -> None:
        """Seed the chain from the env (call once at boot).

        Subsequent ``initial_steps()`` calls are silently ignored —
        use :meth:`replace_steps` for runtime mutation. This is the
        pattern ``build_tenant_resolver`` uses on its first cache
        miss.
        """
        if self._steps:
            return
        self._steps = [ChainStep(p, m, True) for p, m in steps]

    async def replace_steps(
        self,
        steps: list[ChainStep],
        *,
        known_providers: set[str],
    ) -> None:
        """Replace the chain atomically, validating providers.

        Args:
            steps: New chain rows. ``provider`` must be a known
                global provider; ``model`` is accepted as-is (the
                gateway only validates at call time).
            known_providers: Set of valid provider names (built from
                ``build_provider_registry``).

        Raises:
            ValueError: A step references an unknown provider, OR
                the resulting chain has zero enabled steps (we
                reject an empty chain so the gateway doesn't fall
                back to ``_PrefixResolver`` unexpectedly).
        """
        async with self._lock:
            cleaned: list[ChainStep] = []
            for s in steps:
                if s.provider not in known_providers:
                    raise ValueError(
                        f"unknown provider {s.provider!r}; "
                        f"registered: {sorted(known_providers)}"
                    )
                cleaned.append(ChainStep(s.provider, s.model, s.enabled))
            if not any(s.enabled for s in cleaned):
                raise ValueError(
                    "chain must contain at least one enabled step"
                )
            self._steps = cleaned


# Module-level singleton (process-wide). Tests reset via
# ``llm_client.runtime_chain._runtime_chain = RuntimeChain()``.
_runtime_chain = RuntimeChain()


def get_runtime_chain() -> RuntimeChain:
    """Return the process-level :class:`RuntimeChain` singleton."""
    return _runtime_chain


def reset_runtime_chain_for_tests() -> None:
    """Replace the singleton with a fresh empty chain. Tests only."""
    global _runtime_chain
    _runtime_chain = RuntimeChain()


__all__ = ["ChainStep", "RuntimeChain", "get_runtime_chain"]