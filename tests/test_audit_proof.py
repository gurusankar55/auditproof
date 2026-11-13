"""
Offline unit tests for AuditProof v2 (claim lifecycle).

Plain Python, no Docker, no GenLayer node. A tiny stub of the `genlayer`
module is installed first, so the real contract file is imported unchanged.
Web pages and LLM replies are mocked, and the transaction clock is set per
call, so the challenge window and expiry rules are tested exactly.

Run from the repo root:
    python -m unittest tests/test_audit_proof.py -v
"""

import os
import sys
import json
import types
import unittest
import importlib.util
from datetime import datetime, timezone, timedelta


# ---------------- genlayer stub ----------------

class _Addr:
    def __init__(self, hex_):
        self.as_hex = hex_


class _WriteDecorator:
    def __call__(self, fn):
        return fn

    def payable(self, fn):
        return fn


TRANSFERS = []


class _ContractHandle:
    def __init__(self, addr):
        self.addr = addr

    def emit_transfer(self, value):
        TRANSFERS.append((self.addr.as_hex, int(value)))


gl = types.SimpleNamespace()
gl.Contract = object
gl.public = types.SimpleNamespace(write=_WriteDecorator(), view=lambda f: f)
gl.message = types.SimpleNamespace(sender_address=_Addr("0xSUB"), value=0)
gl.message_raw = {"datetime": "2026-11-14T10:00:00Z"}
gl.nondet = types.SimpleNamespace(web=types.SimpleNamespace(render=None), exec_prompt=None)
gl.eq_principle = types.SimpleNamespace(strict_eq=lambda fn: fn())
gl.get_contract_at = lambda addr: _ContractHandle(addr)

stub = types.ModuleType("genlayer")
stub.gl = gl
stub.TreeMap = dict
stub.u256 = int
stub.__all__ = ["gl", "TreeMap", "u256"]
sys.modules["genlayer"] = stub

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT = os.path.join(HERE, "..", "contracts", "audit_proof.py")
spec = importlib.util.spec_from_file_location("audit_proof", CONTRACT)
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)


# ---------------- helpers ----------------

T0 = datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc)
WINDOW = 3600          # 1 hour challenge window
TTL = 7 * 24 * 3600    # verdict current for 7 days
BOND = 10

TOKEN = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
URL = "https://skynet.certik.com/projects/demo-token"
URL2 = "https://skynet.certik.com/projects/demo-token-v2"

PAGES = {}     # url -> page text (missing = fetch fails)
LLM = {}       # url -> {"is_audit_report": bool, "auditor_match": bool}


def fake_render(url, mode="text"):
    if url not in PAGES:
        raise Exception("fetch failed")
    return PAGES[url]


def fake_prompt(prompt, response_format="json"):
    for url, page in PAGES.items():
        if page and page in prompt:
            return json.dumps(LLM.get(url, {"is_audit_report": False, "auditor_match": False}))
    return json.dumps({"is_audit_report": False, "auditor_match": False})


gl.nondet.web.render = fake_render
gl.nondet.exec_prompt = fake_prompt


def at(seconds_after_t0):
    t = T0 + timedelta(seconds=seconds_after_t0)
    gl.message_raw = {"datetime": t.strftime("%Y-%m-%dT%H:%M:%SZ")}


def as_user(addr, value=0):
    gl.message.sender_address = _Addr(addr)
    gl.message.value = value


def real_report(url, address):
    PAGES[url] = ("CertiK Security Assessment. Audit scope: " + address + ". "
                  + "Findings and severity table. " * 20)
    LLM[url] = {"is_audit_report": True, "auditor_match": True}


def new_contract(min_bond=BOND, window=WINDOW, ttl=TTL):
    c = ap.AuditProof.__new__(ap.AuditProof)
    c.claims = {}
    c.token_claims = {}
    c.credits = {}
    ap.AuditProof.__init__(c, min_bond, window, ttl)
    return c


