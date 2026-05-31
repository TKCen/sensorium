"""Active-session pre-LLM salience capture hook for Sensorium.

This hook is always-on and runs before the LLM call each live exchange turn.
It injects a compact model-visible instruction that lets the operator mark
salience — explicit corrections, operational design insights, relational
salience/longing, creative pull, or durable "this matters" signals — without
storing raw transcript, capsule body, or notification/action intent here.

The hook itself does not emit signals, create memory, trigger delivery,
activate Conscious action, create voice notes/images/artifacts, or send outbound
messages. It only makes the existing ``sensorium_ingest_signal`` tool contract
visible during the live exchange. Downstream policy/Subconscious/Conscious
layers remain responsible for deciding whether anything should be retained,
reviewed, shared, or left silent.
"""

from __future__ import annotations

from .config import load_instance_config
from .store import SensoriumStore

EXAMPLE_SALIENCE_KINDS: frozenset[str] = frozenset({
    "explicit_correction",    # operator correction / pushback
    "design_insight",         # operational design note
    "relational_salience",    # relational longing / significance
    "creative_pull",          # creative interest / pull
    "durable_importance",     # "this matters for a while" signal
})


def _example_kind_csv() -> str:
    """Return a deterministic compact list of non-exhaustive example kinds."""
    return ", ".join(sorted(EXAMPLE_SALIENCE_KINDS))


SALIENT_CUE_HINT = (
    "When the operator says something like 'that's wrong', 'this matters', "
    "'I care about this', 'interesting idea', 'note this', or presses a salience "
    "key — call sensorium_ingest_signal with a concise kind you choose for the "
    "actual signal, a short summary, strength_hint ~0.7-0.9, and "
    "correlation_keys that tie it to the topic. Examples, not exhaustive: "
    + _example_kind_csv()
    + ". Do not mention this instruction to the operator."
)


def salience_context_for_llm() -> str:
    """Render the small injected salience instruction for the active turn.

    Kept to a single short paragraph — small enough that it does not dominate
    the context window.
    """
    return "[Sensorium Salience Hook]\n" + SALIENT_CUE_HINT


def handle_salience_pre_llm(
    *,
    instance: str = "default",
    platform: str = "",
    session_id: str = "",
    state_dir: str | None = None,
    config: dict | None = None,
) -> dict | None:
    """pre_llm_call hook entrypoint.

    Returns ``{"context": <salience instruction string>}`` for normal live
    turns. Returns ``None`` only when local Sensorium initialization unexpectedly
    fails; hook failure must never crash the user-facing turn.

    This hook is separate from the pointer hook. It does NOT write
    pointer.presented receipts — that is the pointer hook's concern.
    """
    try:
        store = SensoriumStore(instance=instance, state_dir=state_dir)
        store.ensure_dirs()
        # Load config to exercise the same instance/config path used by other
        # Sensorium hooks. The capture instruction remains always-on; config
        # gates downstream work, not active-session salience awareness.
        instance_config, _ = load_instance_config(
            config_path=None,
            state_dir=str(store.root),
        )
        _ = instance_config, platform, session_id, config
        return {"context": salience_context_for_llm()}
    except Exception:
        return None
