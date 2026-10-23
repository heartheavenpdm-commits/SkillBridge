"""Run with:  python -m unittest -v test_skillbridge"""
import os
import sqlite3
import unittest

import curriculum as C
from access import can_change_role, can_read_attempt, can_rate, can_see_answer_key
from engine import (analyze, apply_retest, band, is_valid_attempt, rank_resources,
                    retest_items, targets, WEIGHTS)

KEY = C.key_letters()
CATS = C.categories()
SKILLS = C.skills()
MODS = C.MODULES
HERE = os.path.dirname(os.path.abspath(__file__))


def simulate(seed, acc, key=KEY):
    """Seeded answer generator, same algorithm as the JavaScript prototype (gen function)."""
    M = 2147483647
    s = seed * 7919 % M
    out = []
    for i in range(100):
        s = s * 16807 % M
        r = s / M
        s = s * 16807 % M
        t = s / M
        k = key[i]
        wrong = [x for x in "ABCD" if x != k]
        out.append("-" if t < 0.03 else (k if r < acc else wrong[min(2, int((r - acc) / (1 - acc) * 3))]))
    return out


def run(answers, corrected=False):
    return analyze(answers, C.key_letters(corrected), CATS, SKILLS, C.EDGES, MODS)


class CurriculumTests(unittest.TestCase):
    def test_every_item_belongs_to_exactly_one_skill(self):
        # SKILLS entries are 4-tuples: (name, module, subtopic, items)
        seen = sorted(i for _, _, _, items in SKILLS for i in items)
        self.assertEqual(seen, list(range(100)))

    def test_skills_have_at_least_three_items(self):
        self.assertTrue(all(len(items) >= 3 for _, _, _, items in SKILLS))

    def test_corrected_key_changes_exactly_the_four_flagged_items(self):
        base, fixed = C.key_letters(), C.key_letters(corrected=True)
        changed = {C.item_id(i) for i in range(100) if base[i] != fixed[i]}
        self.assertEqual(changed, {"5.7", "6.4", "9.4", "9.5"})
        self.assertEqual(fixed[C.item_index("9.4")], "C")