def submit(c, token=TOKEN, url=URL, t=0, who="0xSUB", bond=BOND):
    at(t)
    as_user(who, bond)
    return c.submit_claim(token, "evm", "certik", url)


class Base(unittest.TestCase):
    def setUp(self):
        PAGES.clear()
        LLM.clear()
        TRANSFERS.clear()
        real_report(URL, TOKEN)
        self.c = new_contract()


# ---------------- constructor ----------------

class ConstructorTests(unittest.TestCase):
    def test_window_too_short_rejected(self):
        with self.assertRaises(Exception):
            new_contract(window=0)
        with self.assertRaises(Exception):
            new_contract(window=59)

    def test_ttl_must_exceed_window(self):
        with self.assertRaises(Exception):
            new_contract(window=3600, ttl=3600)

    def test_negative_bond_rejected(self):
        with self.assertRaises(Exception):
            new_contract(min_bond=-1)


# ---------------- 1. immediate settlement fails ----------------

class SettlementTests(Base):
    def test_immediate_settle_fails(self):
        cid = submit(self.c, t=0)
        at(0)
        as_user("0xSUB")
        with self.assertRaisesRegex(Exception, "challenge window still open"):
            self.c.settle(cid)

    def test_settle_one_second_before_deadline_fails(self):
        cid = submit(self.c, t=0)
        at(WINDOW - 1)
        as_user("0xSUB")
        with self.assertRaisesRegex(Exception, "challenge window still open"):
            self.c.settle(cid)

    def test_settle_after_window_pays_bond(self):
        cid = submit(self.c, t=0)
        at(WINDOW)
        as_user("0xANY")   # anyone may trigger; bond goes to submitter
        self.assertEqual(self.c.settle(cid), "VERIFIED")
        self.assertEqual(self.c.get_credit("0xSUB"), BOND)
        self.assertEqual(self.c.get_credit("0xANY"), 0)

    def test_settle_twice_fails(self):
        cid = submit(self.c, t=0)
        at(WINDOW + 5)
        self.c.settle(cid)
        with self.assertRaisesRegex(Exception, "already settled"):
            self.c.settle(cid)

    def test_failed_early_settle_does_not_block_challenge(self):
        cid = submit(self.c, t=0)
        at(10)
        with self.assertRaises(Exception):
            self.c.settle(cid)
        at(20)
        as_user("0xCH", BOND)
        self.assertEqual(self.c.challenge(cid), "SUBMITTER_WON")


# ---------------- 2. timely challenge remains possible ----------------

