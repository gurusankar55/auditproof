# AuditProof — Test Plan (GenLayer Studio)

Deploy with `min_bond = 0`. Use any Studio account.

Replace the `<...>` values with a real, public audit report on the auditor's official site, and the token address shown on that page.

## A. Deterministic rejections (must fail before any web call)

| # | Call | Expected |
|---|---|---|
| A1 | `submit_claim("0x123", "evm", "certik", "https://skynet.certik.com/projects/x")` | Error: `invalid token address for chain` |
| A2 | `submit_claim(<valid 0x addr>, "evm", "fakeauditor", "https://x.com")` | Error: `auditor not supported` |
| A3 | `submit_claim(<valid 0x addr>, "evm", "certik", "https://certik-audit.xyz/report")` | Error: `report_url is not on the auditor's official domain` |
| A4 | `submit_claim(<valid 0x addr>, "evm", "certik", "http://skynet.certik.com/x")` | Error: `report_url must start with https://` |
| A5 | `submit_claim(<valid addr>, "evm", "solidproof", "https://github.com/someoneelse/report")` | Error: official domain (path-scoped source check) |
| A6 | `submit_claim(<valid addr>, "bitcoin", "certik", ...)` | Error: `invalid token address for chain` |

## B. Verdicts (validators run)

| # | Input | Expected `get_status` |
|---|---|---|
| B1 | Real report URL + the token address shown on it | `VERIFIED`; `is_verified` → `true` |
| B2 | Same real report URL + a **different** valid address | `ADDRESS_MISMATCH` |
| B3 | Auditor homepage, e.g. `https://www.certik.com/` + any address | `FAKE` |
| B4 | Non-existent page on the official domain, e.g. `https://skynet.certik.com/projects/does-not-exist-xyz123` | `UNREACHABLE` or `FAKE` |
| B5 | Real Hacken report URL but auditor = `certik` | Rejected at A-stage (domain), so it never reaches validators |

## C. Challenge and money flow

| # | Steps | Expected |
|---|---|---|
| C1 | Submit B1 (VERIFIED), then call `challenge(id)` with a bond | `SUBMITTER_WON`; status still `VERIFIED`; submitter's `get_credit` = both bonds |
| C2 | Call `challenge(id)` again on the same claim | Error: `already challenged or settled` |
| C3 | Submit a new claim and call `settle(id)` from the submitter account | Bond credited to submitter |
| C4 | Call `settle(id)` from a different account | Error: `only submitter can settle` |
| C5 | Call `withdraw()` with zero credit | Error: `nothing to withdraw` |

## D. Primitive behaviour

| # | Steps | Expected |
|---|---|---|
| D1 | `get_status("0x000...unused")` | `NO_CLAIM` |
| D2 | Submit B2 (mismatch) for a token, then submit B1 for the same token | `is_verified` → `true` (latest claim wins) |
| D3 | `is_verified` with an upper-case address | Same result (case-insensitive) |
| D4 | `get_supported_auditors()` | JSON of 7 auditors |

## What to look at in Studio

- In the transaction view, all validators should show **AGREE**, with the same JSON result.
- `get_claim(id)` → the `checks` field shows the four booleans that decided the verdict.
