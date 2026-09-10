# Prospective evidence v0 privacy verification

The added seams pass only scalar IDs and closed classifications to the detached
recorder. `attention_snapshot_evidence` emits class labels only; it does not
emit candidate summaries, correlation keys, source IDs, or text. Its join uses
only exact `source_candidate_ids` already in the same aperture transaction.

`correction_stage_for_signal_kind` accepts only exact structured kind values.
It never examines `summary`, text, settlement, promotion, or any status field.
Unsupported values are absent from the correction/retraction path.

The recorder's allowlist rejects nested structures and raw text/locator/control
fields. It HMACs input IDs before persistence. Existing prospective tests prove
raw values do not occur in the SQLite payload and generator features remain
separated from frozen labels.
