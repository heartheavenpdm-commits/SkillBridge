"""Tests for bulk student import.

Run with:  python -m unittest -v test_bulk_import

The parsing and validation tests need only bulk_import.py. The endpoint tests also
need the rest of the SkillBridge project and skillbridge.db next to this file; they
are skipped if either is missing. Endpoint tests run against a temporary copy of
the database, so the real one is never written to.
"""
import json
import os
import shutil
import tempfile
import unittest

import bulk_import as BI

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import server as S
except ImportError:
    S = None

SAMPLE_CSV = (
   "name,email,password\n"
    "Lea Reyes,lea.reyes@school.ph,\n"
    "Ben Cruz,ben.cruz@school.ph,Secret123\n"   # meets the new strict rule
    "Existing Student,EXISTING@sb.ph,\n"
    "Bad Row,not-an-email,\n"
)


def row(name="Ana Reyes", email="ana@school.ph", password=""):
    return {"line": 2, "name": name, "email": email, "password": password}


class ParseTests(unittest.TestCase):
    def test_minimal_header_and_row(self):
        rows = BI.parse_csv("name,email\nMaria Santos,maria@school.ph\n")
        self.assertEqual(rows, [{"line": 2, "name": "Maria Santos",
                                 "email": "maria@school.ph", "password": ""}])

    def test_header_aliases_and_case_are_accepted(self):
        rows = BI.parse_csv("Full Name, EMAIL ,Password\nBen Cruz,ben@school.ph,secret1\n")
        self.assertEqual((rows[0]["name"], rows[0]["email"], rows[0]["password"]),
                         ("Ben Cruz", "ben@school.ph", "secret1"))

    def test_excel_byte_order_mark_is_ignored(self):
        rows = BI.parse_csv("\ufeffname,email\nNiña Dela Peña,nina@school.ph\n")
        self.assertEqual(rows[0]["name"], "Niña Dela Peña")

    def test_quoted_comma_inside_a_name(self):
        rows = BI.parse_csv('name,email\n"Santos, Maria",maria@school.ph\n')
        self.assertEqual(rows[0]["name"], "Santos, Maria")

    def test_blank_lines_skipped_but_line_numbers_kept(self):
        rows = BI.parse_csv("name,email\n\n,\nAna Reyes,ana@school.ph\n\nLuis Tan,luis@school.ph\n")
        self.assertEqual([(r["line"], r["name"]) for r in rows],
                         [(4, "Ana Reyes"), (6, "Luis Tan")])

    def test_extra_columns_ignored_and_short_rows_padded(self):
        rows = BI.parse_csv("name,email,grade,password\nAna Reyes,ana@school.ph,7\n")
        self.assertEqual(rows[0]["password"], "")

    def test_header_without_email_column_is_rejected(self):
        with self.assertRaisesRegex(BI.BulkImportError, "'name' and 'email'"):
            BI.parse_csv("name,mail\nAna,ana@school.ph\n")

    def test_empty_and_header_only_files_are_rejected(self):
        for text in ("", "   \n\n", None):
            with self.subTest(text=text), self.assertRaises(BI.BulkImportError):
                BI.parse_csv(text)
        with self.assertRaisesRegex(BI.BulkImportError, "no student rows"):
            BI.parse_csv("name,email\n")

    def test_row_limit_is_inclusive(self):
        body = "".join(f"Student {i},s{i}@school.ph\n" for i in range(BI.MAX_ROWS))
        self.assertEqual(len(BI.parse_csv("name,email\n" + body)), BI.MAX_ROWS)
        with self.assertRaisesRegex(BI.BulkImportError, f"at most {BI.MAX_ROWS} students"):
            BI.parse_csv("name,email\n" + body + "Extra Person,extra@school.ph\n")

    def test_size_limit(self):
        big = "name,email\n" + ("Ana Reyes,ana@school.ph," + "x" * 100 + "\n") * 6000
        with self.assertRaisesRegex(BI.BulkImportError, "larger than 512 KB"):
            BI.parse_csv(big)