class ChallengeTests(Base):
    def test_challenge_inside_window_changes_wrong_verdict(self):
        # Page looked like a real report at submit time ...
        cid = submit(self.c, t=0)
        self.assertEqual(json.loads(self.c.get_claim(cid))["status"], "VERIFIED")
        # ... but validators now find it is a marketing page.
        LLM[URL] = {"is_audit_report": False, "auditor_match": True}
        at(WINDOW - 1)
        as_user("0xCH", BOND)
        self.assertEqual(self.c.challenge(cid), "CHALLENGER_WON")
        self.assertEqual(self.c.get_credit("0xCH"), 2 * BOND)
        at(WINDOW - 1)
        self.assertEqual(self.c.get_status(TOKEN), "FAKE")
        self.assertFalse(self.c.is_verified(TOKEN))

    def test_challenge_confirms_correct_verdict(self):
        cid = submit(self.c, t=0)
        at(60)
        as_user("0xCH", BOND)
        self.assertEqual(self.c.challenge(cid), "SUBMITTER_WON")
        self.assertEqual(self.c.get_credit("0xSUB"), 2 * BOND)
        # Resolved challenge = final, even though the window is still running.
        at(61)
        self.assertTrue(self.c.is_verified(TOKEN))

    def test_challenge_after_window_fails(self):
        cid = submit(self.c, t=0)
        at(WINDOW)
        as_user("0xCH", BOND)
        with self.assertRaisesRegex(Exception, "challenge window has closed"):
            self.c.challenge(cid)

    def test_challenge_after_settle_fails(self):
        cid = submit(self.c, t=0)
        at(WINDOW + 1)
        self.c.settle(cid)
        as_user("0xCH", BOND)
        with self.assertRaises(Exception):
            self.c.challenge(cid)

    def test_second_challenge_fails(self):
        cid = submit(self.c, t=0)
        at(30)
        as_user("0xCH", BOND)
        self.c.challenge(cid)
        as_user("0xCH2", BOND)
        with self.assertRaisesRegex(Exception, "already challenged"):
            self.c.challenge(cid)

    def test_challenge_bond_too_low(self):
        cid = submit(self.c, t=0)
        at(30)
        as_user("0xCH", BOND - 1)
        with self.assertRaisesRegex(Exception, "bond below min_bond"):
            self.c.challenge(cid)

    def test_submitter_cannot_self_challenge(self):
        cid = submit(self.c, t=0)
        at(30)
        as_user("0xSUB", BOND)
        with self.assertRaisesRegex(Exception, "submitter cannot challenge"):
            self.c.challenge(cid)

    def test_unreachable_on_recheck_is_inconclusive(self):
        cid = submit(self.c, t=0)
        del PAGES[URL]          # page removed after submit
        at(100)
        as_user("0xCH", BOND)
        self.assertEqual(self.c.challenge(cid), "INCONCLUSIVE")
        self.assertEqual(self.c.get_credit("0xSUB"), BOND)
        self.assertEqual(self.c.get_credit("0xCH"), BOND)
        at(101)
        self.assertFalse(self.c.is_verified(TOKEN))
        self.assertEqual(self.c.get_status(TOKEN), "UNREACHABLE")

    def test_settle_after_challenge_fails(self):
        cid = submit(self.c, t=0)
        at(30)
        as_user("0xCH", BOND)
        self.c.challenge(cid)
        at(WINDOW + 1)
        with self.assertRaisesRegex(Exception, "already settled"):
            self.c.settle(cid)


# ---------------- 3. current view: pending, final, expired ----------------

