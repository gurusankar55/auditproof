# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
import json

"""
AuditProof — on-chain verification of "this token is audited" claims.

Problem: meme coins and new tokens often claim "Audited by CertiK / Hacken / ..."
with a fake or borrowed report link. AuditProof lets anyone submit such a claim
with a bond. GenLayer validators each fetch the report page themselves and must
agree (strict equality) on a small, normalized verdict before it is stored.
Anyone can challenge a verdict with a bond; the claim is re-checked and the
loser's bond goes to the winner.

Other contracts / dApps can call `is_verified(token_address)` as a reusable
trust primitive (launchpads, wallets, listing bots).

Design rules:
  1. Deterministic checks first (auditor domain whitelist, address format).
     Bad input is rejected before any web/LLM call.
  2. Address matching is a literal string check on the fetched page — no LLM.
  3. The LLM only answers two yes/no questions, so validators converge.
  4. Consensus = gl.eq_principle.strict_eq over a canonical JSON string.
"""


# Auditor name -> allowed official domains / path prefixes.
# A report URL must live on one of these, checked deterministically.
AUDITOR_SOURCES = {
    "certik": ["certik.com", "skynet.certik.com"],
    "hacken": ["hacken.io", "audits.hacken.io"],
    "cyberscope": ["cyberscope.io"],
    "solidproof": ["solidproof.io", "github.com/solidproof"],
    "openzeppelin": ["openzeppelin.com", "blog.openzeppelin.com"],
    "trailofbits": ["trailofbits.com", "github.com/trailofbits"],
    "quantstamp": ["quantstamp.com", "certificate.quantstamp.com"],
}

STATUS_PENDING = "PENDING"
STATUS_VERIFIED = "VERIFIED"
STATUS_FAKE = "FAKE"                    # page is not an audit report by that auditor
STATUS_ADDRESS_MISMATCH = "ADDRESS_MISMATCH"  # real report, but for another contract
STATUS_UNREACHABLE = "UNREACHABLE"      # report page could not be fetched

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
HEX = "0123456789abcdefABCDEF"
MAX_PAGE_CHARS = 15000


# ---------------- deterministic helpers ----------------

def _valid_address(chain: str, addr: str) -> bool:
    if chain == "evm":
        return len(addr) == 42 and addr.startswith("0x") and all(c in HEX for c in addr[2:])
    if chain == "solana":
        return 32 <= len(addr) <= 44 and all(c in BASE58 for c in addr)
    return False


def _host_and_path(url: str) -> str:
    u = url.strip()
    if not u.startswith("https://"):
        raise Exception("report_url must start with https://")
    rest = u[len("https://"):]
    rest = rest.split("?")[0].split("#")[0]
    host = rest.split("/")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    path = rest[len(rest.split("/")[0]):]
    return host + path.lower()


def _url_belongs_to_auditor(auditor: str, url: str) -> bool:
    hp = _host_and_path(url)
    host = hp.split("/")[0]
    for src in AUDITOR_SOURCES.get(auditor, []):
        if "/" in src:
            # path-scoped source, e.g. github.com/solidproof
            if hp == src or hp.startswith(src + "/"):
                return True
        elif host == src or host.endswith("." + src):
            return True
    return False


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def _status_from(result: dict) -> str:
    if not result["reachable"]:
        return STATUS_UNREACHABLE
    if not (result["is_audit_report"] and result["auditor_match"]):
        return STATUS_FAKE
    if not result["address_in_page"]:
        return STATUS_ADDRESS_MISMATCH
    return STATUS_VERIFIED


# ---------------- contract ----------------

