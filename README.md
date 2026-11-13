# AuditProof — GenLayer Intelligent Contract

**Verifies "this token is audited" claims on-chain.**

Many meme coins and new tokens claim "Audited by CertiK / Hacken" and link a report that is fake, belongs to another project, or is just a marketing page. AuditProof lets anyone submit an audit claim with a bond. GenLayer validators each open the report page themselves and must reach the **same** verdict before it is stored. Anyone can challenge a verdict with a bond. The report is then re-checked, and the loser's bond goes to the winner.

Other builders can call one view method as a trust primitive:

```python
is_verified(token_address) -> bool
```

Launchpads, wallets, listing bots and scam checkers can use it before showing an "Audited" badge.

---

## Verdicts

| Status | Meaning |
|---|---|
| `VERIFIED` | Real audit report by the named auditor, and the token address appears on the page |
| `ADDRESS_MISMATCH` | Real audit report, but for a different contract (a borrowed report) |
| `FAKE` | Page is not an audit report, or was written by a different firm |
| `UNREACHABLE` | Report page could not be loaded |

---

## How consensus is used

Each check runs three layers, from cheapest and safest to most expensive.

**1. Deterministic checks (no web, no LLM).** These run before any validator work. Invalid input is rejected immediately.
- The auditor must be in the whitelist (`AUDITOR_SOURCES`).
- `report_url` must be `https://` and on that auditor's **official domain**. A report on a random site is rejected outright, so a scammer cannot host a fake "CertiK" page elsewhere.
- The token address must match its chain format (EVM `0x` + 40 hex characters, or Solana base58).

**2. Literal address check (no LLM).** Each validator fetches the page with `gl.nondet.web.render` and checks whether the token address string appears on it (case-insensitive). The LLM never decides this part, so it cannot hallucinate it.

**3. Narrow LLM judgement.** The LLM answers only two yes/no questions:
- `is_audit_report`: is this a real audit report or certificate, rather than marketing, news or a request form?
- `auditor_match`: was it written by the named auditor?

**Equivalence principle.** Each validator returns a canonical JSON string with sorted keys and booleans only:

```json
{"address_in_page": true, "auditor_match": true, "is_audit_report": true, "reachable": true}
```

`gl.eq_principle.strict_eq` requires every validator to produce exactly the same string. Because the output is just four booleans, honest validators converge. Free-text LLM output would rarely match exactly, and a lying leader would be caught.

The final status is derived **deterministically** from those four booleans (`_status_from`).

---

## Claim lifecycle (v2)

```
submit_claim (bond) ──► validators check now ──► PENDING_CHALLENGE
                                                   │  (now < challenge_deadline)
                                                   │  challenge() allowed
                                                   │  settle() rejected
                                                   │  is_verified() = false
        ┌──────────────────────────────────────────┤
        │                                          │
 challenge (bond) inside the window         window closes
 report re-checked by validators            verdict is FINAL
   ├── verdict changes   ─► challenger gets both bonds      challenge() rejected
   ├── verdict same      ─► submitter gets both bonds       settle() allowed ─► bond back
   └── page unreachable  ─► INCONCLUSIVE: both refunded,
                            claim becomes UNREACHABLE
        │                                          │
        └──────────── final verdict ───────────────┘
                           │
              now >= expires_at ─► EXPIRED (no longer current)
```

Rules, enforced in code:

- `challenge_deadline = submitted_at + challenge_window_seconds`. `challenge()` works only while `now < challenge_deadline`. `settle()` works only when `now >= challenge_deadline`. **A claim can never be settled before it could have been challenged.**
- `expires_at = checked_at + verdict_ttl_seconds`. A re-check during a challenge resets `checked_at`.
- **What counts as current:** `is_verified()` is `true` only for a `VERIFIED` verdict that is **final** (window closed or challenge resolved) and **not expired**. During the window, `get_status()` returns `PENDING_CHALLENGE`.
- **Settled claims:** `settle()` only returns the bond. It does not change or extend the verdict. A settled claim stays current until `expires_at`, then shows `EXPIRED`.
- **Expired claims:** expiry is computed from the transaction clock inside the view, so nobody has to call anything for a stale claim to stop counting. To renew, submit the claim again; validators re-check the report.
- A token is `VERIFIED` if **any** of its last 20 claims is VERIFIED, final and not expired, so a later junk claim cannot hide a valid report. Otherwise the newest claim's current status is shown.
- One challenge per claim. The submitter cannot challenge their own claim.
- Payouts go to an internal balance; users pull funds with `withdraw()`.
- Deploy parameters: `min_bond`, `challenge_window_seconds` (60 s – 30 days), `verdict_ttl_seconds` (> window, ≤ 1 year).

Why challenges matter: audit pages change. An auditor may later mark a project as "revoked" or remove the page. A challenge forces a fresh check by a new validator set.

