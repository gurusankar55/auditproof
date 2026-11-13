# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
import json
import datetime

"""
AuditProof v2 — on-chain verification of "this token is audited" claims.

Problem: meme coins and new tokens often claim "Audited by CertiK / Hacken / ..."
with a fake or borrowed report link. AuditProof lets anyone submit such a claim
with a bond. GenLayer validators each fetch the report page themselves and must
agree (strict equality) on a small, normalized verdict before it is stored.

Claim lifecycle (v2):

    submit_claim ──► PENDING_CHALLENGE  (now < challenge_deadline)
                        │   challenge() allowed, settle() rejected,
                        │   is_verified() = false
                        │
                        ├── challenge() inside the window ──► re-check, resolved,
                        │                                    final immediately
                        │
                        └── window ends (now >= challenge_deadline)
                               verdict becomes final, challenge() rejected,
                               settle() allowed (returns the submitter bond)

    Any final verdict ──► EXPIRED once now >= expires_at
                          (expires_at = checked_at + verdict_ttl)

  * A verdict only counts for is_verified() when it is FINAL and NOT EXPIRED.
  * settle() only returns the bond. It never changes or extends a verdict.
  * Expiry is computed from the clock in the view, so nobody has to call
    anything for a stale claim to stop being current.

Design rules:
  1. Deterministic checks first (auditor domain whitelist, address format).
     Bad input is rejected before any web/LLM call.
  2. Address matching is a literal string check on the fetched page — no LLM.
  3. The LLM only answers two yes/no questions, so validators converge.
  4. Consensus = gl.eq_principle.strict_eq over a canonical JSON string.
"""


# Auditor name -> allowed official domains / path prefixes.
AUDITOR_SOURCES = {
    "certik": ["certik.com", "skynet.certik.com"],
    "hacken": ["hacken.io", "audits.hacken.io"],
    "cyberscope": ["cyberscope.io"],
    "solidproof": ["solidproof.io", "github.com/solidproof"],
    "openzeppelin": ["openzeppelin.com", "blog.openzeppelin.com"],
    "trailofbits": ["trailofbits.com", "github.com/trailofbits"],
    "quantstamp": ["quantstamp.com", "certificate.quantstamp.com"],
}

# Stored verdicts
STATUS_VERIFIED = "VERIFIED"
STATUS_FAKE = "FAKE"                          # page is not an audit report by that auditor
STATUS_ADDRESS_MISMATCH = "ADDRESS_MISMATCH"  # real report, but for another contract
STATUS_UNREACHABLE = "UNREACHABLE"            # report page could not be fetched

# Current-view states (computed, never stored)
VIEW_NO_CLAIM = "NO_CLAIM"
VIEW_PENDING = "PENDING_CHALLENGE"
VIEW_EXPIRED = "EXPIRED"
VIEW_TIME_UNAVAILABLE = "TIME_UNAVAILABLE"

# Challenge outcomes
RESULT_CHALLENGER_WON = "CHALLENGER_WON"
RESULT_SUBMITTER_WON = "SUBMITTER_WON"
RESULT_INCONCLUSIVE = "INCONCLUSIVE"   # page unreachable on re-check: both bonds refunded

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
HEX = "0123456789abcdefABCDEF"
MAX_PAGE_CHARS = 15000

MIN_WINDOW_SECONDS = 60                 # a real window: at least 1 minute
MAX_WINDOW_SECONDS = 30 * 24 * 3600     # at most 30 days
MAX_TTL_SECONDS = 365 * 24 * 3600       # a verdict is current for at most 1 year
MAX_CLAIMS_PER_TOKEN = 20               # bounded scan in the views
MIN_TIME = 1600000000                   # clock sanity check (Sep 2020)


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


def _read_clock() -> int:
    """Transaction time in unix seconds (deterministic inside GenVM).
    Returns 0 if no usable time can be read."""
    s = None
    try:
        raw = gl.message_raw
        if "datetime" in raw:
            s = raw["datetime"]
    except Exception:
        s = None
    t = 0
    if s:
        try:
            if isinstance(s, str):
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                dt = s
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            t = int(dt.timestamp())
        except Exception:
            t = 0
    if t < MIN_TIME:
        try:
            t = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        except Exception:
            t = 0
    return t if t >= MIN_TIME else 0


def _now_strict() -> int:
    """Clock for write methods: a write never runs without a valid time."""
    t = _read_clock()
    if t == 0:
        raise Exception("could not read the transaction time")
    return t


def _is_final(c: dict, now: int) -> bool:
    """A verdict is final after a challenge was resolved, or once the
    challenge window has closed."""
    return bool(c["challenged"]) or now >= int(c["challenge_deadline"])


