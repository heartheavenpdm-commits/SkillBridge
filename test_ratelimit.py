"""Tests for sign-in and registration rate limiting.

Run with:  python -m unittest -v test_ratelimit

RateLimiter tests need only ratelimit.py. Endpoint tests also need the SkillBridge
server and skillbridge.db next to this file; they are skipped if either is missing.
Endpoint tests run against a temporary copy of the database and a fake clock.
"""
import os
import shutil
import tempfile
import threading
import unittest

import ratelimit as RL

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import server as S
except ImportError:
    S = None

REAL_ADDR = "198.51.100.7"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def make(self, limit=3, window=100):
        return RL.RateLimiter(limit, window, clock=self.clock)

    def test_allows_up_to_limit_then_blocks(self):
        lim = self.make(3, 100)
        self.assertEqual([lim.attempt("k") for _ in range(3)], [0.0, 0.0, 0.0])
        self.assertGreater(lim.attempt("k"), 0)

    def test_wait_is_time_until_enough_attempts_expire(self):
        lim = self.make(3, 100)
        for t in (0, 10, 20):
            self.clock.now = t
            lim.attempt("k")
        self.clock.now = 30
        self.assertAlmostEqual(lim.attempt("k"), 70.0)   # first attempt leaves the window at t=100

    def test_allowed_again_once_oldest_attempt_leaves_window(self):
        lim = self.make(3, 100)
        for t in (0, 10, 20):
            self.clock.now = t
            lim.attempt("k")
        self.clock.now = 99.9
        self.assertGreater(lim.attempt("k"), 0)
        self.clock.now = 100
        self.assertEqual(lim.attempt("k"), 0.0)

    def test_blocked_attempts_do_not_extend_the_lockout(self):
        lim = self.make(2, 100)
        self.clock.now = 0
        lim.attempt("k")
        self.clock.now = 1
        lim.attempt("k")
        self.clock.now = 50
        self.assertAlmostEqual(lim.attempt("k"), 50.0)
        self.clock.now = 60
        self.assertAlmostEqual(lim.attempt("k"), 40.0)
        self.clock.now = 99
        self.assertAlmostEqual(lim.attempt("k"), 1.0)
        self.clock.now = 100                              # ends on schedule, not later
        self.assertEqual(lim.attempt("k"), 0.0)

    def test_keys_are_independent(self):
        lim = self.make(1, 100)
        self.assertEqual(lim.attempt("a"), 0.0)
        self.assertGreater(lim.attempt("a"), 0)
        self.assertEqual(lim.attempt("b"), 0.0)

    def test_reset_clears_a_key(self):
        lim = self.make(1, 100)
        lim.attempt("a")
        self.assertGreater(lim.attempt("a"), 0)
        lim.reset("a")
        self.assertEqual(lim.attempt("a"), 0.0)

    def test_memory_is_swept_when_key_count_grows(self):
        old = RL.MAX_KEYS
        self.addCleanup(setattr, RL, "MAX_KEYS", old)
        RL.MAX_KEYS = 5
        lim = self.make(1, 10)
        for i in range(5):
            lim.attempt(f"old{i}")
        self.clock.now = 20                               # all five are now outside the window
        lim.attempt("new")
        self.assertEqual(len(lim._events), 1)

    def test_rejects_bad_settings(self):
        for limit, window in ((0, 10), (1, 0)):
            with self.subTest(limit=limit, window=window), self.assertRaises(ValueError):
                RL.RateLimiter(limit, window)

    def test_parallel_burst_cannot_exceed_the_limit(self):
        lim = self.make(5, 100)
        results, lock = [], threading.Lock()

        def go():
            r = lim.attempt("k")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=go) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r == 0.0), 5)


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class AuthThrottleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        original_path = S.DB_PATH
        self.addCleanup(setattr, S, "DB_PATH", original_path)
        S.DB_PATH = os.path.join(tmp, "test.db")
        shutil.copy(os.path.join(HERE, "skillbridge.db"), S.DB_PATH)
        self.db = S.connect()
        self.addCleanup(self.db.close)

        # Swap in limiters that use a fake clock and fresh state for each test.
        self.clock = FakeClock()
        saved = (S.LOGIN_ACCOUNT_LIMIT, S.LOGIN_ADDRESS_LIMIT, S.REGISTER_ADDRESS_LIMIT)
        self.addCleanup(self._restore_limits, saved)
        S.LOGIN_ACCOUNT_LIMIT = RL.RateLimiter(5, 15 * 60, clock=self.clock)
        S.LOGIN_ADDRESS_LIMIT = RL.RateLimiter(100, 15 * 60, clock=self.clock)
        S.REGISTER_ADDRESS_LIMIT = RL.RateLimiter(50, 60 * 60, clock=self.clock)

        pw = S.hash_pw("right-pass")
        self.db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash) VALUES "
                        "('t_one','student','Student One','student.one@sb.ph',?),"
                        "('t_two','student','Student Two','student.two@sb.ph',?)", (pw, pw))
        self.db.commit()

    def _restore_limits(self, saved):
        S.LOGIN_ACCOUNT_LIMIT, S.LOGIN_ADDRESS_LIMIT, S.REGISTER_ADDRESS_LIMIT = saved

    def login(self, email, password, addr=REAL_ADDR):
        return S.route(self.db, "POST", "/api/login", None,
                       {"email": email, "password": password}, addr)

    def login_codes(self, email, passwords, addr=REAL_ADDR):
        codes = []
        for pw in passwords:
            try:
                self.login(email, pw, addr)
                codes.append(200)
            except S.ApiError as e:
                codes.append(e.code)
        return codes

    def register(self, email, addr=REAL_ADDR):
        return S.route(self.db, "POST", "/api/register", None,
                       {"name": "New Person", "email": email,
                        "password": "Test1234", "avatar": "🎓"}, addr)

    def test_five_failures_lock_the_account_even_for_the_right_password(self):
        codes = self.login_codes("student.one@sb.ph", ["wrong"] * 5 + ["right-pass"])
        self.assertEqual(codes, [401] * 5 + [429])

    def test_lockout_message_gives_the_wait_in_minutes(self):
        self.login_codes("student.one@sb.ph", ["wrong"] * 5)
        with self.assertRaises(S.ApiError) as cm:
            self.login("student.one@sb.ph", "right-pass")
        self.assertEqual(cm.exception.code, 429)
        self.assertIn("Too many sign-in attempts for this account", cm.exception.msg)
        self.assertIn("Try again in 15 minutes.", cm.exception.msg)

    def test_account_unlocks_after_the_window(self):
        self.login_codes("student.one@sb.ph", ["wrong"] * 5)
        self.clock.now = 15 * 60
        result = self.login("student.one@sb.ph", "right-pass")
        self.assertIn("token", result)

    def test_a_correct_sign_in_clears_earlier_failures(self):
        self.assertEqual(self.login_codes("student.one@sb.ph", ["wrong"] * 4), [401] * 4)
        self.login("student.one@sb.ph", "right-pass")
        self.assertEqual(self.login_codes("student.one@sb.ph", ["wrong"] * 4), [401] * 4)

    def test_unknown_email_is_throttled_the_same_way(self):
        unknown = self.login_codes("nobody@sb.ph", ["wrong"] * 6)
        real = self.login_codes("student.two@sb.ph", ["wrong"] * 6)
        self.assertEqual(unknown, [401] * 5 + [429])
        self.assertEqual(unknown, real)

    def test_a_lockout_on_one_account_leaves_others_alone(self):
        self.login_codes("student.one@sb.ph", ["wrong"] * 5)
        self.assertEqual(self.login_codes("student.one@sb.ph", ["right-pass"]), [429])
        self.assertEqual(self.login_codes("student.two@sb.ph", ["right-pass"]), [200])

    def test_address_limit_stops_guessing_across_accounts(self):
        S.LOGIN_ADDRESS_LIMIT = RL.RateLimiter(3, 15 * 60, clock=self.clock)
        self.assertEqual(self.login_codes("a@sb.ph", ["x"]), [401])
        self.assertEqual(self.login_codes("b@sb.ph", ["x"]), [401])
        self.assertEqual(self.login_codes("c@sb.ph", ["x"]), [401])
        with self.assertRaises(S.ApiError) as cm:
            self.login("student.one@sb.ph", "right-pass")
        self.assertEqual(cm.exception.code, 429)
        self.assertIn("from this device", cm.exception.msg)

    def test_a_different_address_is_not_affected(self):
        S.LOGIN_ADDRESS_LIMIT = RL.RateLimiter(1, 15 * 60, clock=self.clock)
        self.login_codes("a@sb.ph", ["x"], addr="10.0.0.1")
        self.assertEqual(self.login_codes("a@sb.ph", ["x"], addr="10.0.0.1"), [429])
        self.assertEqual(self.login_codes("student.one@sb.ph", ["right-pass"], addr="10.0.0.2"), [200])

    def test_registration_is_limited_per_address(self):
        S.REGISTER_ADDRESS_LIMIT = RL.RateLimiter(2, 60 * 60, clock=self.clock)
        self.assertIn("token", self.register("new1@sb.ph"))
        self.assertIn("token", self.register("new2@sb.ph"))
        with self.assertRaises(S.ApiError) as cm:
            self.register("new3@sb.ph")
        self.assertEqual(cm.exception.code, 429)
        self.assertIn("Too many sign-up attempts from this device", cm.exception.msg)

    def test_invalid_registrations_still_count(self):
        S.REGISTER_ADDRESS_LIMIT = RL.RateLimiter(2, 60 * 60, clock=self.clock)
        for bad in ("not-an-email", "also-bad"):
            with self.assertRaises(S.ApiError) as cm:
                self.register(bad)
            self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(S.ApiError) as cm:
            self.register("valid@sb.ph")
        self.assertEqual(cm.exception.code, 429)


if __name__ == "__main__":
    unittest.main()