---

## Methods

### Write
| Method | Description |
|---|---|
| `submit_claim(token_address, chain, auditor, report_url)` *payable* | Submit a claim with a bond; checked now, provisional until the window closes. Returns `claim_id` |
| `challenge(claim_id)` *payable* | Dispute the verdict **inside the challenge window**; re-checked and resolved |
| `settle(claim_id)` | **After the window**, return the submitter's bond (anyone may call) |
| `withdraw()` | Withdraw credited funds |

### View
| Method | Description |
|---|---|
| `is_verified(token_address)` | `true` only for a final, unexpired `VERIFIED` verdict |
| `get_status(token_address)` | `NO_CLAIM`, `PENDING_CHALLENGE`, `VERIFIED`, `FAKE`, `ADDRESS_MISMATCH`, `UNREACHABLE` or `EXPIRED` |
| `is_verified_at(token_address, unix_time)` / `get_status_at(...)` | Same, for a caller-supplied time |
| `get_claim(claim_id)` | Full claim JSON plus `current_status`, `can_challenge`, `can_settle` |
| `get_token_claims(token_address)` | Claim ids for a token |
| `get_credit(address_hex)` | Withdrawable balance |
| `get_supported_auditors()` | Auditor → official domains |
| `get_claim_count()` / `get_config()` | Totals and deploy settings |

`chain` is `"evm"` or `"solana"`. `auditor` is one of: `certik`, `hacken`, `cyberscope`, `solidproof`, `openzeppelin`, `trailofbits`, `quantstamp`.

---

## Using it from another contract

```python
audit = gl.get_contract_at(Address(AUDITPROOF_ADDRESS))
if audit.view().is_verified(token_address):
    # show "Audited" badge, allow listing, lower risk score, etc.
    ...
```

---

## Design decisions

- **Domain whitelist is deterministic.** This is the strongest anti-scam rule, and it needs no AI.
- **Address check is literal.** The LLM is not trusted with exact strings.
- **LLM output is reduced to booleans.** This keeps `strict_eq` practical.
- **Page text is capped at 15k characters.** This bounds prompt size and cost.
- **Verdicts are provisional, then final, then expire.** See the claim lifecycle above.
- **Pull payments.** This is a safer funds pattern.

## Limitations

- Only whitelisted auditors are supported. Adding an auditor means updating `AUDITOR_SOURCES` and redeploying.
- If an auditor's page loads its content only through heavy JavaScript, it may come back `UNREACHABLE`. In that case, use the auditor's PDF or GitHub report URL on their official domain.
- A `VERIFIED` result means the audit **exists**. It does not mean the token is safe. Audits can still list open issues.

## Files

```
contracts/audit_proof.py      # the Intelligent Contract (deployed source)
tests/test_audit_proof.py     # offline unit tests: python -m unittest tests/test_audit_proof.py -v
tests/TEST_PLAN.md            # test map + manual Studio test cases
evidence/                     # Studio screenshots of the lifecycle test
```

## Deployment

Deployed v2 contract (GenLayer Studio): [`0x22b7eF48f35AcC552391A3bA0633326017c4d40B`](https://explorer-studio.genlayer.com/address/0x22b7eF48f35AcC552391A3bA0633326017c4d40B) (deployed with `min_bond=0`, `challenge_window_seconds=600`, `verdict_ttl_seconds=900` for the Studio lifecycle test)

The deployed contract is exactly `contracts/audit_proof.py` in this repo.

### Studio lifecycle evidence (contract `0x22b7eF48…d40B`, window 600 s, TTL 900 s)

| Step | Call | Result |
|---|---|---|
| 1 | `submit_claim("0xdAC17F958D2ee523a2206206994597C13D831ec7", "evm", "certik", "https://www.certik.com/")` | `"0"` — verdict `FAKE` (homepage, not a report), validators agree |
| 2 | `settle("0")` immediately after submit | **Error `challenge window still open`** — validators agree |
| 3 | `challenge("0")` from a second account, inside the window | `"SUBMITTER_WON"` — re-check gave the same verdict |
| 4 | `get_status(token)` after the challenge | `"FAKE"` (final) |
| 5 | `get_status(token)` 15 minutes after the re-check | **`"EXPIRED"`** |

Screenshots are in [`evidence/`](evidence/). The earlier test deploy `0x0C40Ec26…2fb0` (window 120 s) also showed `settle("0")` succeeding after the window closed.

## Deploy (GenLayer Studio)

1. Open https://studio.genlayer.com
2. Create a new contract and paste `contracts/audit_proof.py`
3. Deploy with `min_bond` = `0`, `challenge_window_seconds` = `600`, `verdict_ttl_seconds` = `900` for testing (production example: `3600` / `2592000`)
4. Run the cases in `tests/TEST_PLAN.md`

License: MIT
