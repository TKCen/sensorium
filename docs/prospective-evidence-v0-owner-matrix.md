# Prospective evidence v0 owner matrix

The recorder is detached and each row below is post-success/best-effort. No
owner reads the recorder result. Canonical state means only the owner JSONL
files, never the study subtree or its configuration.

| Production owner | Observer stage | Structured evidence only | Failure containment |
|---|---|---|---|
| accepted/non-promoted signal | `source_observed` | source class and inhibit owner outcome | `observe_after_success` |
| promoted signal candidate path | `candidate_updated` | IDs, source class, coalesced state | `observe_after_success` |
| trusted ordinary event candidate update | `candidate_updated` | IDs and coalesced state | `observe_after_success` |
| Kanban settlement | `settled` | written settlement outcome | `observe_after_success` |
| pointer presentation | `presented` | post-write candidate reference | `observe_after_success` |
| Conscious open | `opened` | selected candidate reference | `observe_after_success` |
| Conscious chosen/settled | `settled` | written conscious outcome | `observe_after_success` |
| structured correction/retraction | `correction` / `retraction` | exact signal `kind` mapping | `observe_after_success` |
| bounded attention snapshot | `attention_snapshot` | exact candidate kind mapping in same open transaction | `observe_after_success` |

The recorder wrapper catches every internal recorder failure. It deliberately
has no return value, and owners never branch on it. `tests/test_prospective_owner_seams.py`
proves byte/return equality for accepted non-promoted signal and Conscious open
(the latter covers both open and snapshot hooks), plus exact correction and
attention seam behavior. The existing owner suites cover the remaining owner
operations; independent review should retain the matrix as the audit checklist.