class ViewTests(Base):
    def test_pending_during_window(self):
        cid = submit(self.c, t=0)
        at(1)
        self.assertEqual(self.c.get_status(TOKEN), "PENDING_CHALLENGE")
        self.assertFalse(self.c.is_verified(TOKEN))
        claim = json.loads(self.c.get_claim(cid))
        self.assertTrue(claim["can_challenge"])
        self.assertFalse(claim["can_settle"])

    def test_verified_after_window_without_settle(self):
        cid = submit(self.c, t=0)
        at(WINDOW)
        self.assertEqual(self.c.get_status(TOKEN), "VERIFIED")
        self.assertTrue(self.c.is_verified(TOKEN))
        claim = json.loads(self.c.get_claim(cid))
        self.assertFalse(claim["can_challenge"])
        self.assertTrue(claim["can_settle"])

    def test_stale_claim_expires(self):
        submit(self.c, t=0)
        at(TTL - 1)
        self.assertTrue(self.c.is_verified(TOKEN))
        at(TTL)
        self.assertFalse(self.c.is_verified(TOKEN))
        self.assertEqual(self.c.get_status(TOKEN), "EXPIRED")

    def test_settle_does_not_extend_expiry(self):
        cid = submit(self.c, t=0)
        at(TTL - 10)
        self.c.settle(cid)
        at(TTL)
        self.assertFalse(self.c.is_verified(TOKEN))
        self.assertEqual(self.c.get_status(TOKEN), "EXPIRED")

    def test_challenge_gives_fresh_expiry(self):
        submit(self.c, t=0)
        at(100)
        as_user("0xCH", BOND)
        cid = "0"
        self.c.challenge(cid)
        at(TTL + 50)                 # past the original expiry
        self.assertTrue(self.c.is_verified(TOKEN))
        at(100 + TTL)                # past the re-check expiry
        self.assertFalse(self.c.is_verified(TOKEN))

    def test_resubmit_renews_after_expiry(self):
        submit(self.c, t=0)
        at(TTL + 1)
        self.assertEqual(self.c.get_status(TOKEN), "EXPIRED")
        submit(self.c, t=TTL + 1)
        at(TTL + 2)
        self.assertEqual(self.c.get_status(TOKEN), "PENDING_CHALLENGE")
        at(TTL + 1 + WINDOW)
        self.assertTrue(self.c.is_verified(TOKEN))

    def test_junk_claim_cannot_hide_valid_verdict(self):
        submit(self.c, t=0)                          # valid, final at WINDOW
        PAGES[URL2] = "Buy now! Marketing page. " * 30
        LLM[URL2] = {"is_audit_report": False, "auditor_match": False}
        submit(self.c, url=URL2, t=WINDOW + 10, who="0xGRIEF")
        at(WINDOW + 20)
        self.assertTrue(self.c.is_verified(TOKEN))

    def test_mismatch_then_real_report(self):
        real_report(URL2, OTHER)                     # report is for another token
        submit(self.c, url=URL2, t=0)
        at(WINDOW)
        self.assertEqual(self.c.get_status(TOKEN), "ADDRESS_MISMATCH")
        submit(self.c, url=URL, t=WINDOW)
        at(2 * WINDOW)
        self.assertTrue(self.c.is_verified(TOKEN))

    def test_case_insensitive_lookup(self):
        submit(self.c, t=0)
        at(WINDOW)
        self.assertTrue(self.c.is_verified(TOKEN.upper().replace("0X", "0x")))

    def test_no_claim(self):
        at(0)
        self.assertEqual(self.c.get_status(OTHER), "NO_CLAIM")
        self.assertFalse(self.c.is_verified(OTHER))

    def test_explicit_time_views(self):
        submit(self.c, t=0)
        t0 = int(T0.timestamp())
        self.assertEqual(self.c.get_status_at(TOKEN, t0 + 5), "PENDING_CHALLENGE")
        self.assertTrue(self.c.is_verified_at(TOKEN, t0 + WINDOW))
        self.assertFalse(self.c.is_verified_at(TOKEN, t0 + TTL))
        self.assertEqual(self.c.get_status_at(TOKEN, 0), "TIME_UNAVAILABLE")


# ---------------- input checks and funds ----------------

class InputAndFundsTests(Base):
    def test_bad_inputs(self):
        at(0)
        as_user("0xSUB", BOND)
        with self.assertRaisesRegex(Exception, "invalid token address"):
            self.c.submit_claim("0x123", "evm", "certik", URL)
        with self.assertRaisesRegex(Exception, "auditor not supported"):
            self.c.submit_claim(TOKEN, "evm", "fakeauditor", URL)
        with self.assertRaisesRegex(Exception, "official domain"):
            self.c.submit_claim(TOKEN, "evm", "certik", "https://certik-audit.xyz/r")
        with self.assertRaisesRegex(Exception, "https"):
            self.c.submit_claim(TOKEN, "evm", "certik", "http://skynet.certik.com/x")
        as_user("0xSUB", BOND - 1)
        with self.assertRaisesRegex(Exception, "bond below min_bond"):
            self.c.submit_claim(TOKEN, "evm", "certik", URL)

    def test_withdraw(self):
        cid = submit(self.c, t=0)
        at(WINDOW)
        self.c.settle(cid)
        as_user("0xSUB")
        self.assertEqual(self.c.withdraw(), BOND)
        self.assertEqual(TRANSFERS, [("0xSUB", BOND)])
        with self.assertRaisesRegex(Exception, "nothing to withdraw"):
            self.c.withdraw()


if __name__ == "__main__":
    unittest.main()
