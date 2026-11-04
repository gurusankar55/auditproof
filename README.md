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

## Economic flow

```
submit_claim (bond) ──► verdict stored
        │
        ├── no challenge ──► submitter calls settle() ──► bond back to submitter
        │
        └── challenge (bond) ──► report re-checked by validators
                 ├── verdict changes ──► challenger gets both bonds
                 └── verdict same    ──► submitter gets both bonds
```

- One challenge per claim, which keeps the game bounded.
- Payouts are credited to an internal balance. Users pull funds with `withdraw()`, so the contract never pushes funds mid-logic.
- `min_bond` is set at deploy time and stops spam claims.

Why challenges matter: audit pages change. An auditor may later mark a project as "revoked" or remove the page. A challenge forces a fresh check by a new validator set.

---

## Methods

### Write
| Method | Description |
|---|---|
| `submit_claim(token_address, chain, auditor, report_url)` *payable* | Submit a claim with a bond; verified immediately. Returns `claim_id` |
| `challenge(claim_id)` *payable* | Dispute the verdict with a bond; re-checked and settled |
| `settle(claim_id)` | Submitter ends an unchallenged claim and receives the bond |
| `withdraw()` | Withdraw credited funds |

### View
| Method | Description |
|---|---|
| `is_verified(token_address)` | `true` only if the latest claim is `VERIFIED` |
| `get_status(token_address)` | Latest status, or `NO_CLAIM` |
| `get_claim(claim_id)` | Full claim JSON (checks, bonds, challenge result) |
| `get_credit(address_hex)` | Withdrawable balance |
| `get_supported_auditors()` | Auditor → official domains |
| `get_claim_count()` | Total claims |

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
- **Latest claim wins per token.** A re-submission or challenge updates what `is_verified` returns.
- **Pull payments.** This is a safer funds pattern.

## Limitations

- Only whitelisted auditors are supported. Adding an auditor means updating `AUDITOR_SOURCES` and redeploying.
- If an auditor's page loads its content only through heavy JavaScript, it may come back `UNREACHABLE`. In that case, use the auditor's PDF or GitHub report URL on their official domain.
- A `VERIFIED` result means the audit **exists**. It does not mean the token is safe. Audits can still list open issues.

## Files

```
contracts/audit_proof.py   # the Intelligent Contract
tests/TEST_PLAN.md         # manual test cases for GenLayer Studio
```

## Deploy (GenLayer Studio)

1. Open https://studio.genlayer.com
2. Create a new contract and paste `contracts/audit_proof.py`
3. Deploy with `min_bond` = `0` for testing, or a small value
4. Run the cases in `tests/TEST_PLAN.md`

License: MIT
