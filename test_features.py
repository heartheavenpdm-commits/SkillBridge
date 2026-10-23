"""Tests for the features added in this round.

Run with:  python -m unittest -v test_features

Needs the full SkillBridge project and skillbridge.db next to this file; skipped
otherwise. Each test runs against a temporary copy of the database.
"""
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import server as S
except ImportError:
    S = None

ADDR = "203.0.113.5"


class FeatureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        original = S.DB_PATH
        self.addCleanup(setattr, S, "DB_PATH", original)
        S.DB_PATH = os.path.join(tmp, "test.db")
        shutil.copy(os.path.join(HERE, "skillbridge.db"), S.DB_PATH)
        self.db = S.connect()
        self.addCleanup(self.db.close)

        pw = S.hash_pw("demo")
        self.db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash) VALUES "
                        "('f_admin','admin','Feature Admin','f-admin@sb.ph',?),"
                        "('f_tutor','tutor','Feature Tutor','f-tutor@sb.ph',?),"
                        "('f_tutor2','tutor','Other Tutor','f-tutor2@sb.ph',?)", (pw, pw, pw))
        # Give the seeded student s1 an active assignment to f_tutor, for the access checks.
        # End the seeded assignment at its own start time, which satisfies the table's check.
        self.db.execute("UPDATE tutor_assignments SET ended_at = started_at "
                        "WHERE student_id = 's1' AND ended_at IS NULL")
        self.db.execute("INSERT INTO tutor_assignments (id, student_id, tutor_id, started_at) "
                        "VALUES ('f_link', 's1', 'f_tutor', '2026-01-01')")
        self.db.commit()
        self.admin = self.row("f_admin")
        self.tutor = self.row("f_tutor")
        self.other_tutor = self.row("f_tutor2")
        self.student = self.new_student("f_s")

    # helpers -----------------------------------------------------------------
    def row(self, pid):
        return dict(self.db.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone())

    def new_student(self, pid):
        self.db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash) VALUES (?,?,?,?,?)",
                        (pid, "student", "Student " + pid, pid + "@sb.ph", S.hash_pw("demo")))
        self.db.commit()
        return self.row(pid)

    def call(self, user, method, path, body=None):
        return S.route(self.db, method, path, user, body or {}, ADDR)

    def start_pretest(self, student, module=0):
        return self.call(student, "POST", "/api/student/start", {"kind": "pretest", "module": module})

    def answer(self, student, item, letter=None, skip=False):
        body = {"item": item, "skip": True} if skip else {"item": item, "letter": letter}
        return self.call(student, "POST", "/api/student/answer", body)

    def key(self, item):
        return S.KEY[S.C.item_index(item)]

    def wrong(self, item):
        return next(x for x in "ABCD" if x != self.key(item))

    def count(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()[0]


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class BattleAnswerTests(FeatureTests):
    """The battle screen sends 'letter' and 'skip'. The server used to read only 'chosen'."""

    def test_letter_field_is_recorded_and_progress_returned(self):
        self.start_pretest(self.student)
        res = self.answer(self.student, "1.1", letter=self.key("1.1"))
        self.assertIs(res["correct"], True)
        self.assertEqual(res["progress"], {"answered": 1, "total": 10, "needed": 5})
        stored = self.db.execute("SELECT chosen FROM responses WHERE item_id = '1.1' AND attempt_id IN "
                                 "(SELECT id FROM attempts WHERE student_id = 'f_s')").fetchone()[0]
        self.assertEqual(stored, self.key("1.1"))

    def test_wrong_letter_is_recorded_as_wrong(self):
        self.start_pretest(self.student)
        res = self.answer(self.student, "1.2", letter=self.wrong("1.2"))
        self.assertIs(res["correct"], False)

    def test_skip_is_recorded_as_a_skip_and_not_counted_as_answered(self):
        self.start_pretest(self.student)
        res = self.answer(self.student, "1.3", skip=True)
        self.assertIsNone(res["correct"])
        self.assertEqual(res["progress"]["answered"], 0)

    def test_long_test_answer_accepts_the_letter_field_too(self):
        self.db.execute("INSERT INTO long_test_attempts (id, student_id, status) "
                        "VALUES ('lt1', 'f_s', 'in_progress')")
        self.db.execute("INSERT INTO long_test_responses (attempt_id, item_id) "
                        "VALUES ('lt1', '1.1')")
        self.db.commit()
        # Long-test items are scored against KEY_C, not the pretest KEY.
        correct = S.KEY_C[S.C.item_index("1.1")]
        res = self.call(self.student, "POST", "/api/student/longtest/answer",
                        {"item": "1.1", "letter": correct})
        self.assertIs(res["correct"], True)
        stored = self.db.execute("SELECT chosen FROM long_test_responses "
                                 "WHERE attempt_id = 'lt1' AND item_id = '1.1'").fetchone()[0]
        self.assertEqual(stored, correct)

    def test_letter_outside_a_to_d_is_rejected(self):
        self.start_pretest(self.student)
        with self.assertRaises(S.ApiError) as cm:
            self.answer(self.student, "1.4", letter="E")
        self.assertEqual(cm.exception.code, 400)


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class PracticeTests(FeatureTests):
    def test_practice_items_never_include_the_answer_key(self):
        items = self.call(self.student, "GET", "/api/student/practice/0")
        self.assertEqual(len(items), 10)
        for it in items:
            self.assertNotIn("correct_option", it)

    def test_practice_answer_is_checked_and_stored(self):
        right = self.call(self.student, "POST", "/api/student/practice/answer",
                          {"item": "1.1", "chosen": self.key("1.1")})
        wrong = self.call(self.student, "POST", "/api/student/practice/answer",
                          {"item": "1.2", "chosen": self.wrong("1.2")})
        self.assertEqual(right, {"correct": True})
        self.assertEqual(wrong, {"correct": False})
        flags = [r[0] for r in self.db.execute(
            "SELECT is_correct FROM practice_responses WHERE student_id = 'f_s' ORDER BY id")]
        self.assertEqual(flags, [1, 0])

    def test_practice_rejects_unknown_items_and_bad_letters(self):
        for body in ({"item": "99.9", "chosen": "A"}, {"item": "1.1", "chosen": "E"},
                     {"item": "1.1", "chosen": None}, {"chosen": "A"}):
            with self.subTest(body=body), self.assertRaises(S.ApiError) as cm:
                self.call(self.student, "POST", "/api/student/practice/answer", body)
            self.assertEqual(cm.exception.code, 400)

    def test_practice_history_appears_in_the_student_state(self):
        self.call(self.student, "POST", "/api/student/practice/answer", {"item": "1.1", "chosen": self.key("1.1")})
        self.call(self.student, "POST", "/api/student/practice/answer", {"item": "1.2", "chosen": self.wrong("1.2")})
        st = self.call(self.student, "GET", "/api/student/state")
        self.assertEqual(st["practice"], [{"module": 0, "name": S.MODS[0], "answered": 2, "correct": 1}])


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class ViewTests(FeatureTests):
    def test_evaluation_skills_carry_subtopic_and_module_rollup(self):
        summary = S.student_summary(self.db, self.row("s1"))
        skills = {s["skill"]: s for s in summary["evaluation"]["skills"]}
        self.assertEqual(skills["Number patterns"]["subtopic"], "Patterns in Nature")
        rollup = summary["evaluation"]["rollup"]
        self.assertEqual([r["module"] for r in rollup], [0])
        self.assertIsNotNone(rollup[0]["pre"])

    def test_admin_graph_includes_modules_subtopics_and_skill_subtopics(self):
        g = self.call(self.admin, "GET", "/api/admin/graph")
        self.assertEqual(len(g["modules"]), 10)
        self.assertEqual(len(g["subtopics"]), 21)
        mean = next(s for s in g["skills"] if s["name"] == "Mean")
        self.assertEqual(mean["subtopic_name"], "Mean and Median")

    def test_validation_save_is_audited(self):
        self.call(self.admin, "POST", "/api/admin/validation/save",
                  {"attempt_id": "x1", "item_id": "1.1", "chosen": "B",
                   "system_label": "F", "expert_label": "P"})
        self.assertEqual(self.count("SELECT COUNT(*) FROM audit_log WHERE action = 'validation_save' "
                                    "AND entity_id = 'x1:1.1'"), 1)


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class PasswordTests(FeatureTests):
    def test_short_password_is_rejected_and_nothing_else_changes(self):
        before = self.row("f_s")
        with self.assertRaises(S.ApiError) as cm:
            self.call(self.admin, "POST", "/api/admin/users/update",
                      {"id": "f_s", "name": "Changed Name", "role": "student", "is_active": 1, "password": "abc"})
        self.assertEqual(cm.exception.code, 400)
        after = self.row("f_s")
        self.assertEqual(after["full_name"], before["full_name"])
        self.assertEqual(after["password_hash"], before["password_hash"])

    def test_admin_can_set_a_new_password_that_then_verifies(self):
         self.call(self.admin, "POST", "/api/admin/users/update",
                  {"id": "f_s", "name": "Student f_s", "role": "student",
                   "is_active": 1, "password": "New-pass-1"})
         self.assertTrue(S.check_pw("New-pass-1", self.row("f_s")["password_hash"]))
         self.assertFalse(S.check_pw("demo", self.row("f_s")["password_hash"]))

    def test_update_without_a_password_keeps_the_current_one(self):
        before = self.row("f_s")["password_hash"]
        self.call(self.admin, "POST", "/api/admin/users/update",
                  {"id": "f_s", "name": "Renamed Student", "role": "student", "is_active": 1})
        self.assertEqual(self.row("f_s")["password_hash"], before)


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class WeightTests(FeatureTests):
    def test_stored_weights_match_the_engine(self):
        self.assertTrue(S.weights_status(self.db)["match"])

    def test_a_changed_weight_is_reported_on_the_overview(self):
        self.db.execute("UPDATE scoring_weights SET weight = 0.5 WHERE version = 1 AND category = 'F'")
        self.db.commit()
        self.assertFalse(S.weights_status(self.db)["match"])
        self.assertFalse(self.call(self.admin, "GET", "/api/admin/overview")["weights"]["match"])

    def test_an_attempt_is_scored_with_the_weights_of_its_own_version(self):
        aid = self.db.execute("SELECT id FROM attempts WHERE student_id = 's1' AND kind = 'pretest' "
                              "AND module_id = 0").fetchone()[0]
        self.db.execute("INSERT INTO scoring_weights (version, category, weight) VALUES "
                        "(2, 'F', 0.6), (2, 'P', 0.2), (2, 'C', 0.2)")
        self.db.execute("UPDATE attempts SET scoring_version = 2 WHERE id = ?", (aid,))
        course_v2 = S.analysis(self.db, aid)["course"]
        self.db.execute("UPDATE attempts SET scoring_version = 1 WHERE id = ?", (aid,))
        course_v1 = S.analysis(self.db, aid)["course"]
        self.assertNotAlmostEqual(course_v1, course_v2, places=3)
        self.assertAlmostEqual(course_v1, S.analysis(self.db, aid, weights=S.WEIGHTS)["course"], places=9)

    def test_scoring_records_the_version_it_used(self):
        self.start_pretest(self.student, 0)
        for k in range(1, 6):
            self.answer(self.student, f"1.{k}", letter=self.key(f"1.{k}"))
        self.call(self.student, "POST", "/api/student/submit")
        aid = self.db.execute("SELECT id FROM attempts WHERE student_id = 'f_s'").fetchone()[0]
        self.assertEqual(self.count("SELECT scoring_version FROM attempts WHERE id = ?", (aid,)), S.CURRENT_SCORING_VERSION)


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class StaffSensitivityTests(FeatureTests):
    def test_admin_can_run_it_for_any_student(self):
        res = self.call(self.admin, "POST", "/api/staff/sensitivity", {"student_id": "s1"})
        self.assertTrue(res["has_data"])
        self.assertTrue(res["skills"])

    def test_the_assigned_tutor_can_run_it(self):
        self.assertTrue(self.call(self.tutor, "POST", "/api/staff/sensitivity", {"student_id": "s1"})["has_data"])

    def test_a_tutor_without_the_assignment_is_refused(self):
        with self.assertRaises(S.ApiError) as cm:
            self.call(self.other_tutor, "POST", "/api/staff/sensitivity", {"student_id": "s1"})
        self.assertEqual(cm.exception.code, 403)

    def test_students_cannot_run_it(self):
        with self.assertRaises(S.ApiError) as cm:
            self.call(self.student, "POST", "/api/staff/sensitivity", {"student_id": "s1"})
        self.assertEqual(cm.exception.code, 403)

    def test_missing_or_unknown_student(self):
        with self.assertRaises(S.ApiError) as cm:
            self.call(self.admin, "POST", "/api/staff/sensitivity", {})
        self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(S.ApiError) as cm:
            self.call(self.admin, "POST", "/api/staff/sensitivity", {"student_id": "nobody"})
        self.assertEqual(cm.exception.code, 404)


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class TutorCommentTests(FeatureTests):
    def test_overview_lists_each_tutors_comments(self):
        pre = self.db.execute("SELECT id FROM attempts WHERE student_id = 's1' AND kind = 'pretest' "
                              "AND module_id = 0").fetchone()[0]
        self.db.execute("INSERT INTO tutor_ratings (id, student_id, tutor_id, retest_id, stars, comment) "
                        "VALUES ('r1', 's1', 'f_tutor', ?, 4, 'Clear explanations')", (pre,))
        self.db.commit()
        tutors = {t["id"]: t for t in self.call(self.admin, "GET", "/api/admin/overview")["tutors"]}
        self.assertEqual([c["comment"] for c in tutors["f_tutor"]["comments"]], ["Clear explanations"])
        self.assertEqual(tutors["f_tutor2"]["comments"], [])


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class RetestFlowTests(FeatureTests):
    """Pretest, tutor-assigned retest, then a second retest. The second retest documents
    current behaviour: the code allows repeats. Whether to cap them is still a decision."""

    def test_pretest_then_assigned_retest_then_repeat(self):
        st = self.new_student("f_r")
        # A tutor may only assign a retest to a student assigned to them,
        # so link f_r to f_tutor before the assign call.
        self.db.execute(
                    "INSERT INTO tutor_assignments (id, student_id, tutor_id, started_at) "
                    "VALUES ('f_link_r', 'f_r', 'f_tutor', '2026-01-01')")
        self.db.commit()

        self.start_pretest(st, 0)
        for k in range(1, 6):
            self.answer(st, f"1.{k}", letter=self.wrong(f"1.{k}"))
        self.assertEqual(self.call(st, "POST", "/api/student/submit")["status"], "submitted")

        self.call(self.tutor, "POST", "/api/tutor/assign", {"student_id": "f_r", "module_id": 0})
        pending = self.call(st, "GET", "/api/student/state")["assigned_retests"]
        self.assertEqual([p["module_id"] for p in pending], [0])

        self.call(st, "POST", "/api/student/start", {"kind": "retest", "module": 0})
        aid = self.db.execute("SELECT id FROM attempts WHERE student_id = 'f_r' AND kind = 'retest' "
                              "AND status = 'in_progress'").fetchone()[0]
        for (iid,) in self.db.execute("SELECT item_id FROM responses WHERE attempt_id = ?", (aid,)).fetchall():
            self.answer(st, iid, letter=self.key(iid))
        self.assertEqual(self.call(st, "POST", "/api/student/submit")["status"], "submitted")

        self.call(st, "POST", "/api/student/start", {"kind": "retest", "module": 0})


if __name__ == "__main__":
    unittest.main()