def _current_status(c: dict, now: int) -> str:
    """What this claim means *right now* (the current verification view)."""
    if now <= 0:
        return VIEW_TIME_UNAVAILABLE
    if now >= int(c["expires_at"]):
        return VIEW_EXPIRED
    if not _is_final(c, now):
        return VIEW_PENDING
    return c["status"]


# ---------------- contract ----------------

class AuditProof(gl.Contract):
    claims: TreeMap[str, str]          # claim_id -> claim JSON
    token_claims: TreeMap[str, str]    # lowercased token address -> JSON list of claim_ids (newest last)
    credits: TreeMap[str, u256]        # address hex -> withdrawable amount
    claim_count: u256
    min_bond: u256
    challenge_window: u256             # seconds
    verdict_ttl: u256                  # seconds a verdict stays current

    def __init__(self, min_bond: int, challenge_window_seconds: int, verdict_ttl_seconds: int):
        if min_bond < 0:
            raise Exception("min_bond must be >= 0")
        if challenge_window_seconds < MIN_WINDOW_SECONDS or challenge_window_seconds > MAX_WINDOW_SECONDS:
            raise Exception("challenge_window_seconds must be between 60 and 2592000")
        if verdict_ttl_seconds <= challenge_window_seconds or verdict_ttl_seconds > MAX_TTL_SECONDS:
            raise Exception("verdict_ttl_seconds must be greater than the challenge window and at most 31536000")
        self.claim_count = u256(0)
        self.min_bond = u256(min_bond)
        self.challenge_window = u256(challenge_window_seconds)
        self.verdict_ttl = u256(verdict_ttl_seconds)

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

    def _ids_for(self, token_address: str) -> list:
        key = token_address.strip().lower()
        if key not in self.token_claims:
            return []
        return json.loads(self.token_claims[key])

    def _token_view(self, token_address: str, now: int) -> str:
        """Current status for a token.
        VERIFIED if ANY of its recent claims is VERIFIED, final and not expired
        (a later junk claim cannot hide a valid report). Otherwise the current
        status of the newest claim."""
        ids = self._ids_for(token_address)
        if len(ids) == 0:
            return VIEW_NO_CLAIM
        if now <= 0:
            return VIEW_TIME_UNAVAILABLE
        for cid in ids:
            if _current_status(json.loads(self.claims[cid]), now) == STATUS_VERIFIED:
                return STATUS_VERIFIED
        return _current_status(json.loads(self.claims[ids[-1]]), now)

    # ---- write methods ----

    @gl.public.write.payable
    def submit_claim(self, token_address: str, chain: str, auditor: str, report_url: str) -> str:
        """Submit an audit claim with a bond. Validators check it now; the
        verdict is provisional until the challenge window closes."""
        chain = chain.strip().lower()
        auditor = auditor.strip().lower()
        token_address = token_address.strip()
        report_url = report_url.strip()

        if int(gl.message.value) < int(self.min_bond):
            raise Exception("bond below min_bond")
        if auditor not in AUDITOR_SOURCES:
            raise Exception("auditor not supported")
        if not _valid_address(chain, token_address):
            raise Exception("invalid token address for chain")
        if not _url_belongs_to_auditor(auditor, report_url):
            raise Exception("report_url is not on the auditor's official domain")

        now = _now_strict()
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
            "submitted_at": now,
            "checked_at": now,
            "challenge_deadline": now + int(self.challenge_window),
            "expires_at": now + int(self.verdict_ttl),
            "challenged": False,
            "challenger": "",
            "challenger_bond": 0,
            "settled": False,
        })

        key = token_address.lower()
        ids = self._ids_for(key)
        ids.append(claim_id)
        if len(ids) > MAX_CLAIMS_PER_TOKEN:
            ids = ids[len(ids) - MAX_CLAIMS_PER_TOKEN:]
        self.token_claims[key] = json.dumps(ids)
        return claim_id

    @gl.public.write.payable
    def challenge(self, claim_id: str) -> str:
        """Dispute a verdict while the challenge window is open.
        The report is re-fetched and re-judged by validators.
          - verdict changes        -> challenger wins both bonds
          - verdict unchanged      -> submitter wins both bonds
          - page now unreachable   -> INCONCLUSIVE: both bonds refunded and the
                                      claim becomes UNREACHABLE (not verified)
        One challenge per claim. The re-checked verdict is final and gets a
        fresh expiry."""
        c = self._load(claim_id)
        if c["challenged"]:
            raise Exception("claim already challenged")
        if c["settled"]:
            raise Exception("claim already settled")
        now = _now_strict()
        if now >= int(c["challenge_deadline"]):
            raise Exception("challenge window has closed")
        if now >= int(c["expires_at"]):
            raise Exception("claim has expired")
        bond = int(gl.message.value)
        if bond < int(self.min_bond):
            raise Exception("bond below min_bond")
        challenger = gl.message.sender_address.as_hex
        if challenger == c["submitter"]:
            raise Exception("submitter cannot challenge own claim")

        result = self._check_report(c["auditor"], c["token_address"], c["report_url"])
        new_status = _status_from(result)

        c["challenged"] = True
        c["challenger"] = challenger
        c["challenger_bond"] = bond
        c["challenged_at"] = now
        c["previous_status"] = c["status"]
        c["checks"] = result
        c["checked_at"] = now
        c["expires_at"] = now + int(self.verdict_ttl)
        c["settled"] = True   # bonds are paid out right here

        if new_status == STATUS_UNREACHABLE and c["status"] != STATUS_UNREACHABLE:
            c["status"] = STATUS_UNREACHABLE
            c["challenge_result"] = RESULT_INCONCLUSIVE
            self._credit(c["submitter"], int(c["submitter_bond"]))
            self._credit(challenger, bond)
        elif new_status != c["status"]:
            c["status"] = new_status
            c["challenge_result"] = RESULT_CHALLENGER_WON
            self._credit(challenger, int(c["submitter_bond"]) + bond)
        else:
            c["challenge_result"] = RESULT_SUBMITTER_WON
            self._credit(c["submitter"], int(c["submitter_bond"]) + bond)

        self._save(claim_id, c)
        return c["challenge_result"]

    @gl.public.write
    def settle(self, claim_id: str) -> str:
        """Return the submitter's bond for an unchallenged claim, but ONLY after
        the challenge window has closed. Anyone may call it; the bond always
        goes to the submitter. Settling does not change or extend the verdict:
        it stays current until expires_at."""
        c = self._load(claim_id)
        if c["settled"]:
            raise Exception("claim already settled")
        now = _now_strict()
        if now < int(c["challenge_deadline"]):
            raise Exception("challenge window still open")
        c["settled"] = True
        c["settled_at"] = now
        self._credit(c["submitter"], int(c["submitter_bond"]))
        self._save(claim_id, c)
        return c["status"]

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
        """True only if the token has a VERIFIED verdict that is final
        (challenge window closed or challenge resolved) and not expired."""
        return self._token_view(token_address, _read_clock()) == STATUS_VERIFIED

    @gl.public.view
    def get_status(self, token_address: str) -> str:
        """NO_CLAIM, PENDING_CHALLENGE, VERIFIED, FAKE, ADDRESS_MISMATCH,
        UNREACHABLE or EXPIRED — computed for the current time."""
        return self._token_view(token_address, _read_clock())

    @gl.public.view
    def is_verified_at(self, token_address: str, unix_time: int) -> bool:
        """Same as is_verified, but for a time the caller supplies."""
        return self._token_view(token_address, int(unix_time)) == STATUS_VERIFIED

    @gl.public.view
    def get_status_at(self, token_address: str, unix_time: int) -> str:
        return self._token_view(token_address, int(unix_time))

    @gl.public.view
    def get_claim(self, claim_id: str) -> str:
        """Full claim JSON plus its current lifecycle state."""
        if claim_id not in self.claims:
            return ""
        c = json.loads(self.claims[claim_id])
        now = _read_clock()
        c["now"] = now
        c["current_status"] = _current_status(c, now)
        c["can_challenge"] = (now > 0 and not c["challenged"] and not c["settled"]
                              and now < int(c["challenge_deadline"]) and now < int(c["expires_at"]))
        c["can_settle"] = (now > 0 and not c["settled"] and now >= int(c["challenge_deadline"]))
        return json.dumps(c, sort_keys=True)

    @gl.public.view
    def get_token_claims(self, token_address: str) -> str:
        return json.dumps(self._ids_for(token_address))

    @gl.public.view
    def get_credit(self, address_hex: str) -> int:
        return int(self.credits[address_hex]) if address_hex in self.credits else 0

    @gl.public.view
    def get_supported_auditors(self) -> str:
        return json.dumps(AUDITOR_SOURCES, sort_keys=True)

    @gl.public.view
    def get_claim_count(self) -> int:
        return int(self.claim_count)

    @gl.public.view
    def get_config(self) -> str:
        return json.dumps({
            "min_bond": int(self.min_bond),
            "challenge_window_seconds": int(self.challenge_window),
            "verdict_ttl_seconds": int(self.verdict_ttl),
            "max_claims_per_token": MAX_CLAIMS_PER_TOKEN,
        }, sort_keys=True)