class CheckRowTests(unittest.TestCase):
    def test_well_formed_row_passes(self):
        self.assertIsNone(BI.check_row(row()))

    def test_missing_name(self):
        self.assertEqual(BI.check_row(row(name="")), "name is missing")

    def test_name_length_bounds(self):
        self.assertIsNone(BI.check_row(row(name="Al")))
        self.assertIsNone(BI.check_row(row(name="x" * 120)))
        self.assertIsNotNone(BI.check_row(row(name="A")))
        self.assertIsNotNone(BI.check_row(row(name="x" * 121)))

    def test_missing_email(self):
        self.assertEqual(BI.check_row(row(email="")), "email is missing")

    def test_email_shapes(self):
        for bad in ("ana", "ana@school", "ana @school.ph", "a@b@c.ph", "ana@.ph", "x" * 250 + "@a.ph"):
            with self.subTest(email=bad):
                self.assertEqual(BI.check_row(row(email=bad)), "email is not valid")
        for good in ("ana@school.ph", "first.last+tag@sub.school.edu.ph"):
            with self.subTest(email=good):
                self.assertIsNone(BI.check_row(row(email=good)))

    def test_password_rules(self):
        self.assertIsNone(BI.check_row(row(password="")))            # blank → generated
        self.assertIsNone(BI.check_row(row(password="Abcdef12")))    # 8 chars, upper+lower+digit
        self.assertIsNone(BI.check_row(row(password="Aa1" + "x" * 125)))  # exactly 128
        self.assertIsNotNone(BI.check_row(row(password="abc")))      # too short
        self.assertIsNotNone(BI.check_row(row(password="abcdefg1"))) # no uppercase
        self.assertIsNotNone(BI.check_row(row(password="ABCDEFG1"))) # no lowercase
        self.assertIsNotNone(BI.check_row(row(password="Abcdefgh"))) # no digit
        self.assertIsNotNone(BI.check_row(row(password="Aa1" + "x" * 126)))  # 129 chars


class PlanTests(unittest.TestCase):
    def test_ready_row(self):
        self.assertEqual(BI.plan([row()], set())[0]["status"], "ready")

    def test_existing_email_matches_case_insensitively(self):
        res = BI.plan([row(email="MARIA@sb.ph")], {"maria@sb.ph"})
        self.assertEqual((res[0]["status"], res[0]["message"]), ("error", "email is already registered"))

    def test_duplicate_in_file_points_at_first_occurrence(self):
        rows = [dict(row(email="b@school.ph"), line=2),
                dict(row(name="Other Person", email="B@school.ph"), line=5)]
        res = BI.plan(rows, set())
        self.assertEqual(res[0]["status"], "ready")
        self.assertEqual(res[1]["message"], "duplicate of line 2 in this file")

    def test_invalid_first_row_does_not_block_a_valid_repeat(self):
        rows = [dict(row(name="A", email="c@school.ph"), line=2),
                dict(row(email="c@school.ph"), line=3)]
        self.assertEqual([r["status"] for r in BI.plan(rows, set())], ["error", "ready"])

    def test_results_keep_row_order_and_flag_supplied_passwords(self):
        rows = [dict(row(email="one@school.ph"), line=2),
                dict(row(email="two@school.ph", password="abcd"), line=3)]
        res = BI.plan(rows, set())
        self.assertEqual([r["email"] for r in res], ["one@school.ph", "two@school.ph"])
        self.assertEqual([r["has_password"] for r in res], [False, True])

    def test_summary_counts(self):
        res = BI.plan([row(email="x@school.ph"), row(email="bad"), row(email="y@school.ph", password="abc")], set())
        s = BI.summary(res, dry_run=True)
        self.assertEqual((s["total"], s["ready"], s["failed"], s["created"], s["dry_run"]),
                         (3, 1, 2, 0, True))


class GeneratedPasswordTests(unittest.TestCase):
    def test_shape(self):
        pw = BI.make_password()
        self.assertEqual(len(pw), BI.GENERATED_LENGTH)
        self.assertTrue(set(pw) <= set(BI.PASSWORD_ALPHABET))

    def test_no_look_alike_characters(self):
        self.assertEqual(set("iIlLoO01") & set(BI.PASSWORD_ALPHABET), set())

    def test_not_repeated(self):
        self.assertEqual(len({BI.make_password() for _ in range(50)}), 50)


@unittest.skipIf(S is None or not os.path.exists(os.path.join(HERE, "skillbridge.db")),
                 "needs the full SkillBridge project and skillbridge.db next to this file")
class BulkEndpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        original_path = S.DB_PATH
        self.addCleanup(setattr, S, "DB_PATH", original_path)
        S.DB_PATH = os.path.join(tmp, "test.db")
        shutil.copy(os.path.join(HERE, "skillbridge.db"), S.DB_PATH)
        self.db = S.connect()
        self.addCleanup(self.db.close)   # runs before rmtree, so the file is released first

        pw = S.hash_pw("demo")
        self.db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash) VALUES "
                        "('test_admin','admin','Test Admin','test-admin@sb.ph',?),"
                        "('test_tutor','tutor','Test Tutor','test-tutor@sb.ph',?),"
                        "('test_existing','student','Existing Student','existing@sb.ph',?)",
                        (pw, pw, pw))
        self.db.commit()
        self.admin = dict(self.db.execute("SELECT * FROM profiles WHERE id='test_admin'").fetchone())
        self.tutor = dict(self.db.execute("SELECT * FROM profiles WHERE id='test_tutor'").fetchone())

    def call(self, user, csv_text, commit=False):
        return S.route(self.db, "POST", "/api/admin/users/bulk", user,
                       {"csv": csv_text, "commit": commit})

    def count(self, sql="SELECT COUNT(*) FROM profiles"):
        return self.db.execute(sql).fetchone()[0]

    def stored(self, email):
        return self.db.execute("SELECT role, full_name, password_hash FROM profiles WHERE email = ?",
                               (email,)).fetchone()

    def test_only_admins_may_import(self):
        for who in (self.tutor, {"id": "s1", "role": "student"}):
            with self.subTest(role=who["role"]), self.assertRaises(S.ApiError) as cm:
                self.call(who, SAMPLE_CSV, commit=True)
            self.assertEqual(cm.exception.code, 403)

    def test_preview_writes_nothing(self):
        before = self.count()
        res = self.call(self.admin, SAMPLE_CSV)
        self.assertTrue(res["dry_run"])
        self.assertEqual(self.count(), before)

    def test_commit_creates_students_and_only_students(self):
        res = self.call(self.admin, SAMPLE_CSV, commit=True)
        self.assertEqual((res["created"], res["failed"]), (2, 2))
        for email in ("lea.reyes@school.ph", "ben.cruz@school.ph"):
            with self.subTest(email=email):
                self.assertEqual(self.stored(email)["role"], "student")

    def test_supplied_password_is_hashed_and_not_echoed(self):
        res = self.call(self.admin, SAMPLE_CSV, commit=True)
        ben = self.stored("ben.cruz@school.ph")
        self.assertTrue(S.check_pw("Secret123", ben["password_hash"]))
        self.assertNotIn("Secret123", ben["password_hash"])
        self.assertNotIn("Secret123", json.dumps(res))

    def test_generated_password_is_returned_once_and_works(self):
        res = self.call(self.admin, SAMPLE_CSV, commit=True)
        with_pw = [r for r in res["rows"] if "password" in r]
        self.assertEqual([r["email"] for r in with_pw], ["lea.reyes@school.ph"])
        generated = with_pw[0]["password"]
        self.assertEqual(len(generated), BI.GENERATED_LENGTH)
        self.assertTrue(S.check_pw(generated, self.stored("lea.reyes@school.ph")["password_hash"]))

    def test_existing_email_is_skipped_case_insensitively(self):
        self.call(self.admin, SAMPLE_CSV, commit=True)
        self.assertEqual(self.count("SELECT COUNT(*) FROM profiles WHERE LOWER(email) = 'existing@sb.ph'"), 1)

    def test_rerunning_the_same_file_creates_nothing_new(self):
        self.call(self.admin, SAMPLE_CSV, commit=True)
        before = self.count()
        audit_before = self.count("SELECT COUNT(*) FROM audit_log WHERE action = 'admin_bulk_import'")
        res = self.call(self.admin, SAMPLE_CSV, commit=True)
        self.assertEqual(res["created"], 0)
        self.assertEqual(self.count(), before)
        self.assertEqual(self.count("SELECT COUNT(*) FROM audit_log WHERE action = 'admin_bulk_import'"),
                         audit_before)

    def test_audit_entry_holds_ids_and_counts_not_emails(self):
        res = self.call(self.admin, SAMPLE_CSV, commit=True)
        created_ids = sorted(r["id"] for r in res["rows"] if r["status"] == "created")
        entry = self.db.execute("SELECT actor_id, detail FROM audit_log WHERE action = 'admin_bulk_import' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(entry["actor_id"], "test_admin")
        detail = json.loads(entry["detail"])
        self.assertEqual((detail["submitted"], detail["created"]), (4, 2))
        self.assertEqual(sorted(detail["ids"]), created_ids)
        self.assertNotIn("@", entry["detail"])

    def test_file_level_problems_are_400_errors(self):
        for text in ("", "name,mail\nAna,ana@school.ph\n"):
            with self.subTest(text=text), self.assertRaises(S.ApiError) as cm:
                self.call(self.admin, text)
            self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()