class EngineTests(unittest.TestCase):
    def test_pretest_course_matches_javascript_prototype(self):
        # Values printed by the prototype test harness (seed, accuracy) -> course %.
        expected = {(11, .62): 52.5, (23, .48): 47.9, (37, .70): 64.8, (41, .55): 53.2,
                    (53, .58): 51.8, (59, .66): 63.6, (67, .52): 51.6, (71, .44): 39.1}
        for (sd, acc), want in expected.items():
            with self.subTest(seed=sd):
                self.assertAlmostEqual(run(simulate(sd, acc))["course"], want, delta=0.051)

    def test_targets_match_javascript_prototype_for_maria(self):
        names = [SKILLS[k][0] for k in targets(run(simulate(11, .62)))]
        self.assertEqual(names, ["Nature & role of math", "r range & special values",
                                 "Borda & instant runoff"])

    def test_perfect_and_blank_attempts(self):
        self.assertEqual(run(list(KEY))["course"], 100)
        blank = run(["-"] * 100)
        self.assertEqual(blank["course"], 0)
        self.assertEqual(blank["blank"], 100)

    def test_blank_stays_in_the_denominator(self):
        ans = list(KEY)
        i = C.item_index("1.1")          # first item of 'Number patterns'
        ans[i] = "-"
        res = run(ans)
        den = sum(WEIGHTS[CATS[j]] for j in SKILLS[0][3])   # [3] is the items list
        self.assertAlmostEqual(res["sk"][0]["P"], 100 * (den - WEIGHTS[CATS[i]]) / den)
        self.assertEqual(res["sk"][0]["blank"], 1)

    def test_root_cause_points_at_mean_when_sd_is_blank(self):
        ans = list(KEY)
        for i in SKILLS[15][3]:           # 'Std deviation & variance' left blank
            ans[i] = "-"
        res = run(ans)
        mean_k = 11
        self.assertEqual(res["sk"][mean_k]["P"], 100)
        self.assertAlmostEqual(res["root"][mean_k], 40.0)   # 0.5 * deficit(SD) = 0.5 * 80
        self.assertIn(mean_k, targets(res))

    def test_resource_ranking_prefers_the_matching_video(self):
        ans = list(KEY)
        for i in SKILLS[11][3]:           # 'Mean' left blank
            ans[i] = "-"
        top = rank_resources(run(ans)["need"], C.RESOURCES)[0]
        self.assertEqual(top["id"], "R-M05-001")
        self.assertAlmostEqual(top["cosine"], 1.0)

    def test_retest_carries_forward_untargeted_skills(self):
        pre = run(simulate(11, .62))
        tgt = targets(pre)
        ans2 = ["-"] * 100
        for i in retest_items(tgt, SKILLS):
            ans2[i] = KEY[i]
        out = apply_retest(pre, run(ans2), tgt)
        for k, s in enumerate(pre["sk"]):
            if k not in tgt:
                self.assertEqual(out["post"][k], s["P"])
        self.assertAlmostEqual(out["after"], 100)
        self.assertGreater(out["gain"], 0)

    def test_validity_threshold_is_fifty_percent(self):
        self.assertTrue(is_valid_attempt(50, 100))
        self.assertFalse(is_valid_attempt(49, 100))

    def test_bands(self):
        self.assertEqual(band(80), "Mastered")
        self.assertEqual(band(79.9), "Developing")
        self.assertEqual(band(59.9), "Needs Review")


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
            self.db.executescript(f.read())
        self.db.execute("INSERT INTO modules VALUES (0, 'Nature of Mathematics', 1)")
        # skills has 4 columns: (id, module_id, subtopic_id, name); subtopic is a FK.
        self.db.execute("INSERT INTO subtopics VALUES (0, 0, 'Patterns')")
        self.db.execute("INSERT INTO skills VALUES (0, 0, 0, 'Number patterns')")
        self.db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash) VALUES "
                        "('s1','student','Maria Santos','maria@sb.ph','h'),"
                        "('s2','student','Paolo Garcia','paolo@sb.ph','h'),"
                        "('t1','tutor','Ana Ayuban','ana@sb.ph','h'),"
                        "('t2','tutor','Juan dela Cruz','juan@sb.ph','h')")

    def tearDown(self):
        self.db.close()

    def _attempt(self, aid, student, kind="pretest", parent=None):
        self.db.execute("INSERT INTO attempts (id, student_id, kind, parent_attempt_id, module_id) VALUES (?,?,?,?,0)",
                        (aid, student, kind, parent))

    def test_only_one_pretest_per_student_per_module(self):
        self._attempt("a1", "s1")
        with self.assertRaises(sqlite3.IntegrityError):
            self._attempt("a2", "s1")

    def test_retest_needs_a_parent_attempt(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._attempt("r1", "s1", kind="retest", parent=None)

    def test_one_active_tutor_per_student(self):
        self.db.execute("INSERT INTO tutor_assignments (id, student_id, tutor_id, started_at) "
                        "VALUES ('x1','s1','t1','2026-01-01')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO tutor_assignments (id, student_id, tutor_id, started_at) "
                            "VALUES ('x2','s1','t2','2026-02-01')")

    def test_invalid_answer_letter_is_rejected(self):
        self._attempt("a1", "s1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO responses (attempt_id, item_id, chosen) VALUES ('a1','1.1','E')")

    def test_blank_response_is_allowed(self):
        self._attempt("a1", "s1")
        self.db.execute("INSERT INTO items (id, module_id, position, primary_skill_id, category, stem, correct_option) "
                        "VALUES ('1.1',0,1,0,'F','2,4,6,8,?','B')")
        self.db.execute("INSERT INTO responses (attempt_id, item_id, chosen) VALUES ('a1','1.1',NULL)")

    def test_unknown_student_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._attempt("a9", "nobody")


class AccessTests(unittest.TestCase):
    def test_role_rules(self):
        s1 = {"id": "s1", "role": "student"}
        s2 = {"id": "s2", "role": "student"}
        t1 = {"id": "t1", "role": "tutor"}
        adm = {"id": "a", "role": "admin"}
        att = {"student_id": "s1", "kind": "retest", "status": "submitted"}
        links = {("s1", "t1")}
        self.assertTrue(can_read_attempt(s1, att, links))
        self.assertFalse(can_read_attempt(s2, att, links))
        self.assertTrue(can_read_attempt(t1, att, links))
        self.assertFalse(can_read_attempt(t1, {"student_id": "s2"}, links))
        self.assertTrue(can_read_attempt(adm, att, links))
        self.assertTrue(can_rate(s1, att))
        self.assertFalse(can_rate(s2, att))
        self.assertFalse(can_rate(s1, {"student_id": "s1", "kind": "retest", "status": "in_progress"}))
        self.assertTrue(can_change_role(adm))
        self.assertFalse(can_change_role(s1))
        self.assertFalse(can_see_answer_key(s1))
        self.assertTrue(can_see_answer_key(t1))


if __name__ == "__main__":
    unittest.main()