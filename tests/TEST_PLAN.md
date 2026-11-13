# AuditProof v2 — Tests

## 1. Offline unit tests (no Docker, no node)

```
python -m unittest tests/test_audit_proof.py -v
```

30 tests. The real `contracts/audit_proof.py` is imported unchanged with a small `genlayer` stub; the transaction clock, web pages and LLM replies are mocked. Key cases the steward asked for:

| Requirement | Test |
|---|---|
| Immediate settlement fails | `SettlementTests.test_immediate_settle_fails`, `test_settle_one_second_before_deadline_fails` |
| A failed early settle does not block a challenge | `SettlementTests.test_failed_early_settle_does_not_block_challenge` |
| A timely challenge remains possible | `ChallengeTests.test_challenge_inside_window_changes_wrong_verdict`, `test_challenge_confirms_correct_verdict` |
| Challenge after the window fails | `ChallengeTests.test_challenge_after_window_fails` |
| Settlement only after the window | `SettlementTests.test_settle_after_window_pays_bond` |
| Provisional verdict is not "verified" | `ViewTests.test_pending_during_window` |
| Stale claims stop being current | `ViewTests.test_stale_claim_expires`, `test_settle_does_not_extend_expiry` |
| Re-check gives a fresh expiry | `ViewTests.test_challenge_gives_fresh_expiry` |

## 2. GenLayer Studio (deployed contract)

Deploy `contracts/audit_proof.py` with:

- `min_bond` = `0`
- `challenge_window_seconds` = `600` (10 minutes — Studio transactions take 1–2 minutes each, so a shorter window is hard to test by hand)
- `verdict_ttl_seconds` = `900` (15 minutes)

Use a real public audit report on the auditor's official site as `<URL>`, and the token address shown on that page as `<TOKEN>`.

### A. Deterministic rejections

| # | Call | Expected |
|---|---|---|
| A1 | `submit_claim("0x123", "evm", "certik", "https://skynet.certik.com/projects/x")` | `invalid token address for chain` |
| A2 | `submit_claim(<TOKEN>, "evm", "fakeauditor", "https://x.com")` | `auditor not supported` |
| A3 | `submit_claim(<TOKEN>, "evm", "certik", "https://certik-audit.xyz/report")` | `report_url is not on the auditor's official domain` |
| A4 | `submit_claim(<TOKEN>, "evm", "certik", "http://skynet.certik.com/x")` | `report_url must start with https://` |

### B. Lifecycle (account 1 = submitter, account 2 = challenger)

| # | Steps | Expected |
|---|---|---|
| B1 | Account 1: `submit_claim(<TOKEN>, "evm", "certik", <URL>)` | Returns `"0"` |
| B2 | Right away, account 1: `settle("0")` | Error `challenge window still open` |
| B3 | Right away: `get_status(<TOKEN>)` and `is_verified(<TOKEN>)` | `PENDING_CHALLENGE`, `false` |
| B4 | Within 10 minutes, account 2: `challenge("0")` | `SUBMITTER_WON` (challenge still possible after the failed settle) |
| B5 | `get_status(<TOKEN>)`, `is_verified(<TOKEN>)` | `VERIFIED`, `true` |
| B6 | Account 1: `submit_claim(<TOKEN>, "evm", "certik", <URL>)` | Returns `"1"` |
| B7 | Wait 10+ minutes. Account 2: `challenge("1")` | Error `challenge window has closed` |
| B8 | Account 1: `settle("1")` | Succeeds. `get_credit(<account 1>)` includes the bond |
| B9 | Wait until 15 minutes after the last check of the claim (submit, or challenge re-check). `get_status(<TOKEN>)`, `is_verified(<TOKEN>)` | `EXPIRED`, `false` |
| B10 | `get_claim("1")` | Shows `challenge_deadline`, `expires_at`, `current_status`, `can_challenge`, `can_settle` |

### C. Other views

`get_status("0x0000000000000000000000000000000000000001")` → `NO_CLAIM`; `get_config()` shows the 3 deploy values; `get_supported_auditors()` → 7 auditors.

In each transaction view, the validators should show **AGREE**.