class AuditProof(gl.Contract):
    claims: TreeMap[str, str]          # claim_id -> claim JSON
    latest_by_token: TreeMap[str, str]  # lowercased token address -> claim_id
    credits: TreeMap[str, u256]         # address hex -> withdrawable amount
    claim_count: u256
    min_bond: u256

    def __init__(self, min_bond: int):
        self.claim_count = u256(0)
        self.min_bond = u256(min_bond)

    # ---- internal: the non-deterministic check, run by every validator ----
    def _check_report(self, auditor: str, token_address: str, report_url: str) -> dict:
        def run() -> str:
            try:
                page = gl.nondet.web.render(report_url, mode="text")
            except Exception:
                page = ""
            page = (page or "")[:MAX_PAGE_CHARS]

            if len(page.strip()) < 200:
                return json.dumps(
                    {"reachable": False, "is_audit_report": False,
                     "auditor_match": False, "address_in_page": False},
                    sort_keys=True,
                )

            # Literal, deterministic-given-page check. No LLM for addresses.
            address_in_page = token_address.lower() in page.lower()

            prompt = f"""You are checking a web page that is claimed to be a smart contract
security audit report written by the auditor "{auditor}".

Answer ONLY with JSON: {{"is_audit_report": true/false, "auditor_match": true/false}}

- is_audit_report: true only if the page is an actual security audit report or an
  official audit result page for a specific smart contract / project (findings,
  scope, severity, or an official audit certificate). Marketing pages, news,
  social posts, generic homepages, or "audit request" forms are false.
- auditor_match: true only if the page clearly shows the audit was performed by
  "{auditor}" (not a different firm).

PAGE TEXT:
{page}
"""
            out = gl.nondet.exec_prompt(prompt, response_format="json")
            if isinstance(out, str):
                out = json.loads(out)
            return json.dumps(
                {
                    "reachable": True,
                    "is_audit_report": _to_bool(out.get("is_audit_report", False)),
                    "auditor_match": _to_bool(out.get("auditor_match", False)),
                    "address_in_page": address_in_page,
                },
                sort_keys=True,
            )

        # All validators must produce the exact same canonical JSON.
        return json.loads(gl.eq_principle.strict_eq(run))

    def _load(self, claim_id: str) -> dict:
        if claim_id not in self.claims:
            raise Exception("unknown claim_id")
        return json.loads(self.claims[claim_id])

    def _save(self, claim_id: str, c: dict) -> None:
        self.claims[claim_id] = json.dumps(c, sort_keys=True)

    def _credit(self, who: str, amount: int) -> None:
        if amount <= 0:
            return
        cur = int(self.credits[who]) if who in self.credits else 0
        self.credits[who] = u256(cur + amount)

    # ---- write methods ----

    @gl.public.write.payable
    def submit_claim(self, token_address: str, chain: str, auditor: str, report_url: str) -> str:
        """Submit an audit claim with a bond and verify it immediately."""
        chain = chain.strip().lower()
        auditor = auditor.strip().lower()
        token_address = token_address.strip()

        if int(gl.message.value) < int(self.min_bond):
            raise Exception("bond below min_bond")
        if auditor not in AUDITOR_SOURCES:
            raise Exception("auditor not supported")
        if not _valid_address(chain, token_address):
            raise Exception("invalid token address for chain")
        if not _url_belongs_to_auditor(auditor, report_url):
            raise Exception("report_url is not on the auditor's official domain")

        result = self._check_report(auditor, token_address, report_url)
        status = _status_from(result)

        claim_id = str(int(self.claim_count))
        self.claim_count = u256(int(self.claim_count) + 1)
        self._save(claim_id, {
            "id": claim_id,
            "token_address": token_address,
            "chain": chain,
            "auditor": auditor,
            "report_url": report_url,
            "submitter": gl.message.sender_address.as_hex,
            "submitter_bond": int(gl.message.value),
            "status": status,
            "checks": result,
            "challenged": False,
            "challenger": "",
            "challenger_bond": 0,
            "settled": False,
        })
        self.latest_by_token[token_address.lower()] = claim_id
        return claim_id

    @gl.public.write.payable
    def challenge(self, claim_id: str) -> str:
        """Dispute a verdict. The report is re-fetched and re-judged by validators.
        If the verdict changes, the challenger wins both bonds; otherwise the
        submitter wins the challenger's bond. One challenge per claim."""
        c = self._load(claim_id)
        if c["challenged"] or c["settled"]:
            raise Exception("claim already challenged or settled")
        bond = int(gl.message.value)
        if bond < int(self.min_bond):
            raise Exception("bond below min_bond")

        result = self._check_report(c["auditor"], c["token_address"], c["report_url"])
        new_status = _status_from(result)
        challenger = gl.message.sender_address.as_hex
        pot = c["submitter_bond"] + bond

        c["challenged"] = True
        c["challenger"] = challenger
        c["challenger_bond"] = bond
        c["previous_status"] = c["status"]
        c["settled"] = True

        if new_status != c["status"]:
            c["status"] = new_status
            c["checks"] = result
            c["challenge_result"] = "CHALLENGER_WON"
            self._credit(challenger, pot)
        else:
            c["challenge_result"] = "SUBMITTER_WON"
            self._credit(c["submitter"], pot)

        self._save(claim_id, c)
        return c["challenge_result"]

    @gl.public.write
    def settle(self, claim_id: str) -> None:
        """Submitter ends an unchallenged claim and gets the bond back."""
        c = self._load(claim_id)
        if c["settled"]:
            raise Exception("already settled")
        if gl.message.sender_address.as_hex != c["submitter"]:
            raise Exception("only submitter can settle")
        c["settled"] = True
        self._credit(c["submitter"], c["submitter_bond"])
        self._save(claim_id, c)

    @gl.public.write
    def withdraw(self) -> int:
        who = gl.message.sender_address.as_hex
        amount = int(self.credits[who]) if who in self.credits else 0
        if amount == 0:
            raise Exception("nothing to withdraw")
        self.credits[who] = u256(0)
        gl.get_contract_at(gl.message.sender_address).emit_transfer(value=u256(amount))
        return amount

    # ---- view methods (the reusable primitive) ----

    @gl.public.view
    def is_verified(self, token_address: str) -> bool:
        key = token_address.strip().lower()
        if key not in self.latest_by_token:
            return False
        c = json.loads(self.claims[self.latest_by_token[key]])
        return c["status"] == STATUS_VERIFIED

    @gl.public.view
    def get_status(self, token_address: str) -> str:
        key = token_address.strip().lower()
        if key not in self.latest_by_token:
            return "NO_CLAIM"
        return json.loads(self.claims[self.latest_by_token[key]])["status"]

    @gl.public.view
    def get_claim(self, claim_id: str) -> str:
        if claim_id not in self.claims:
            return ""
        return self.claims[claim_id]

    @gl.public.view
    def get_credit(self, address_hex: str) -> int:
        return int(self.credits[address_hex]) if address_hex in self.credits else 0

    @gl.public.view
    def get_supported_auditors(self) -> str:
        return json.dumps(AUDITOR_SOURCES, sort_keys=True)

    @gl.public.view
    def get_claim_count(self) -> int:
        return int(self.claim_count)
