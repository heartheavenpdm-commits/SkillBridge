"""SkillBridge server."""
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import uuid
import ssl
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import curriculum as C
import lessons as L
from access import can_answer
from engine import WEIGHTS, analyze, band, is_valid_attempt, rank_resources
import bulk_import as BI
import ratelimit as RL

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "skillbridge.db")
KEY = C.key_letters(corrected=True)
KEY_B = C.set_key("B")
KEY_C = C.set_key("C")
CATS = C.categories()
SKILLS = C.skills()
MODS = C.MODULES
EDGES = C.EDGES
SUBTOPICS = C.SUBTOPICS

SKILL_OF = {i: k for k, (_, _, _, items) in enumerate(SKILLS) for i in items}
SKILL_ITEMS = {k: items for k, (_, _, _, items) in enumerate(SKILLS)}
MOD_SKILLS = {m: [k for k, s in enumerate(SKILLS) if s[1] == m] for m in range(10)}
MOD_ITEMS = {m: list(range(m * 10, m * 10 + 10)) for m in range(10)}
RESOURCE_BY_ID = {rid: (title, cov) for rid, title, cov in C.RESOURCES}
LESSON_CONTENT = L.LESSONS
DMAP = {}


def _load_allowed_origins():
    default = ["http://127.0.0.1:8000", "https://127.0.0.1:8000",
               "http://localhost:8000", "https://localhost:8000"]
    env = os.environ.get("SKILLBRIDGE_ALLOWED_ORIGINS", "")
    extra = [o.strip() for o in env.split(",") if o.strip()]
    return default + extra


ALLOWED_ORIGINS = _load_allowed_origins()

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("skillbridge")

LOGIN_ACCOUNT_LIMIT = RL.RateLimiter(limit=5, window=15 * 60)
LOGIN_ADDRESS_LIMIT = RL.RateLimiter(limit=100, window=15 * 60)
REGISTER_ADDRESS_LIMIT = RL.RateLimiter(limit=50, window=60 * 60)


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    row = db.execute("SELECT version FROM schema_version").fetchone()
    version = row[0] if row else 0
    if version < 1:
        cols = [r[1] for r in db.execute("PRAGMA table_info(profiles)")]
        if "is_verified" not in cols:
            db.execute("ALTER TABLE profiles ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 1")
            db.execute("ALTER TABLE profiles ADD COLUMN verification_token TEXT")
        db.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (1)")
        db.commit()


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def validate_password_strength(pw):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Z]", pw):
        return "Password must contain an uppercase letter."
    if not re.search(r"[a-z]", pw):
        return "Password must contain a lowercase letter."
    if not re.search(r"[0-9]", pw):
        return "Password must contain a number."
    return None


def _read_md(filename):
    for base in (os.path.join(HERE, "data"), HERE):
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return open(path, encoding="utf-8").read()
    raise FileNotFoundError(f"cannot find {filename}")


def parse_questions(filename="questionnaire.md"):
    text = _read_md(filename)
    parts = re.split(r"\*\*(\d+\.\d+)\.\*\* ", text)
    out = {}
    for iid, body in zip(parts[1::2], parts[2::2]):
        stem = body.split("\n\nA)")[0].strip()
        out[iid] = (stem, dict(re.findall(r"^([A-D])\) (.+?)\s*$", body, re.M)))
    assert len(out) == 100 and all(len(o) == 4 for _, o in out.values()), \
        f"{filename}: parse failed"
    return out


def audit(db, actor_id, action, entity, entity_id=None, detail=None):
    try:
        db.execute("INSERT INTO audit_log (actor_id, action, entity, entity_id, detail) VALUES (?,?,?,?,?)",
                   (actor_id, action, entity, entity_id,
                    json.dumps(detail) if detail is not None else None))
    except Exception:
        pass


def refresh_dmap(db):
    """Rebuild the distractor map from Set A items only.

    Retest (B) and long-test (C) distractors are ignored because they are scored
    with distractor_map=None everywhere else, and because the (index, letter) key
    would otherwise collide with pretest items at the same position.
    """
    global DMAP
    new = {}
    for r in db.execute(
            "SELECT o.item_id, o.option_letter, o.error_source "
            "FROM item_options o JOIN items i ON i.id = o.item_id "
            "WHERE o.error_source IS NOT NULL AND i.set_code = 'A'"):
        idx = C.item_index(r["item_id"])
        new[(idx, r["option_letter"])] = r["error_source"]
    DMAP = new


def seed(db):
    db.executescript(open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read())
    db.executescript("CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES profiles(id), created_at TEXT NOT NULL);")

    qs_a = parse_questions("questionnaire.md")
    qs_b = parse_questions("questionnaire_set_b.md")
    qs_c = parse_questions("questionnaire_set_c_retest.md")

    for i, t in enumerate(MODS):
        db.execute("INSERT INTO modules VALUES (?,?,?)", (i, t, i + 1))
    for i, (mod, title) in enumerate(SUBTOPICS):
        db.execute("INSERT INTO subtopics VALUES (?,?,?)", (i, mod, title))
    for k, (name, mod, sub, _) in enumerate(SKILLS):
        db.execute("INSERT INTO skills VALUES (?,?,?,?)", (k, mod, sub, name))
    for a, b in EDGES:
        db.execute("INSERT INTO skill_prerequisites VALUES (?,?)", (b, a))
    for rid, title, cov in C.RESOURCES:
        kind = "video" if "video" in title else "practice_set"
        db.execute("INSERT INTO resources (id,kind,title,is_published) VALUES (?,?,?,1)",
                   (rid, kind, title))
        for k, w in cov.items():
            db.execute("INSERT INTO resource_skills VALUES (?,?,?)", (rid, k, w))

    for set_code, qs, key in (("A", qs_a, KEY), ("B", qs_b, KEY_B), ("C", qs_c, KEY_C)):
        for i in range(100):
            iid = C.item_id(i, set_code)
            bare = C.item_id(i)
            stem, opts = qs[bare]
            db.execute(
                "INSERT INTO items (id,module_id,position,set_code,primary_skill_id,category,stem,correct_option) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (iid, i // 10, i % 10 + 1, set_code, SKILL_OF[i], CATS[i], stem, key[i]))
            correct = key[i]
            for letter, body in opts.items():
                src = None if letter == correct else CATS[i]
                db.execute("INSERT INTO item_options (item_id,option_letter,body,error_source) VALUES (?,?,?,?)",
                           (iid, letter, body, src))

    people = [("a1", "admin", "Admin", "admin@sb.ph", "Admin1234", "👑"),
              ("t1", "tutor", "Ana Ayuban", "ana@sb.ph", "Demo1234", "🎓"),
              ("t2", "tutor", "Juan dela Cruz", "juan@sb.ph", "Demo1234", "🎓"),
              ("s1", "student", "Maria Santos", "maria@sb.ph", "Demo1234", "🦄"),
              ("s2", "student", "Paolo Garcia", "paolo@sb.ph", "Demo1234", "🐯")]
    for pid, role, name, email, pw, avatar in people:
        db.execute("INSERT INTO profiles (id,role,full_name,email,password_hash,avatar,is_verified) VALUES (?,?,?,?,?,?,1)",
                   (pid, role, name, email, hash_pw(pw), avatar))
    _seed_demo_attempts(db, "s1", seed_val=11, acc=0.62, modules=(0,))
    db.commit()
    refresh_dmap(db)


def _seed_demo_attempts(db, sid, seed_val, acc, modules=(0,)):
    M = 2147483647
    s = seed_val * 7919 % M
    answers = []
    for i in range(100):
        s = s * 16807 % M
        r = s / M
        s = s * 16807 % M
        t = s / M
        k = KEY[i]
        wrong = [x for x in "ABCD" if x != k]
        answers.append("-" if t < 0.03
                       else (k if r < acc
                             else wrong[min(2, int((r - acc) / (1 - acc) * 3))]))
    for m in modules:
        aid = str(uuid.uuid4())
        db.execute("INSERT INTO attempts (id, student_id, kind, status, module_id, started_at, submitted_at) VALUES (?,?,?,?,?,?,?)",
                   (aid, sid, "pretest", "submitted", m, now(), now()))
        for i in MOD_ITEMS[m]:
            iid = C.item_id(i)
            chosen = answers[i] if answers[i] != "-" else None
            db.execute("INSERT INTO responses (attempt_id, item_id, chosen, answered_at) VALUES (?,?,?,?)",
                       (aid, iid, chosen, now() if chosen else None))
        score_and_store(db, {"id": aid, "kind": "pretest", "module_id": m})


def user_from(db, handler):
    tok = handler.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    row = db.execute("SELECT p.* FROM sessions s JOIN profiles p ON p.id = s.user_id WHERE s.token = ? AND p.is_active = 1", (tok,)).fetchone()
    return dict(row) if row else None


def need_role(u, *roles):
    if u["role"] not in roles:
        raise ApiError(403, "this account cannot use this page")


def latest(db, sid, kind, m=None, status=None):
    sql, args = "SELECT * FROM attempts WHERE student_id = ? AND kind = ?", [sid, kind]
    if m is not None:
        sql += " AND module_id = ?"
        args.append(m)
    if status:
        sql += " AND status = ?"
        args.append(status)
    row = db.execute(sql + " ORDER BY started_at DESC, rowid DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def cur_attempt(db, sid):
    row = db.execute("SELECT * FROM attempts WHERE student_id = ? AND status = 'in_progress' ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    return dict(row) if row else None


def active_tutor(db, sid):
    return db.execute("SELECT t.tutor_id, p.full_name FROM tutor_assignments t JOIN profiles p ON p.id = t.tutor_id WHERE t.student_id = ? AND t.ended_at IS NULL AND p.is_active = 1", (sid,)).fetchone()


def answers_of(db, aid):
    ans = ["-"] * 100
    for r in db.execute("SELECT item_id, chosen FROM responses WHERE attempt_id = ? AND chosen IS NOT NULL", (aid,)):
        ans[C.item_index(r["item_id"])] = r["chosen"]
    return ans


def skill_rows(db, aid):
    return {r["skill_id"]: dict(r) for r in db.execute("SELECT * FROM skill_scores WHERE attempt_id = ?", (aid,))}


def attempt_items(db, aid):
    rows = db.execute("SELECT r.item_id, r.answered_at, i.module_id, i.stem, i.category, i.primary_skill_id "
                      "FROM responses r JOIN items i ON i.id = r.item_id WHERE r.attempt_id = ?", (aid,)).fetchall()
    rows = sorted(rows, key=lambda r: C.item_index(r["item_id"]))
    opts = {}
    ids = [r["item_id"] for r in rows]
    if ids:
        placeholders = ",".join("?" * len(ids))
        for o in db.execute(f"SELECT item_id, option_letter, body FROM item_options WHERE item_id IN ({placeholders})", ids):
            opts.setdefault(o["item_id"], {})[o["option_letter"]] = o["body"]
    return [{"id": r["item_id"], "module": r["module_id"], "module_name": MODS[r["module_id"]],
             "skill": SKILLS[r["primary_skill_id"]][0], "cat": r["category"], "stem": r["stem"],
             "options": {k: opts[r["item_id"]][k] for k in sorted(opts[r["item_id"]])},
             "done": r["answered_at"] is not None} for r in rows]


CURRENT_SCORING_VERSION = 1


def load_weights(db, version):
    rows = {r["category"]: r["weight"] for r in db.execute("SELECT category, weight FROM scoring_weights WHERE version = ?", (version,))}
    if set(rows) != {"F", "P", "C"}:
        raise RuntimeError(f"scoring_weights has no complete set for version {version}")
    return rows


def weights_status(db):
    table = load_weights(db, CURRENT_SCORING_VERSION)
    return {"version": CURRENT_SCORING_VERSION, "table": table, "engine": dict(WEIGHTS),
            "match": all(abs(table[c] - WEIGHTS[c]) < 1e-9 for c in WEIGHTS)}


def analysis(db, aid, weights=None):
    row = db.execute("SELECT scoring_version, kind FROM attempts WHERE id = ?", (aid,)).fetchone()
    if weights is None:
        weights = load_weights(db, row["scoring_version"] if row else CURRENT_SCORING_VERSION)
    kind = row["kind"] if row else "pretest"
    key = KEY_B if kind == "retest" else KEY
    dmap = DMAP if kind != "retest" else None
    return analyze(answers_of(db, aid), key, CATS, SKILLS, EDGES, MODS,
                   distractor_map=dmap, weights=weights)


def analyze_long_test(db, attempt_id):
    weights = load_weights(db, CURRENT_SCORING_VERSION)
    answers = ["-"] * 100
    for r in db.execute("SELECT item_id, chosen FROM long_test_responses "
                        "WHERE attempt_id = ? AND chosen IS NOT NULL", (attempt_id,)):
        answers[C.item_index(r["item_id"])] = r["chosen"]
    return analyze(answers, KEY_C, CATS, SKILLS, EDGES, MODS,
                   distractor_map=None, weights=weights)


def mod_need(db, aid, m):
    inside = [(a, b) for a, b in EDGES if SKILLS[a][1] == m and SKILLS[b][1] == m]
    res = analyze(answers_of(db, aid), KEY, CATS, SKILLS, inside, MODS, distractor_map=DMAP)
    return [v if SKILLS[k][1] == m else 0.0 for k, v in enumerate(res["need"])]


def mod_targets(need, m):
    return sorted([k for k in MOD_SKILLS[m] if need[k] > 0], key=lambda k: -need[k])[:2]


def score_and_store(db, att):
    db.execute("UPDATE attempts SET scoring_version = ? WHERE id = ?", (CURRENT_SCORING_VERSION, att["id"]))
    res = analysis(db, att["id"])
    touched = sorted({SKILL_OF[C.item_index(r["item_id"])] for r in db.execute("SELECT item_id FROM responses WHERE attempt_id = ?", (att["id"],))})
    for k in touched:
        s = res["sk"][k]
        db.execute("INSERT INTO skill_scores VALUES (?,?,?,?,?,?,?,?,?)",
                   (att["id"], k, round(s["P"], 2), len(SKILL_ITEMS[k]),
                    s["err"]["F"], s["err"]["P"], s["err"]["C"], s["err"]["U"], s["blank"]))
    if att["kind"] == "pretest":
        need = mod_need(db, att["id"], att["module_id"])
        for rank, r in enumerate(rank_resources(need, C.RESOURCES), 1):
            db.execute("INSERT INTO recommendations VALUES (?,?,?,?)",
                       (att["id"], rank, r["id"], round(r["cosine"], 4)))


def advice(k, before, after, target, resp, pre_rows):
    missed = [C.item_id(i) for i in SKILL_ITEMS[k] if resp.get(C.item_id(i)) != KEY[i]]
    weak_pre = [a for a, b in EDGES
                if b == k and SKILLS[a][1] == SKILLS[k][1] and pre_rows[a]["proficiency"] < 80]
    best = max((r for r in C.RESOURCES if k in r[2]), key=lambda r: r[2][k], default=None)
    if before >= 80:
        action = "Strong. Keep practicing."
    elif before >= 60:
        action = "Practice: " + C.FOCUS[k] + "."
    else:
        action = "Review first: " + C.FOCUS[k] + "."
    if weak_pre and before < 80:
        action = f"Check {SKILLS[weak_pre[0]][0]} first. " + action
    shown = after if after is not None else before
    return {"skill": SKILLS[k][0], "focus": C.FOCUS[k], "before": round(before, 1),
            "after": None if after is None else round(after, 1), "target": target,
            "band": band(shown), "missed": missed, "action": action,
            "resource": best[1] if best else None}


def evaluation(db, sid, blocks):
    done = [b for b in blocks if b["pretest"] == "submitted"]
    skills = sorted((dict(s, module=b["module"], module_name=b["name"]) for b in done for s in b["skills"]),
                    key=lambda s: s["after"] if s["after"] is not None else s["before"])
    cur = [s["after"] if s["after"] is not None else s["before"] for s in skills]
    last = db.execute("SELECT module_id FROM attempts WHERE student_id = ? AND status = 'submitted' ORDER BY submitted_at DESC, rowid DESC LIMIT 1", (sid,)).fetchone()
    return {"chapters_done": len(done), "updated_after": last["module_id"] if last else None,
            "course": round(mean(cur), 1) if cur else None, "skills": skills,
            "rollup": [{"module": b["module"], "name": b["name"], "pre": b.get("pre_avg"), "post": b.get("post_avg")} for b in done]}


def error_source_totals(db, sid):
    totals = {"F": 0, "P": 0, "C": 0, "U": 0}
    rows = db.execute("SELECT s.err_arith, s.err_proc, s.err_concept, s.err_unclassified FROM skill_scores s JOIN attempts a ON a.id = s.attempt_id WHERE a.student_id = ? AND a.status = 'submitted'", (sid,)).fetchall()
    for r in rows:
        totals["F"] += r["err_arith"] or 0
        totals["P"] += r["err_proc"] or 0
        totals["C"] += r["err_concept"] or 0
        totals["U"] += r["err_unclassified"] or 0
    total = sum(totals.values())
    pct = {k: round(v / total * 100, 1) if total else 0 for k, v in totals.items()}
    dominant = max(("F", "P", "C"), key=lambda k: totals[k]) if total else None
    labels = {"F": "Arithmetic", "P": "Procedural", "C": "Conceptual"}
    return {"counts": totals, "pct": pct, "total": total,
            "dominant": dominant, "dominant_label": labels.get(dominant, "—")}


def coverage_stats(db, sid):
    administered = db.execute("SELECT COUNT(*) FROM responses r JOIN attempts a ON a.id = r.attempt_id WHERE a.student_id = ?", (sid,)).fetchone()[0]
    answered = db.execute("SELECT COUNT(*) FROM responses r JOIN attempts a ON a.id = r.attempt_id WHERE a.student_id = ? AND r.chosen IS NOT NULL", (sid,)).fetchone()[0]
    pct = round(answered / administered * 100, 0) if administered else 0
    return {"administered": administered, "answered": answered, "pct": pct}


def student_history(db, sid):
    out = []
    for r in db.execute("SELECT a.id, a.kind, a.status, a.module_id, a.started_at, a.submitted_at, m.title AS module_title FROM attempts a LEFT JOIN modules m ON m.id = a.module_id WHERE a.student_id = ? ORDER BY COALESCE(a.submitted_at, a.started_at) DESC, a.rowid DESC", (sid,)):
        correct = db.execute("SELECT COUNT(*) FROM responses r JOIN items i ON i.id = r.item_id WHERE r.attempt_id = ? AND r.chosen IS NOT NULL AND r.chosen = i.correct_option", (r["id"],)).fetchone()[0]
        administered = db.execute("SELECT COUNT(*) FROM responses WHERE attempt_id = ?", (r["id"],)).fetchone()[0]
        answered = db.execute("SELECT COUNT(*) FROM responses WHERE attempt_id = ? AND chosen IS NOT NULL", (r["id"],)).fetchone()[0]
        pct = round(correct / administered * 100, 0) if administered else 0
        out.append({"id": r["id"], "kind": r["kind"], "status": r["status"],
                    "module": r["module_id"], "module_name": r["module_title"],
                    "started": r["started_at"], "submitted": r["submitted_at"],
                    "correct": correct, "administered": administered,
                    "answered": answered, "pct": pct})
    for r in db.execute("SELECT * FROM long_test_attempts WHERE student_id = ? ORDER BY COALESCE(submitted_at, started_at) DESC, rowid DESC", (sid,)):
        administered = r["administered_count"] or 0
        correct = r["correct_count"] or 0
        out.append({"id": r["id"], "kind": "longtest", "status": r["status"],
                    "scope": r["scope"], "module": None,
                    "module_name": "Long Test (Final Boss)",
                    "started": r["started_at"], "submitted": r["submitted_at"],
                    "correct": correct, "administered": administered,
                    "answered": administered,
                    "pct": round(correct / administered * 100, 0) if administered else 0})
    out.sort(key=lambda a: (a.get("submitted") or a.get("started") or ""), reverse=True)
    return out


def lesson_progress(db, sid, m):
    row = db.execute("SELECT * FROM lesson_progress WHERE student_id = ? AND module_id = ?", (sid, m)).fetchone()
    if row:
        return dict(row)
    return {"student_id": sid, "module_id": m, "lesson_done": 0,
            "simulation_done": 0, "practice_done": 0, "scenario_done": 0}


def all_lessons_done(db, sid):
    for m in range(10):
        if not lesson_progress(db, sid, m)["lesson_done"]:
            return False
    return True


def all_pretests_done(db, sid):
    for m in range(10):
        if not latest(db, sid, "pretest", m, "submitted"):
            return False
    return True


def long_test_unlocked(db, sid):
    return all_lessons_done(db, sid) and all_pretests_done(db, sid)


def mark_lesson_section(db, sid, m, section):
    if section not in ("lesson", "simulation", "practice", "scenario"):
        raise ApiError(400, "unknown section")
        
    row = db.execute("SELECT 1 FROM lesson_progress WHERE student_id=? AND module_id=?", (sid, m)).fetchone()
    if not row:
        db.execute("INSERT INTO lesson_progress (student_id, module_id) VALUES (?,?)", (sid, m))
        
    if section == "lesson":
        # Fix 15: The chat is the entire lesson, so mark all sub-sections complete.
        db.execute("UPDATE lesson_progress SET lesson_done = 1, simulation_done = 1, practice_done = 1, scenario_done = 1, updated_at = ? WHERE student_id = ? AND module_id = ?",
                   (now(), sid, m))
    else:
        col = section + "_done"
        db.execute(f"UPDATE lesson_progress SET {col} = 1, updated_at = ? WHERE student_id = ? AND module_id = ?",
                   (now(), sid, m))
                   
    audit(db, sid, "lesson_done", "lesson_progress", f"{sid}:{m}", {"section": section})
    db.commit()
    return lesson_progress(db, sid, m)


def long_test_state(db, sid):
    cur = db.execute("SELECT * FROM long_test_attempts WHERE student_id = ? AND status = 'in_progress' ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    last = db.execute("SELECT * FROM long_test_attempts WHERE student_id = ? AND status = 'submitted' ORDER BY submitted_at DESC, rowid DESC LIMIT 1", (sid,)).fetchone()
    unlocked = long_test_unlocked(db, sid)
    out = {"unlocked": unlocked, "attempt": None, "last": None,
           "perfect": False, "boss_defeated": False}
    if last:
        last_d = dict(last)
        tested = None
        if last_d.get("skills_tested"):
            try:
                tested = json.loads(last_d["skills_tested"])
            except Exception:
                pass
        out["last"] = {"id": last_d["id"], "scope": last_d["scope"],
                       "correct": last_d["correct_count"],
                       "administered": last_d["administered_count"],
                       "submitted_at": last_d["submitted_at"],
                       "skills_tested": tested}
                       
    # Fix 10: Boss is defeated if ANY submitted attempt was perfect.
    perfect_row = db.execute(
        "SELECT 1 FROM long_test_attempts WHERE student_id = ? AND status = 'submitted' "
        "AND correct_count IS NOT NULL AND administered_count > 0 "
        "AND correct_count = administered_count LIMIT 1", (sid,)).fetchone()
    out["boss_defeated"] = bool(perfect_row)
    
    if cur:
        cur_d = dict(cur)
        items = [r["item_id"] for r in db.execute("SELECT item_id FROM long_test_responses WHERE attempt_id = ? ORDER BY item_id", (cur_d["id"],))]
        resp = {r["item_id"]: r for r in db.execute("SELECT * FROM long_test_responses WHERE attempt_id = ?", (cur_d["id"],))}
        opts = {}
        if items:
            placeholders = ",".join("?" * len(items))
            for o in db.execute(f"SELECT item_id, option_letter, body FROM item_options WHERE item_id IN ({placeholders})", items):
                opts.setdefault(o["item_id"], {})[o["option_letter"]] = o["body"]
        items_out = [{"id": iid,
                      "module": C.item_index(iid) // 10,
                      "stem": db.execute("SELECT stem FROM items WHERE id = ?", (iid,)).fetchone()["stem"],
                      "options": {k: opts[iid][k] for k in sorted(opts[iid])},
                      "done": resp[iid]["answered_at"] is not None}
                     for iid in items]
        out["attempt"] = {"id": cur_d["id"], "scope": cur_d["scope"], "items": items_out}
    return out


def long_test_start(db, sid):
    if db.execute("SELECT 1 FROM long_test_attempts WHERE student_id = ? AND status = 'in_progress'", (sid,)).fetchone():
        raise ApiError(409, "finish current long test first")
    if not long_test_unlocked(db, sid):
        raise ApiError(403, "complete all lessons and 10 module tests first")
    last = db.execute("SELECT * FROM long_test_attempts WHERE student_id = ? AND status = 'submitted' ORDER BY submitted_at DESC, rowid DESC LIMIT 1", (sid,)).fetchone()
    scope, skills_tested, item_ids = "full", None, None
    if last:
        weak = _weak_skills_from_long(db, last["id"])
        if weak:
            scope, skills_tested = "weak", weak
            item_ids = sorted(i for k in weak for i in SKILL_ITEMS[k])
        else:
            scope = "full"
    if item_ids is None:
        item_ids = list(range(100))
    aid = str(uuid.uuid4())
    db.execute("INSERT INTO long_test_attempts (id, student_id, status, scope, parent_attempt_id, skills_tested, started_at) VALUES (?,?,?,?,?,?,?)",
               (aid, sid, "in_progress", scope,
                last["id"] if last else None,
                json.dumps(skills_tested) if skills_tested else None, now()))
    for i in item_ids:
        db.execute("INSERT INTO long_test_responses (attempt_id, item_id) VALUES (?,?)",
                   (aid, C.item_id(i, "C")))
    audit(db, sid, "longtest_start", "long_test", aid,
          {"scope": scope, "items": len(item_ids)})
    db.commit()
    return {"attempt": aid, "scope": scope, "items": len(item_ids)}


def _weak_skills_from_long(db, attempt_id):
    answers = ["-"] * 100
    tested = set()
    for r in db.execute("SELECT item_id, chosen FROM long_test_responses "
                        "WHERE attempt_id = ? AND chosen IS NOT NULL", (attempt_id,)):
        i = C.item_index(r["item_id"])
        answers[i] = r["chosen"]
        tested.add(i)
    for r in db.execute("SELECT item_id FROM long_test_responses WHERE attempt_id = ?", (attempt_id,)):
        tested.add(C.item_index(r["item_id"]))
    res = analyze(answers, KEY_C, CATS, SKILLS, EDGES, MODS, distractor_map=None)
    # Fix 11: Only flag skills that were actually in this test
    tested_skills = {SKILL_OF[i] for i in tested}
    return [k for k, s in enumerate(res["sk"]) if s["P"] < 80 and k in tested_skills]


def long_test_answer(db, sid, body):
    cur = db.execute("SELECT * FROM long_test_attempts WHERE student_id = ? AND status = 'in_progress' ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    if not cur:
        raise ApiError(409, "no long test in progress")
    item = body.get("item")
    chosen = None if body.get("skip") else body.get("chosen", body.get("letter"))
    if chosen not in (None, "A", "B", "C", "D"):
        raise ApiError(400, "answer must be A, B, C, D or null")
    row = db.execute("SELECT answered_at FROM long_test_responses WHERE attempt_id = ? AND item_id = ?", (cur["id"], item)).fetchone()
    if row is None:
        raise ApiError(400, "that question is not in this long test")
    if row["answered_at"]:
        raise ApiError(409, "already answered")
    db.execute("UPDATE long_test_responses SET chosen = ?, answered_at = ? WHERE attempt_id = ? AND item_id = ?",
               (chosen, now(), cur["id"], item))
    db.commit()
    return {"correct": None if chosen is None else chosen == KEY_C[C.item_index(item)]}


def long_test_submit(db, sid):
    cur = db.execute("SELECT * FROM long_test_attempts WHERE student_id = ? AND status = 'in_progress' ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    if not cur:
        raise ApiError(409, "no long test in progress")
    total = db.execute("SELECT COUNT(*) FROM long_test_responses WHERE attempt_id = ?", (cur["id"],)).fetchone()[0]
    answered = db.execute("SELECT COUNT(*) FROM long_test_responses WHERE attempt_id = ? AND chosen IS NOT NULL", (cur["id"],)).fetchone()[0]
    if not is_valid_attempt(answered, total):
        raise ApiError(400, f"answer at least half of the {total} questions first ({answered} answered so far)")
    answers = ["-"] * 100
    for r in db.execute("SELECT item_id, chosen FROM long_test_responses WHERE attempt_id = ? AND chosen IS NOT NULL", (cur["id"],)):
        answers[C.item_index(r["item_id"])] = r["chosen"]
    administered = [r["item_id"] for r in db.execute("SELECT item_id FROM long_test_responses WHERE attempt_id = ?", (cur["id"],))]
    correct = sum(1 for iid in administered if answers[C.item_index(iid)] == KEY_C[C.item_index(iid)])
    db.execute("UPDATE long_test_attempts SET status = 'submitted', submitted_at = ?, correct_count = ?, administered_count = ? WHERE id = ?",
               (now(), correct, len(administered), cur["id"]))
    audit(db, sid, "longtest_submit", "long_test", cur["id"],
          {"correct": correct, "administered": len(administered), "scope": cur["scope"]})
    db.commit()
    return {"id": cur["id"], "scope": cur["scope"], "correct": correct,
            "administered": len(administered),
            "perfect": correct == len(administered),
            "diagnostic": _long_test_diagnostic(db, sid, cur["id"])}


def _long_test_diagnostic(db, sid, attempt_id):
    administered = [r["item_id"] for r in db.execute("SELECT item_id FROM long_test_responses WHERE attempt_id = ?", (attempt_id,))]
    answers = ["-"] * 100
    for r in db.execute("SELECT item_id, chosen FROM long_test_responses WHERE attempt_id = ? AND chosen IS NOT NULL", (attempt_id,)):
        answers[C.item_index(r["item_id"])] = r["chosen"]
    res = analyze(answers, KEY_C, CATS, SKILLS, EDGES, MODS, distractor_map=None)
    tested_skills = sorted({SKILL_OF[C.item_index(iid)] for iid in administered})
    skills_rows = []
    for k in tested_skills:
        p = res["sk"][k]["P"]
        skills_rows.append({"code": C.skill_code(k), "skill": SKILLS[k][0],
                            "module": SKILLS[k][1], "module_name": MODS[SKILLS[k][1]],
                            "P": round(p, 1), "band": band(p)})
    module_rows = []
    for m in range(10):
        ks = [r for r in skills_rows if r["module"] == m]
        if not ks:
            continue
        avg = sum(r["P"] for r in ks) / len(ks)
        module_rows.append({"module": m, "name": MODS[m], "skills": len(ks),
                            "avg": round(avg, 1), "band": band(avg)})
    course_prof = round(sum(r["P"] for r in skills_rows) / len(skills_rows), 1) if skills_rows else 0
    weak = sorted([r for r in skills_rows if r["band"] != "Mastered"], key=lambda r: r["P"])
    err = {"F": 0, "P": 0, "C": 0, "U": 0}
    for k in tested_skills:
        for c in err:
            err[c] += res["sk"][k]["err"][c]
    total_err = sum(err.values())
    prof = [res["sk"][k]["P"] for k in range(len(SKILLS))]
    deficit = [max(0.0, 80.0 - p) if k in tested_skills else 0.0 for k, p in enumerate(prof)]
    children = {}
    for a, b in EDGES:
        children.setdefault(a, []).append(b)
    root = [deficit[k] + _long_influence(k, deficit, children) for k in range(len(SKILLS))]
    root_rows = []
    for k in tested_skills:
        if deficit[k] <= 0:
            continue
        root_rows.append({"skill": SKILLS[k][0], "P": round(prof[k], 1), "score": root[k]})
    root_rows.sort(key=lambda r: -r["score"])
    for r in root_rows:
        r["score"] = round(r["score"], 1)
    root_rows = root_rows[:5]
    need = [max(0.0, 80.0 - p) if k in tested_skills else 0.0 for k, p in enumerate(prof)]
    recs = [{"id": r["id"], "title": r["title"],
             "relevance": round(r["cosine"] * 100, 0)}
            for r in rank_resources(need, C.RESOURCES, top_n=5)]
    focus = [f"{r['skill']} ({r['P']}%)" for r in weak[:3]]
    return {"overall": {"course_proficiency": course_prof,
                        "skills_assessed": len(skills_rows),
                        "needs_review": sum(1 for r in skills_rows if r["band"] == "Needs Review"),
                        "weak_count": len(weak)},
            "skills": skills_rows, "modules": module_rows,
            "error_source": {"counts": err, "total": total_err},
            "root_cause": root_rows, "recommendations": recs,
            "weak_skills": [{"skill": r["skill"], "P": r["P"], "band": r["band"]}
                            for r in weak],
            "focus": focus}


def _long_influence(k, deficit, children):
    from collections import deque
    total, seen, queue = 0.0, set(), deque([(k, 0)])
    while queue:
        node, depth = queue.popleft()
        for child in children.get(node, ()):
            if child in seen:
                continue
            seen.add(child)
            total += deficit[child] * 0.5 ** (depth + 1)
            queue.append((child, depth + 1))
    return total


def module_block(db, sid, m):
    pre, ret = latest(db, sid, "pretest", m), latest(db, sid, "retest", m)
    d = {"module": m, "name": MODS[m],
         "pretest": pre["status"] if pre else "none",
         "retest": ret["status"] if ret else "none"}
    if pre and pre["status"] == "submitted":
        b = skill_rows(db, pre["id"])
        a = skill_rows(db, ret["id"]) if ret and ret["status"] == "submitted" else {}
        tg = mod_targets(mod_need(db, pre["id"], m), m)
        resp = {r["item_id"]: r["chosen"] for r in db.execute("SELECT item_id, chosen FROM responses WHERE attempt_id = ?", (pre["id"],))}
        d["skills"] = [dict(advice(k, b[k]["proficiency"],
                                   a[k]["proficiency"] if k in a else None,
                                   k in tg, resp, b),
                            subtopic=SUBTOPICS[SKILLS[k][2]][1]) for k in MOD_SKILLS[m]]
        d["targets"] = [SKILLS[k][0] for k in tg]
        d["pre_avg"] = round(mean(x["before"] for x in d["skills"]), 1)
        d["post_avg"] = round(mean(x["after"] if x["after"] is not None else x["before"] for x in d["skills"]), 1)
    return d


def student_tutor_comments(db, sid):
    rows = db.execute(
        "SELECT n.note, n.created_at, p.full_name AS tutor_name "
        "FROM session_notes n JOIN profiles p ON p.id = n.tutor_id "
        "WHERE n.student_id = ? ORDER BY n.created_at DESC LIMIT 30", (sid,)).fetchall()
    return [dict(r) for r in rows]


def state(db, u):
    sid = u["id"]
    cur, tut = cur_attempt(db, sid), active_tutor(db, sid)
    mods = [module_block(db, sid, m) for m in range(10)]
    for m in range(10):
        mods[m]["lesson"] = lesson_progress(db, sid, m)
    assigned = [dict(r) for r in db.execute(
        "SELECT a.*, m.title as module_name FROM assigned_retests a "
        "JOIN modules m ON m.id = a.module_id "
        "WHERE a.student_id = ? AND a.status = 'pending'", (sid,))]
    return {"name": u["full_name"], "avatar": u.get("avatar"), "email": u.get("email"),
            "is_verified": u.get("is_verified"),
            "tutor_id": tut["tutor_id"] if tut else None,
            "tutor": tut["full_name"] if tut else None,
            "modules": mods, "cleared": sum(b["pretest"] == "submitted" for b in mods),
            "evaluation": evaluation(db, sid, mods),
            "error_source": error_source_totals(db, sid),
            "coverage": coverage_stats(db, sid),
            "history": student_history(db, sid),
            "lessons_done": all_lessons_done(db, sid),
            "pretests_done": all_pretests_done(db, sid),
            "long_test": long_test_state(db, sid),
            "assigned_retests": assigned,
            "tutor_comments": student_tutor_comments(db, sid),
            "attempt": cur["id"] if cur else None,
            "kind": cur["kind"] if cur else None,
            "module": cur["module_id"] if cur else None,
            "items": attempt_items(db, cur["id"]) if cur else [],
            "progress": attempt_progress(db, cur["id"]) if cur else None,
            "practice": practice_summary(db, sid)}


def start(db, u, kind, m):
    sid = u["id"]
    if cur_attempt(db, sid):
        raise ApiError(409, "finish your current battle first")
    if not isinstance(m, int) or not 0 <= m <= 9:
        raise ApiError(400, "choose a module from 0 to 9")
    if kind == "pretest" and m > 0:
        if not latest(db, sid, "pretest", m - 1, "submitted"):
            raise ApiError(403, f"finish Chapter {m} first to unlock Chapter {m + 1}")
    if kind == "pretest":
        if latest(db, sid, "pretest", m):
            raise ApiError(409, "this module's pretest was already taken")
        ids, parent, set_code = MOD_ITEMS[m], None, "A"
    elif kind == "retest":
        pre = latest(db, sid, "pretest", m, "submitted")
        if not pre:
            raise ApiError(400, "finish this module's pretest first")
        tg = mod_targets(mod_need(db, pre["id"], m), m)
        if not tg:
            raise ApiError(400, "no skill in this module needs retaking")
        ids, parent, set_code = sorted(i for k in tg for i in SKILL_ITEMS[k]), pre["id"], "B"
    else:
        raise ApiError(400, "kind must be pretest or retest")
    aid = str(uuid.uuid4())
    tut = active_tutor(db, sid)
    db.execute("INSERT INTO attempts (id,student_id,kind,parent_attempt_id,module_id,tutor_id,started_at) VALUES (?,?,?,?,?,?,?)",
               (aid, sid, kind, parent, m, tut["tutor_id"] if tut else None, now()))
    db.executemany("INSERT INTO responses (attempt_id,item_id) VALUES (?,?)",
                   [(aid, C.item_id(i, set_code)) for i in ids])
    audit(db, sid, "start_" + kind, "attempt", aid,
          {"module": m, "items": len(ids), "set": set_code})
    db.commit()
    if kind == "retest":
        db.execute("UPDATE assigned_retests SET status = 'done' WHERE student_id = ? AND module_id = ? AND status = 'pending'", (sid, m))
        db.commit()
    return {"attempt": aid, "items": len(ids)}


def answer(db, u, body):
    cur = cur_attempt(db, u["id"])
    if not cur:
        raise ApiError(409, "no battle in progress")
    if not can_answer(u, cur):
        raise ApiError(403, "not your battle")
    item = body.get("item")
    chosen = None if body.get("skip") else body.get("chosen", body.get("letter"))
    if chosen not in (None, "A", "B", "C", "D"):
        raise ApiError(400, "answer must be A, B, C, D or null (skip)")
    row = db.execute("SELECT answered_at FROM responses WHERE attempt_id = ? AND item_id = ?", (cur["id"], item)).fetchone()
    if row is None:
        raise ApiError(400, "that question is not in this battle")
    if row["answered_at"]:
        raise ApiError(409, "already answered")
    db.execute("UPDATE responses SET chosen = ?, answered_at = ? WHERE attempt_id = ? AND item_id = ?",
               (chosen, now(), cur["id"], item))
    db.commit()
    key = KEY_B if cur["kind"] == "retest" else KEY
    return {"correct": None if chosen is None else chosen == key[C.item_index(item)],
            "progress": attempt_progress(db, cur["id"])}


def submit(db, u):
    cur = cur_attempt(db, u["id"])
    if not cur:
        raise ApiError(409, "no battle in progress")
    if not can_answer(u, cur):
        raise ApiError(403, "not your battle")
    n = db.execute("SELECT COUNT(*) FROM responses WHERE attempt_id = ? AND chosen IS NOT NULL", (cur["id"],)).fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM responses WHERE attempt_id = ?", (cur["id"],)).fetchone()[0]
    if not is_valid_attempt(n, total):
        raise ApiError(400, f"answer at least half of the {total} questions first ({n} answered so far)")
    db.execute("UPDATE attempts SET status = 'submitted', submitted_at = ? WHERE id = ?", (now(), cur["id"]))
    score_and_store(db, cur)
    audit(db, u["id"], "submit_" + cur["kind"], "attempt", cur["id"],
          {"module": cur["module_id"], "answered": n, "administered": total})
    db.commit()
    return {"status": "submitted", "answered": n, "total": total}


def choose_tutor(db, u, tid):
    if tid and not db.execute("SELECT 1 FROM profiles WHERE id = ? AND role = 'tutor' AND is_active = 1", (tid,)).fetchone():
        raise ApiError(400, "that tutor does not exist")
    db.execute("UPDATE tutor_assignments SET ended_at = ? WHERE student_id = ? AND ended_at IS NULL", (now(), u["id"]))
    if tid:
        db.execute("INSERT INTO tutor_assignments (id,student_id,tutor_id,started_at) VALUES (?,?,?,?)",
                   (str(uuid.uuid4()), u["id"], tid, now()))
    audit(db, u["id"], "choose_tutor", "profile", u["id"], {"tutor_id": tid})
    db.commit()
    return {"tutor_id": tid or None}


def rate_tutor(db, u, body):
    if u["role"] != "student":
        raise ApiError(403, "students only")
    retest_id = body.get("retest_id")
    stars = body.get("stars")
    comment = (body.get("comment") or "").strip()[:500]
    if not retest_id or not isinstance(stars, int) or not 1 <= stars <= 5:
        raise ApiError(400, "retest_id and stars (1-5) required")
        
    # Fix 14: Get the tutor_id from the attempt itself, not the current active tutor
    retest = db.execute(
        "SELECT tutor_id FROM attempts WHERE id = ? AND student_id = ? AND kind = 'retest' AND status = 'submitted'",
        (retest_id, u["id"])).fetchone()
    if not retest:
        raise ApiError(404, "no submitted retest found for you")
    if db.execute("SELECT 1 FROM tutor_ratings WHERE retest_id = ?", (retest_id,)).fetchone():
        raise ApiError(409, "you already rated this retest")
        
    tutor_id = retest["tutor_id"]
    if not tutor_id:
        tut = active_tutor(db, u["id"])
        tutor_id = tut["tutor_id"] if tut else None
        
    if not tutor_id:
        raise ApiError(400, "no tutor was assigned to this retest")
        
    db.execute("INSERT INTO tutor_ratings (id, student_id, tutor_id, retest_id, stars, comment) VALUES (?,?,?,?,?,?)",
               (str(uuid.uuid4()), u["id"], tutor_id, retest_id, stars, comment))
    audit(db, u["id"], "rate_tutor", "tutor_rating", retest_id,
          {"stars": stars, "tutor_id": tutor_id})
    db.commit()
    return {"ok": True}


def student_summary(db, s):
    sid = s["id"]
    tut = active_tutor(db, sid)
    blocks = [module_block(db, sid, m) for m in range(10)]
    done = [b for b in blocks if b["pretest"] == "submitted"]
    pre_all = [x["before"] for b in done for x in b["skills"]]
    gains = [x["after"] - x["before"] for b in blocks for x in b.get("skills", []) if x["after"] is not None]
    pending = [b for b in done if b["targets"] and b["retest"] == "none"]
    if not done:
        status = "Not started"
    elif len(done) < 10:
        status = f"In progress ({len(done)} of 10)"
    elif pending:
        status = "Not retaken"
    elif not gains:
        status = "No retake needed"
    else:
        g = mean(gains)
        status = "Improving" if g >= 10 else "Slight gain" if g > 0 else "No gain"
    rating = db.execute("SELECT AVG(stars) FROM tutor_ratings WHERE student_id = ?", (sid,)).fetchone()[0]
    brief_data = {"brief": "", "root_cause": [], "recommendations": [], "focus": []}
    if done:
        try:
            d = diagnostic_report(db, sid)
            brief_data = {"brief": d["brief"], "root_cause": d["root_cause"],
                          "recommendations": d["recommendations"],
                          "focus": [s["skill"] for s in d["skills"][:3]]}
        except Exception:
            pass
    notes = [dict(r) for r in db.execute("SELECT * FROM session_notes WHERE student_id = ? ORDER BY created_at DESC", (sid,))]
    return {"id": sid, "name": s["full_name"], "avatar": s.get("avatar"),
            "tutor_id": tut["tutor_id"] if tut else None,
            "tutor": tut["full_name"] if tut else None,
            "modules_done": len(done),
            "pretest": round(mean(pre_all), 1) if pre_all else None,
            "gain": round(mean(gains), 1) if gains else None,
            "status": status, "rating": round(rating, 1) if rating else None,
            "evaluation": evaluation(db, sid, blocks),
            "error_source": error_source_totals(db, sid),
            "coverage": coverage_stats(db, sid),
            "history": student_history(db, sid),
            "brief": brief_data["brief"],
            "root_cause": brief_data["root_cause"],
            "recommendations": brief_data["recommendations"],
            "focus": brief_data["focus"],
            "session_notes": notes,
            "practice": practice_summary(db, sid),
            "modules": [{"module": b["module"], "name": b["name"],
                         "status": "Retested" if b["retest"] == "submitted"
                                   else "Cleared" if b["pretest"] == "submitted"
                                   else "Not started",
                         "pre": b.get("pre_avg"), "post": b.get("post_avg")}
                        for b in blocks]}


def tutor_students(db, u):
    rows = db.execute("SELECT p.* FROM tutor_assignments t JOIN profiles p ON p.id = t.student_id WHERE t.tutor_id = ? AND t.ended_at IS NULL ORDER BY p.full_name", (u["id"],)).fetchall()
    return [student_summary(db, dict(r)) for r in rows]


def update_tutor_profile(db, u, body):
    name = (body.get("name") or "").strip()
    avatar = body.get("avatar") or u["avatar"]
    bio = body.get("bio") or ""
    subjects = body.get("subjects") or ""
    if not (2 <= len(name) <= 120):
        raise ApiError(400, "name must be 2 to 120 characters")
    db.execute("UPDATE profiles SET full_name = ?, avatar = ?, bio = ?, subjects = ? WHERE id = ?",
               (name, avatar, bio, subjects, u["id"]))
    audit(db, u["id"], "update_tutor_profile", "profile", u["id"],
          {"name": name, "bio": bio, "subjects": subjects})
    db.commit()
    return {"name": name, "avatar": avatar, "bio": bio, "subjects": subjects}


def add_session_note(db, u, body):
    student_id = body.get("student_id")
    note = (body.get("note") or "").strip()
    if not student_id or not note:
        raise ApiError(400, "student_id and note required")
    # Only a tutor assigned to the student may write a note about them.
    if not db.execute(
            "SELECT 1 FROM tutor_assignments "
            "WHERE student_id = ? AND tutor_id = ? AND ended_at IS NULL",
            (student_id, u["id"])).fetchone():
        raise ApiError(403, "this student is not assigned to you")
    db.execute("INSERT INTO session_notes (student_id, tutor_id, note) VALUES (?,?,?)",
               (student_id, u["id"], note))
    audit(db, u["id"], "add_session_note", "student", student_id, {"note": note})
    db.commit()
    return {"ok": True}


def assign_retest(db, u, body):
    student_id = body.get("student_id")
    module_id = body.get("module_id")
    if not student_id or not isinstance(module_id, int):
        raise ApiError(400, "student_id and module_id required")
    # Only a tutor assigned to the student may assign a retest.
    if not db.execute(
            "SELECT 1 FROM tutor_assignments "
            "WHERE student_id = ? AND tutor_id = ? AND ended_at IS NULL",
            (student_id, u["id"])).fetchone():
        raise ApiError(403, "this student is not assigned to you")
    db.execute("DELETE FROM assigned_retests WHERE student_id = ? AND module_id = ? AND status = 'pending'",
               (student_id, module_id))
    db.execute("INSERT INTO assigned_retests (student_id, tutor_id, module_id) VALUES (?,?,?)",
               (student_id, u["id"], module_id))
    audit(db, u["id"], "assign_retest", "student", student_id, {"module_id": module_id})
    db.commit()
    return {"ok": True}


def admin_overview(db):
    studs = [student_summary(db, dict(r)) for r in db.execute("SELECT * FROM profiles WHERE role = 'student' AND is_active = 1 ORDER BY full_name")]
    tutors, tutor_comments = [], {}
    for t in db.execute("SELECT id, full_name FROM profiles WHERE role = 'tutor' AND is_active = 1 ORDER BY full_name"):
        tutor_comments[t["id"]] = [dict(r) for r in db.execute(
            "SELECT r.stars, r.comment, r.created_at, p.full_name AS student FROM tutor_ratings r "
            "JOIN profiles p ON p.id = r.student_id WHERE r.tutor_id = ? ORDER BY r.created_at DESC",
            (t["id"],))]
        mine = [s for s in studs if s["tutor_id"] == t["id"]]
        g = [s["gain"] for s in mine if s["gain"] is not None]
        rate = db.execute("SELECT AVG(stars) FROM tutor_ratings WHERE tutor_id = ?", (t["id"],)).fetchone()[0]
        rating_count = db.execute("SELECT COUNT(*) FROM tutor_ratings WHERE tutor_id = ?", (t["id"],)).fetchone()[0]
        tutors.append({"id": t["id"], "name": t["full_name"], "students": len(mine),
                       "improved": sum(x >= 10 for x in g), "retaken": len(g),
                       "avg_gain": round(mean(g), 1) if g else None,
                       "rating": round(rate, 2) if rate else None,
                       "rating_count": rating_count})
    for x in tutors:
        x["comments"] = tutor_comments.get(x["id"], [])
    return {"students": studs, "tutors": tutors, "weights": weights_status(db)}


def admin_list_users(db):
    return [dict(r) for r in db.execute(
        "SELECT id, role, full_name, email, is_active, is_verified, created_at FROM profiles ORDER BY role, is_active DESC, full_name")]


def admin_create_user(db, admin_u, body):
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    role = body.get("role") or "student"
    password = body.get("password") or "Demo1234"
    if not (2 <= len(name) <= 120):
        raise ApiError(400, "name must be 2 to 120 characters")
    if "@" not in email or len(email) < 5:
        raise ApiError(400, "invalid email")
    if role not in ("student", "tutor", "admin"):
        raise ApiError(400, "invalid role")
    pw_err = validate_password_strength(password)
    if pw_err:
        raise ApiError(400, pw_err)
    if db.execute("SELECT 1 FROM profiles WHERE email = ?", (email,)).fetchone():
        raise ApiError(409, "email already registered")
    pid = str(uuid.uuid4())[:8]
    db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash, is_verified) VALUES (?,?,?,?,?,1)",
               (pid, role, name, email, hash_pw(password)))
    audit(db, admin_u["id"], "admin_create_user", "profile", pid, {"email": email, "role": role})
    db.commit()
    return {"id": pid}


def admin_update_user(db, admin_u, body):
    uid = body.get("id")
    name = (body.get("name") or "").strip()
    role = body.get("role")
    is_active = body.get("is_active")
    if not uid:
        raise ApiError(400, "user id required")
    if body.get("password"):
        pw_err = validate_password_strength(body["password"])
        if pw_err:
            raise ApiError(400, pw_err)
    if not (2 <= len(name) <= 120):
        raise ApiError(400, "name must be 2 to 120 characters")
    if role not in ("student", "tutor", "admin"):
        raise ApiError(400, "invalid role")
    db.execute("UPDATE profiles SET full_name = ?, role = ?, is_active = ? WHERE id = ?",
               (name, role, int(is_active), uid))
    if body.get("password"):
        db.execute("UPDATE profiles SET password_hash = ? WHERE id = ?", (hash_pw(body["password"]), uid))
    audit(db, admin_u["id"], "admin_update_user", "profile", uid,
          {"name": name, "role": role, "active": is_active})
    db.commit()
    return {"id": uid}


def admin_permanent_delete_user(db, admin_u, uid):
    if not uid:
        raise ApiError(400, "user id required")

    # sessions.user_id has no ON DELETE CASCADE, so remove sessions first.
    db.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    # resources.author_id and attempts.tutor_id are nullable but not cascaded.
    db.execute("UPDATE resources SET author_id = NULL WHERE author_id = ?", (uid,))
    db.execute("UPDATE attempts SET tutor_id = NULL WHERE tutor_id = ?", (uid,))
    # Explicit deletes; some would cascade from attempts but keep intent obvious.
    db.execute("DELETE FROM long_test_responses WHERE attempt_id IN "
               "(SELECT id FROM long_test_attempts WHERE student_id = ?)", (uid,))
    db.execute("DELETE FROM long_test_attempts WHERE student_id = ?", (uid,))
    db.execute("DELETE FROM responses WHERE attempt_id IN "
               "(SELECT id FROM attempts WHERE student_id = ?)", (uid,))
    db.execute("DELETE FROM skill_scores WHERE attempt_id IN "
               "(SELECT id FROM attempts WHERE student_id = ?)", (uid,))
    db.execute("DELETE FROM recommendations WHERE attempt_id IN "
               "(SELECT id FROM attempts WHERE student_id = ?)", (uid,))
    db.execute("DELETE FROM attempts WHERE student_id = ?", (uid,))
    db.execute("DELETE FROM tutor_assignments WHERE student_id = ? OR tutor_id = ?", (uid, uid))
    db.execute("DELETE FROM tutor_ratings WHERE student_id = ? OR tutor_id = ?", (uid, uid))
    db.execute("DELETE FROM session_notes WHERE student_id = ? OR tutor_id = ?", (uid, uid))
    db.execute("DELETE FROM assigned_retests WHERE student_id = ? OR tutor_id = ?", (uid, uid))
    db.execute("DELETE FROM expert_labels WHERE expert_id = ?", (uid,))
    db.execute("UPDATE audit_log SET actor_id = NULL WHERE actor_id = ?", (uid,))
    db.execute("DELETE FROM profiles WHERE id = ?", (uid,))

    audit(db, admin_u["id"], "admin_permanent_delete_user", "profile", uid, {})
    db.commit()
    return {"ok": True}


def admin_bulk_import_students(db, admin_u, body):
    try:
        rows = BI.parse_csv(body.get("csv"))
    except BI.BulkImportError as e:
        raise ApiError(400, str(e))
    existing = {r[0].lower() for r in db.execute("SELECT email FROM profiles")}
    results = BI.plan(rows, existing)
    if not body.get("commit"):
        return BI.summary(results, dry_run=True)
    pending = []
    for row, res in zip(rows, results):
        if res["status"] != "ready":
            continue
        generated = not row["password"]
        pw = BI.make_password() if generated else row["password"]
        pending.append((row, res, pw, generated, hash_pw(pw)))
    created_ids = []
    for row, res, pw, generated, pwhash in pending:
        pid = "s_" + secrets.token_hex(6)
        try:
            db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash, is_verified) VALUES (?,?,?,?,?,1)",
                       (pid, "student", row["name"], row["email"], pwhash))
        except sqlite3.IntegrityError as e:
            taken = db.execute("SELECT 1 FROM profiles WHERE email = ?", (row["email"],)).fetchone()
            res["status"] = "error"
            res["message"] = "email is already registered" if taken else "could not save this row: " + str(e)
            continue
        res["status"] = "created"
        res["id"] = pid
        if generated:
            res["password"] = pw
        created_ids.append(pid)
    if created_ids:
        audit(db, admin_u["id"], "admin_bulk_import", "profile", None,
              {"submitted": len(rows), "created": len(created_ids), "ids": created_ids})
        db.commit()
    return BI.summary(results, dry_run=False)


def admin_audit(db, limit=200, action=None, actor_id=None):
    sql = ("SELECT a.id, a.actor_id, a.action, a.entity, a.entity_id, a.detail, a.created_at, "
           "p.full_name AS actor_name, p.role AS actor_role "
           "FROM audit_log a LEFT JOIN profiles p ON p.id = a.actor_id WHERE 1=1")
    args = []
    if action:
        sql += " AND a.action = ?"
        args.append(action)
    if actor_id:
        sql += " AND a.actor_id = ?"
        args.append(actor_id)
    sql += " ORDER BY a.id DESC LIMIT ?"
    args.append(int(limit))
    rows = db.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except Exception:
                pass
        out.append(d)
    actions = [r["action"] for r in db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return {"entries": out, "actions": actions}


def admin_list_items(db):
    # Only Set A items are taggable — Set B/C are scored with no distractor map.
    items = {}
    for r in db.execute("SELECT id, module_id, position, category, stem, primary_skill_id, correct_option "
                        "FROM items WHERE set_code = 'A' ORDER BY module_id, position"):
        items[r["id"]] = {"id": r["id"], "module": r["module_id"],
                          "module_name": MODS[r["module_id"]], "position": r["position"],
                          "category": r["category"], "stem": r["stem"],
                          "skill": SKILLS[r["primary_skill_id"]][0],
                          "correct_option": r["correct_option"], "options": []}
    for r in db.execute("SELECT o.item_id, o.option_letter, o.body, o.error_source "
                        "FROM item_options o JOIN items i ON i.id = o.item_id "
                        "WHERE i.set_code = 'A' ORDER BY o.item_id, o.option_letter"):
        if r["item_id"] in items:
            items[r["item_id"]]["options"].append({"letter": r["option_letter"],
                                                   "body": r["body"],
                                                   "error_source": r["error_source"]})
    return list(items.values())


def admin_tag_option(db, u, iid, letter, source):
    if source not in (None, "F", "P", "C"):
        raise ApiError(400, "source must be F, P, C, or null")
    row = db.execute(
        "SELECT 1 FROM item_options o JOIN items i ON i.id = o.item_id "
        "WHERE o.item_id = ? AND o.option_letter = ? AND i.set_code = 'A'",
        (iid, letter)).fetchone()
    if not row:
        raise ApiError(404, "no such pretest option")
    db.execute("UPDATE item_options SET error_source = ? WHERE item_id = ? AND option_letter = ?",
               (source, iid, letter))
    audit(db, u["id"], "tag_distractor", "item_options", f"{iid}:{letter}", {"source": source})
    db.commit()
    refresh_dmap(db)
    return {"item_id": iid, "letter": letter, "error_source": source}


def _check_acyclic(edges):
    children = {}
    for a, b in edges:
        children.setdefault(a, []).append(b)
    nodes = set(children.keys()) | {c for v in children.values() for c in v}
    color = {n: 0 for n in nodes}
    stack = []

    def dfs(n):
        color[n] = 1
        stack.append(n)
        for c in children.get(n, ()):
            if color.get(c, 0) == 1:
                return stack[stack.index(c):] + [c]
            if color.get(c, 0) == 0:
                r = dfs(c)
                if r:
                    return r
        stack.pop()
        color[n] = 2
        return None

    for n in nodes:
        if color[n] == 0:
            r = dfs(n)
            if r:
                return False, r
    return True, None


def admin_list_graph(db):
    skills = [{"id": r["id"], "module": r["module_id"],
               "module_name": MODS[r["module_id"]],
               "subtopic": r["subtopic_id"],
               "subtopic_name": SUBTOPICS[r["subtopic_id"]][1],
               "name": r["name"], "code": C.skill_code(r["id"])}
              for r in db.execute("SELECT id, module_id, subtopic_id, name FROM skills ORDER BY id")]
    edges = [{"from": r["requires_skill_id"], "to": r["skill_id"]}
             for r in db.execute("SELECT skill_id, requires_skill_id FROM skill_prerequisites ORDER BY requires_skill_id, skill_id")]
    modules = [{"id": i, "name": name} for i, name in enumerate(MODS)]
    subtopics = [{"id": i, "module": mod, "title": title}
                 for i, (mod, title) in enumerate(SUBTOPICS)]
    return {"skills": skills, "edges": edges, "modules": modules, "subtopics": subtopics}


def admin_add_edge(db, u, a, b):
    if not isinstance(a, int) or not isinstance(b, int):
        raise ApiError(400, "from and to must be integers")
    if a == b:
        raise ApiError(400, "a skill cannot be its own prerequisite")
    if not db.execute("SELECT 1 FROM skills WHERE id = ?", (a,)).fetchone():
        raise ApiError(404, f"skill {a} does not exist")
    if not db.execute("SELECT 1 FROM skills WHERE id = ?", (b,)).fetchone():
        raise ApiError(404, f"skill {b} does not exist")
    if db.execute("SELECT 1 FROM skill_prerequisites WHERE skill_id = ? AND requires_skill_id = ?", (b, a)).fetchone():
        raise ApiError(409, "that edge already exists")
    db.execute("INSERT INTO skill_prerequisites VALUES (?,?)", (b, a))
    rows = db.execute("SELECT skill_id, requires_skill_id FROM skill_prerequisites").fetchall()
    edges = [(r["requires_skill_id"], r["skill_id"]) for r in rows]
    ok, cycle = _check_acyclic(edges)
    if not ok:
        db.execute("DELETE FROM skill_prerequisites WHERE skill_id = ? AND requires_skill_id = ?", (b, a))
        db.rollback()
        names = [SKILLS[k][0] for k in (cycle or [])]
        raise ApiError(400, "that edge would create a cycle: " + " → ".join(names))
    audit(db, u["id"], "graph_add_edge", "skill_prerequisites", f"{a}->{b}", {"from": a, "to": b})
    db.commit()
    return {"from": a, "to": b}


def admin_remove_edge(db, u, a, b):
    row = db.execute("SELECT 1 FROM skill_prerequisites WHERE skill_id = ? AND requires_skill_id = ?", (b, a)).fetchone()
    if not row:
        raise ApiError(404, "no such edge")
    db.execute("DELETE FROM skill_prerequisites WHERE skill_id = ? AND requires_skill_id = ?", (b, a))
    audit(db, u["id"], "graph_remove_edge", "skill_prerequisites", f"{a}->{b}", {"from": a, "to": b})
    db.commit()
    return {"from": a, "to": b}


def admin_validation_get(db):
    sql = """
        SELECT a.id as attempt_id, a.student_id, p.full_name as student_name,
               r.item_id, r.chosen, i.correct_option, i.category as item_cat, i.stem,
               o.body as chosen_text
        FROM responses r
        JOIN attempts a ON a.id = r.attempt_id
        JOIN profiles p ON p.id = a.student_id
        JOIN items i ON i.id = r.item_id
        LEFT JOIN item_options o ON o.item_id = r.item_id AND o.option_letter = r.chosen
        WHERE a.status = 'submitted' AND r.chosen IS NOT NULL AND r.chosen != i.correct_option
        ORDER BY a.submitted_at DESC LIMIT 50
    """
    rows = db.execute(sql).fetchall()
    out = []
    for r in rows:
        sys_label = DMAP.get((C.item_index(r["item_id"]), r["chosen"])) or r["item_cat"] or "U"
        expert = db.execute("SELECT expert_label FROM expert_labels WHERE attempt_id = ? AND item_id = ?",
                            (r["attempt_id"], r["item_id"])).fetchone()
        out.append({"attempt_id": r["attempt_id"], "student_name": r["student_name"],
                    "item_id": r["item_id"], "stem": r["stem"], "chosen": r["chosen"],
                    "chosen_text": r["chosen_text"], "system_label": sys_label,
                    "expert_label": expert["expert_label"] if expert else None})
    return out


def admin_validation_save(db, u, body):
    aid = body.get("attempt_id")
    iid = body.get("item_id")
    chosen = body.get("chosen")
    sys_label = body.get("system_label")
    expert_label = body.get("expert_label")
    if expert_label not in ("F", "P", "C", "U"):
        raise ApiError(400, "Invalid label")
    db.execute("INSERT OR REPLACE INTO expert_labels (attempt_id, item_id, chosen, expert_id, expert_label, system_label) VALUES (?,?,?,?,?,?)",
               (aid, iid, chosen, u["id"], expert_label, sys_label))
    audit(db, u["id"], "validation_save", "expert_label", f"{aid}:{iid}",
          {"label": expert_label, "system": sys_label})
    db.commit()
    return {"ok": True}


def admin_validation_kappa(db):
    rows = db.execute("SELECT system_label, expert_label FROM expert_labels").fetchall()
    if not rows:
        return {"kappa": 0, "agreement": 0, "total": 0}
    total = len(rows)
    agree = sum(1 for r in rows if r["system_label"] == r["expert_label"])
    po = agree / total
    sys_counts = {}
    exp_counts = {}
    for r in rows:
        sys_counts[r["system_label"]] = sys_counts.get(r["system_label"], 0) + 1
        exp_counts[r["expert_label"]] = exp_counts.get(r["expert_label"], 0) + 1
    labels = set(sys_counts.keys()) | set(exp_counts.keys())
    pe = sum((sys_counts.get(l, 0) / total) * (exp_counts.get(l, 0) / total) for l in labels)
    kappa = (po - pe) / (1 - pe) if (1 - pe) > 0 else 0
    return {"kappa": round(kappa, 3), "agreement": round(po * 100, 1), "total": total}


def _downstream_influence(k, deficit, children):
    from collections import deque
    total, seen, queue = 0.0, set(), deque([(k, 0)])
    while queue:
        node, depth = queue.popleft()
        for child in children.get(node, ()):
            if child in seen:
                continue
            seen.add(child)
            total += deficit[child] * 0.5 ** (depth + 1)
            queue.append((child, depth + 1))
    return total


def diagnostic_report(db, sid):
    student = db.execute("SELECT id, full_name FROM profiles WHERE id = ?", (sid,)).fetchone()
    if not student:
        raise ApiError(404, "student not found")
    pretests, retests = {}, {}
    for m in range(10):
        p = latest(db, sid, "pretest", m, "submitted")
        r = latest(db, sid, "retest", m, "submitted")
        if p:
            pretests[m] = p
        if r:
            retests[m] = r
    active = {}
    for m, att in pretests.items():
        for k, row in skill_rows(db, att["id"]).items():
            active[k] = row["proficiency"]
    for m, att in retests.items():
        for k, row in skill_rows(db, att["id"]).items():
            active[k] = row["proficiency"]
    item_answer = {}
    administered_items = set()
    for m in range(10):
        for att in (pretests.get(m), retests.get(m)):
            if not att:
                continue
            for r in db.execute("SELECT item_id, chosen FROM responses WHERE attempt_id = ?", (att["id"],)):
                administered_items.add(r["item_id"])
                if r["chosen"] is not None:
                    item_answer[r["item_id"]] = r["chosen"]
    total_administered = len(administered_items)
    total_correct = sum(1 for iid, chosen in item_answer.items() if chosen == KEY[C.item_index(iid)])
    err_totals = {"F": 0, "P": 0, "C": 0, "U": 0}
    for m in range(10):
        for att in (pretests.get(m), retests.get(m)):
            if not att:
                continue
            for r in db.execute("SELECT err_arith, err_proc, err_concept, err_unclassified FROM skill_scores WHERE attempt_id = ?", (att["id"],)):
                err_totals["F"] += r["err_arith"]
                err_totals["P"] += r["err_proc"]
                err_totals["C"] += r["err_concept"]
                err_totals["U"] += r["err_unclassified"]
    err_total = sum(err_totals.values())
    err_pct = {k: (v / err_total * 100 if err_total else 0) for k, v in err_totals.items()}
    dominant = max(("F", "P", "C"), key=lambda k: err_totals[k]) if err_total else None
    dominant_label = {"F": "Arithmetic", "P": "Procedural", "C": "Conceptual"}.get(dominant, "—")
    skills_rows = []
    for k in range(len(SKILLS)):
        if k not in active:
            continue
        p = active[k]
        skills_rows.append({"code": C.skill_code(k), "skill": SKILLS[k][0],
                            "module": SKILLS[k][1], "module_name": MODS[SKILLS[k][1]],
                            "P": round(p, 1), "band": band(p)})
    module_rows = []
    for m in range(10):
        ks = [r for r in skills_rows if r["module"] == m]
        if not ks:
            continue
        avg = sum(r["P"] for r in ks) / len(ks)
        module_rows.append({"module": m, "name": MODS[m], "skills": len(ks),
                            "avg": round(avg, 1), "band": band(avg)})
    course_prof = round(sum(r["P"] for r in skills_rows) / len(skills_rows), 1) if skills_rows else None
    needs_review = sum(1 for r in skills_rows if r["band"] == "Needs Review")
    prof = [active.get(k, 100.0) for k in range(len(SKILLS))]
    deficit = [max(0.0, 80.0 - p) for p in prof]
    children = {}
    for a, b in EDGES:
        children.setdefault(a, []).append(b)
    influence = [_downstream_influence(k, deficit, children) for k in range(len(SKILLS))]
    need = [deficit[k] + 0.5 * influence[k] for k in range(len(SKILLS))]
    root = [deficit[k] + influence[k] for k in range(len(SKILLS))]
    root_rows = []
    for k in range(len(SKILLS)):
        if deficit[k] <= 0 or root[k] <= 0:
            continue
        best_dep, best_edge = None, 0.0
        for a, b in EDGES:
            if a != k:
                continue
            edge_inf = deficit[b] * 0.5
            if edge_inf > best_edge:
                best_edge, best_dep = edge_inf, b
        root_rows.append({"rank_score": root[k], "skill": SKILLS[k][0],
                          "P": round(prof[k], 1),
                          "dependent": SKILLS[best_dep][0] if best_dep is not None else None,
                          "dependent_P": round(prof[best_dep], 1) if best_dep is not None else None,
                          "note": ("Weak prerequisite is limiting the dependent skill."
                                   if best_dep is not None
                                   else "Weak skill with no downstream dependent recorded.")})
    root_rows.sort(key=lambda r: -r["rank_score"])
    root_rows = root_rows[:5]
    max_root = root_rows[0]["rank_score"] if root_rows else 1.0
    for r in root_rows:
        r["rank_score"] = round(r["rank_score"] / max_root * 100, 1)
    recs = []
    weak_skill_names = [r["skill"] for r in skills_rows if r["P"] < 80]
    for r in rank_resources(need, C.RESOURCES, top_n=6):
        rid = r["id"]
        title, coverage = RESOURCE_BY_ID.get(rid, (r["title"], {}))
        covers = sorted(C.skill_code(k) for k in coverage.keys())
        matched = [SKILLS[k][0] for k in coverage.keys() if SKILLS[k][0] in weak_skill_names]
        explanation = (f"Targets your weak skills: {', '.join(matched)}."
                       if matched else "Recommended for general improvement.")
        recs.append({"id": rid, "title": title, "cosine": r["cosine"],
                     "covers": covers, "explanation": explanation,
                     "kind": "video" if "video" in title.lower() else "practice"})
    if recs:
        top = recs[0]["cosine"] or 1.0
        for r in recs:
            r["relevance"] = round(r["cosine"] / top * 100, 0)
    else:
        for r in recs:
            r["relevance"] = 0
    focus_skills = [f"{r['skill']} ({r['P']}%)" for r in sorted(skills_rows, key=lambda x: x["P"])[:3]]
    suggested = {"F": "Focus on arithmetic accuracy. Use drills and check each calculation.",
                 "P": "Practice the procedure step-by-step. Talk through the formula before substituting values.",
                 "C": "Discuss the concept first. Use worked examples and ask the tutee to explain in their own words."}.get(dominant, "Run a mixed diagnostic review to locate the source of difficulty.")
    prereq_check = [r["skill"] for r in root_rows[:2]] or ["(no prerequisite risk detected)"]
    brief_lines = ["=" * 46, "PEER TUTOR BRIEF — WHSG-DRA",
                   f"Student: {student['full_name']}", "=" * 46, "",
                   "FOCUS SKILLS (Priority 1-3):",
                   *[f"  • {s}" for s in focus_skills], "",
                   f"DOMINANT ERROR TYPE: {dominant_label} ({err_totals.get(dominant, 0)} errors)", "",
                   "SUGGESTED APPROACH:", f"  {suggested}", "",
                   "PREREQUISITE CHECK:",
                   *[f"  ⚠ Verify mastery of: {s}" for s in prereq_check], "",
                   "RECOMMENDED RESOURCES:",
                   *[f"  {i+1}. {r['title']} ({r['id']})" for i, r in enumerate(recs[:3])], "",
                   "SESSION PLAN:", "  1. Review prerequisite skill (if any)",
                   "  2. Walk through the target formula step-by-step",
                   "  3. Guided practice (3 problems)",
                   "  4. Independent practice (2 problems)",
                   "  5. Assign the top-ranked resource for homework", "", "=" * 46]
    brief = "\n".join(brief_lines)
    return {"student": {"id": student["id"], "name": student["full_name"]},
            "overall": {"total_score": total_correct,
                        "total_administered": total_administered,
                        "total_pct": round(total_correct / total_administered * 100, 0) if total_administered else 0,
                        "course_proficiency": course_prof,
                        "skills_assessed": len(skills_rows),
                        "needs_review": needs_review},
            "error_source": {"counts": err_totals,
                             "pct": {k: round(v, 1) for k, v in err_pct.items()},
                             "dominant": dominant, "dominant_label": dominant_label},
            "skills": skills_rows, "modules": module_rows,
            "root_cause": root_rows, "recommendations": recs, "brief": brief}


def demo_students(db):
    return [{"id": r["id"], "name": r["full_name"]}
            for r in db.execute("SELECT id, full_name FROM profiles WHERE role = 'student' AND is_active = 1 ORDER BY full_name")]


def sensitivity_report(db, sid):
    pretests, retests = {}, {}
    for m in range(10):
        p = latest(db, sid, "pretest", m, "submitted")
        r = latest(db, sid, "retest", m, "submitted")
        if p:
            pretests[m] = p
        if r:
            retests[m] = r
    if not pretests:
        return {"has_data": False}

    # Build a merged answer array with the correct key per item.
    # Retest answers must be scored against KEY_B, pretest against KEY.
    ans = ["-"] * 100
    merged_key = list(KEY)
    tested_idx = set()

    for m in range(10):
        if m not in pretests:
            continue
        for r in db.execute("SELECT item_id, chosen FROM responses WHERE attempt_id = ?",
                            (pretests[m]["id"],)):
            i = C.item_index(r["item_id"])
            tested_idx.add(i)
            if r["chosen"] is not None:
                ans[i] = r["chosen"]

    for m in range(10):
        if m not in retests:
            continue
        for r in db.execute("SELECT item_id, chosen FROM responses WHERE attempt_id = ?",
                            (retests[m]["id"],)):
            i = C.item_index(r["item_id"])
            tested_idx.add(i)
            merged_key[i] = KEY_B[i]
            ans[i] = r["chosen"] if r["chosen"] is not None else "-"

    res_default = analyze(ans, merged_key, CATS, SKILLS, EDGES, MODS, distractor_map=DMAP)
    eq = {"F": 1 / 3, "P": 1 / 3, "C": 1 / 3}
    res_equal = analyze(ans, merged_key, CATS, SKILLS, EDGES, MODS,
                        distractor_map=DMAP, weights=eq)
    fh = {"F": 0.6, "P": 0.2, "C": 0.2}
    res_fheavy = analyze(ans, merged_key, CATS, SKILLS, EDGES, MODS,
                         distractor_map=DMAP, weights=fh)

    tested_skills = sorted({SKILL_OF[i] for i in tested_idx})
    rows = []
    for k in tested_skills:
        rows.append({"skill": SKILLS[k][0],
                     "default": round(res_default["sk"][k]["P"], 1),
                     "equal": round(res_equal["sk"][k]["P"], 1),
                     "f_heavy": round(res_fheavy["sk"][k]["P"], 1),
                     "delta_equal": round(res_equal["sk"][k]["P"] - res_default["sk"][k]["P"], 1),
                     "delta_f_heavy": round(res_fheavy["sk"][k]["P"] - res_default["sk"][k]["P"], 1)})
    rows.sort(key=lambda r: -abs(r["delta_equal"]))
    return {"has_data": True, "skills": rows}


AVATAR_CHOICES = ["🎓", "🎒", "📚", "💡", "🧙", "🦸", "🧚", "🐯",
                  "🐼", "🦊", "🐸", "🦁", "🐨", "🐰", "🐧", "🦄"]


def _throttle_error(wait, what):
    mins = max(1, math.ceil(wait / 60))
    return ApiError(429, f"{what} Try again in {mins} minute{'s' if mins != 1 else ''}.")


def login(db, body, addr=""):
    email = (body.get("email") or "").strip()
    wait = LOGIN_ADDRESS_LIMIT.attempt("addr:" + addr)
    if wait:
        raise _throttle_error(wait, "Too many sign-in attempts from this device.")
    account_key = "acct:" + email.lower()
    wait = LOGIN_ACCOUNT_LIMIT.attempt(account_key)
    if wait:
        raise _throttle_error(wait, "Too many sign-in attempts for this account.")
    row = db.execute("SELECT * FROM profiles WHERE email = ? AND is_active = 1", (email,)).fetchone()
    if not row or not check_pw(body.get("password") or "", row["password_hash"]):
        raise ApiError(401, "email or password does not match")
    LOGIN_ACCOUNT_LIMIT.reset(account_key)
    tok = secrets.token_urlsafe(24)
    db.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)", (tok, row["id"], now()))
    audit(db, row["id"], "login", "session", tok, {"email": row["email"], "role": row["role"]})
    db.commit()
    return {"token": tok, "id": row["id"], "name": row["full_name"],
            "role": row["role"], "avatar": row["avatar"], "email": row["email"],
            "bio": row["bio"], "subjects": row["subjects"],
            "is_verified": row["is_verified"]}


def register(db, body, addr=""):
    wait = REGISTER_ADDRESS_LIMIT.attempt("addr:" + addr)
    if wait:
        raise _throttle_error(wait, "Too many sign-up attempts from this device.")
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    avatar = (body.get("avatar") or "🎓").strip()
    role = body.get("role") or "student"
    if role not in ("student", "tutor"):
        raise ApiError(400, "role must be student or tutor")
    if not (2 <= len(name) <= 120):
        raise ApiError(400, "name must be 2 to 120 characters")
    if "@" not in email or len(email) < 5:
        raise ApiError(400, "please enter a valid email address")
    pw_err = validate_password_strength(password)
    if pw_err:
        raise ApiError(400, pw_err)
    if avatar not in AVATAR_CHOICES:
        raise ApiError(400, "pick an avatar from the list")
    if db.execute("SELECT 1 FROM profiles WHERE email = ?", (email,)).fetchone():
        raise ApiError(409, "that email is already registered — try signing in")
    pid = ("t_" if role == "tutor" else "s_") + secrets.token_hex(6)
    db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash, avatar, is_verified) VALUES (?,?,?,?,?,?,1)",
               (pid, role, name, email, hash_pw(password), avatar))
    tok = secrets.token_urlsafe(24)
    db.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)", (tok, pid, now()))
    audit(db, pid, "register", "profile", pid,
          {"email": email, "avatar": avatar, "role": role})
    db.commit()
    return {"token": tok, "id": pid, "name": name, "role": role,
            "avatar": avatar, "email": email, "is_verified": 1,
            "message": "Account created."}


def verify_email(db, token):
    row = db.execute("SELECT id FROM profiles WHERE verification_token = ?", (token,)).fetchone()
    if not row:
        raise ApiError(400, "invalid or expired token")
    db.execute("UPDATE profiles SET is_verified = 1, verification_token = NULL WHERE id = ?", (row["id"],))
    db.commit()
    return {"ok": True, "message": "Email verified successfully."}


def update_profile(db, u, body):
    name = (body.get("name") or "").strip()
    avatar = (body.get("avatar") or "").strip()
    if not (2 <= len(name) <= 120):
        raise ApiError(400, "name must be 2 to 120 characters")
    if avatar not in AVATAR_CHOICES:
        raise ApiError(400, "pick an avatar from the list")
    db.execute("UPDATE profiles SET full_name = ?, avatar = ? WHERE id = ?",
               (name, avatar, u["id"]))
    audit(db, u["id"], "update_profile", "profile", u["id"],
          {"name": name, "avatar": avatar})
    db.commit()
    return {"id": u["id"], "name": name, "role": u["role"], "avatar": avatar}


def delete_account(db, u):
    # Fix 12: Append a suffix to the email to free it up for re-registration.
    db.execute("UPDATE profiles SET is_active = 0, email = email || '.deleted.' || strftime('%s','now') WHERE id = ?", (u["id"],))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
    
    # Fix 13: End any active tutor assignments so students don't see a deleted tutor.
    if u["role"] == "tutor":
        db.execute("UPDATE tutor_assignments SET ended_at = ? WHERE tutor_id = ? AND ended_at IS NULL",
                   (now(), u["id"]))
                   
    audit(db, u["id"], "delete_account", "profile", u["id"], {})
    db.commit()
    return {"ok": True}


def attempt_progress(db, aid):
    total = db.execute("SELECT COUNT(*) FROM responses WHERE attempt_id = ?", (aid,)).fetchone()[0]
    answered = db.execute("SELECT COUNT(*) FROM responses WHERE attempt_id = ? AND chosen IS NOT NULL", (aid,)).fetchone()[0]
    return {"answered": answered, "total": total, "needed": -(-total // 2)}


def practice_summary(db, sid):
    rows = db.execute("SELECT i.module_id AS module, COUNT(*) AS answered, SUM(p.is_correct) AS correct "
                      "FROM practice_responses p JOIN items i ON i.id = p.item_id "
                      "WHERE p.student_id = ? GROUP BY i.module_id ORDER BY i.module_id", (sid,)).fetchall()
    return [{"module": r["module"], "name": MODS[r["module"]],
             "answered": r["answered"], "correct": r["correct"] or 0} for r in rows]


def practice_answer(db, u, body):
    item, chosen = body.get("item"), body.get("chosen")
    if not isinstance(item, str):
        raise ApiError(400, "unknown practice item")
    row = db.execute("SELECT module_id FROM items WHERE id = ?", (item,)).fetchone()
    if not row:
        raise ApiError(400, "unknown practice item")
    if chosen not in ("A", "B", "C", "D"):
        raise ApiError(400, "answer must be A, B, C or D")
    # Practice Mode must obey the same chapter lock as the assessment.
    m = row["module_id"]
    if m > 0 and not latest(db, u["id"], "pretest", m - 1, "submitted"):
        raise ApiError(403, f"finish Chapter {m} first to unlock Chapter {m + 1}")
    correct = chosen == KEY[C.item_index(item)]
    db.execute("INSERT INTO practice_responses (student_id, item_id, chosen, is_correct) VALUES (?,?,?,?)",
               (u["id"], item, chosen, int(correct)))
    db.commit()
    return {"correct": correct}


def staff_sensitivity(db, u, sid):
    if not sid:
        raise ApiError(400, "student_id required")
    if not db.execute("SELECT 1 FROM profiles WHERE id = ? AND role = 'student'", (sid,)).fetchone():
        raise ApiError(404, "no such student")
    if u["role"] == "tutor" and not db.execute(
            "SELECT 1 FROM tutor_assignments WHERE student_id = ? AND tutor_id = ? AND ended_at IS NULL",
            (sid, u["id"])).fetchone():
        raise ApiError(403, "this student is not assigned to you")
    return sensitivity_report(db, sid)


def logout(db, token):
    """Invalidate a server-side session. Idempotent."""
    if token:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        db.commit()
    return {"ok": True}


def _parse_query(query):
    params = {}
    for pair in (query or "").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v
    return params


def route(db, method, path, u, body, addr="", query="", token=""):
    # /api/verify used to lose its query string because _handle stripped it.
    if method == "GET" and path == "/api/verify":
        return verify_email(db, _parse_query(query).get("token", ""))
    if method == "POST" and path == "/api/login":
        return login(db, body, addr)
    if method == "POST" and path == "/api/register":
        return register(db, body, addr)
    if method == "POST" and path == "/api/logout":
        return logout(db, token)
    if method == "GET" and path == "/api/demo/students":
        return demo_students(db)
    if method == "POST" and path == "/api/demo/diagnostic":
        return diagnostic_report(db, body.get("student_id"))
    if method == "POST" and path == "/api/demo/sensitivity":
        return sensitivity_report(db, body.get("student_id"))
    if u is None:
        raise ApiError(401, "sign in first")
    if method == "GET" and path == "/api/me":
        return {"id": u["id"], "name": u["full_name"], "role": u["role"],
                "avatar": u.get("avatar"), "email": u.get("email"),
                "bio": u.get("bio"), "subjects": u.get("subjects"),
                "is_verified": u.get("is_verified")}
    if method == "POST" and path == "/api/student/profile":
        need_role(u, "student")
        return update_profile(db, u, body)
    if method == "POST" and path == "/api/student/delete":
        need_role(u, "student")
        return delete_account(db, u)
    if method == "POST" and path == "/api/student/rate":
        need_role(u, "student")
        return rate_tutor(db, u, body)
    if method == "POST" and path == "/api/tutor/profile":
        need_role(u, "tutor")
        return update_tutor_profile(db, u, body)
    if method == "POST" and path == "/api/tutor/delete":
        need_role(u, "tutor")
        return delete_account(db, u)
    if method == "POST" and path == "/api/tutor/note":
        need_role(u, "tutor")
        return add_session_note(db, u, body)
    if method == "POST" and path == "/api/tutor/assign":
        need_role(u, "tutor")
        return assign_retest(db, u, body)
    if method == "GET" and path == "/api/tutors":
        return [dict(r) for r in db.execute("SELECT id, full_name AS name FROM profiles WHERE role = 'tutor' AND is_active = 1 ORDER BY full_name")]
    if method == "GET" and path == "/api/student/state":
        need_role(u, "student")
        return state(db, u)
    if method == "POST" and path == "/api/student/start":
        need_role(u, "student")
        return start(db, u, body.get("kind"), body.get("module"))
    if method == "POST" and path == "/api/student/answer":
        need_role(u, "student")
        return answer(db, u, body)
    if method == "POST" and path == "/api/student/submit":
        need_role(u, "student")
        return submit(db, u)
    if method == "POST" and path == "/api/student/tutor":
        need_role(u, "student")
        return choose_tutor(db, u, body.get("tutor_id"))
    if method == "GET" and path.startswith("/api/student/lesson/"):
        need_role(u, "student")
        try:
            m = int(path.rsplit("/", 1)[-1])
        except ValueError:
            raise ApiError(400, "module must be an integer")
        if not 0 <= m <= 9:
            raise ApiError(400, "module must be 0..9")
        content = LESSON_CONTENT.get(m)
        if not content:
            raise ApiError(404, "lesson not found")
        return {"module": m, "title": content["title"], "turns": content["chat"],
                "progress": lesson_progress(db, u["id"], m)}
    if method == "POST" and path == "/api/student/lesson/mark":
        need_role(u, "student")
        m = body.get("module")
        section = body.get("section")
        if not isinstance(m, int) or not 0 <= m <= 9:
            raise ApiError(400, "module must be 0..9")
        return mark_lesson_section(db, u["id"], m, section)
    if method == "GET" and path.startswith("/api/student/practice/"):
        need_role(u, "student")
        try:
            m = int(path.rsplit("/", 1)[-1])
        except ValueError:
            raise ApiError(400, "module must be an integer")
        if not 0 <= m <= 9:
            raise ApiError(400, "module must be 0..9")
        # Practice Mode respects the same chapter lock as the assessment.
        if m > 0 and not latest(db, u["id"], "pretest", m - 1, "submitted"):
            raise ApiError(403, f"finish Chapter {m} first to unlock Chapter {m + 1}")
        ids = MOD_ITEMS[m]
        items = []
        for i in ids:
            iid = C.item_id(i)
            row = db.execute("SELECT stem, category, primary_skill_id, correct_option FROM items WHERE id = ?", (iid,)).fetchone()
            opts = {}
            for o in db.execute("SELECT option_letter, body FROM item_options WHERE item_id = ?", (iid,)):
                opts[o["option_letter"]] = o["body"]
            items.append({"id": iid, "module": m, "stem": row["stem"],
                          "cat": row["category"],
                          "skill": SKILLS[row["primary_skill_id"]][0],
                          "options": opts})
        return items
    if method == "GET" and path == "/api/student/longtest":
        need_role(u, "student")
        return long_test_state(db, u["id"])
    if method == "POST" and path == "/api/student/longtest/start":
        need_role(u, "student")
        return long_test_start(db, u["id"])
    if method == "POST" and path == "/api/student/longtest/answer":
        need_role(u, "student")
        return long_test_answer(db, u["id"], body)
    if method == "POST" and path == "/api/student/longtest/submit":
        need_role(u, "student")
        return long_test_submit(db, u["id"])
    if method == "GET" and path == "/api/tutor/students":
        need_role(u, "tutor")
        return tutor_students(db, u)
    if method == "GET" and path == "/api/admin/overview":
        need_role(u, "admin")
        return admin_overview(db)
    if method == "GET" and path == "/api/admin/audit":
        need_role(u, "admin")
        return admin_audit(db, limit=body.get("limit", 200),
                           action=body.get("action"), actor_id=body.get("actor_id"))
    if method == "GET" and path == "/api/admin/items":
        need_role(u, "admin")
        return admin_list_items(db)
    if method == "POST" and path == "/api/admin/items/tag":
        need_role(u, "admin")
        return admin_tag_option(db, u, body.get("item_id"), body.get("letter"), body.get("source"))
    if method == "GET" and path == "/api/admin/graph":
        need_role(u, "admin")
        return admin_list_graph(db)
    if method == "POST" and path == "/api/admin/graph/add":
        need_role(u, "admin")
        return admin_add_edge(db, u, body.get("from"), body.get("to"))
    if method == "POST" and path == "/api/admin/graph/remove":
        need_role(u, "admin")
        return admin_remove_edge(db, u, body.get("from"), body.get("to"))
    if method == "GET" and path == "/api/admin/users":
        need_role(u, "admin")
        return admin_list_users(db)
    if method == "POST" and path == "/api/admin/users/create":
        need_role(u, "admin")
        return admin_create_user(db, u, body)
    if method == "POST" and path == "/api/admin/users/update":
        need_role(u, "admin")
        return admin_update_user(db, u, body)
    if method == "POST" and path == "/api/admin/users/permanent-delete":
        need_role(u, "admin")
        return admin_permanent_delete_user(db, u, body.get("id"))
    if method == "POST" and path == "/api/admin/users/bulk":
        need_role(u, "admin")
        return admin_bulk_import_students(db, u, body)
    if method == "GET" and path == "/api/admin/validation":
        need_role(u, "admin")
        return admin_validation_get(db)
    if method == "POST" and path == "/api/admin/validation/save":
        need_role(u, "admin")
        return admin_validation_save(db, u, body)
    if method == "GET" and path == "/api/admin/validation/kappa":
        need_role(u, "admin")
        return admin_validation_kappa(db)
    if method == "POST" and path == "/api/student/practice/answer":
        need_role(u, "student")
        return practice_answer(db, u, body)
    if method == "POST" and path == "/api/staff/sensitivity":
        need_role(u, "admin", "tutor")
        return staff_sensitivity(db, u, body.get("student_id"))
    raise ApiError(404, "no such route")


PAGES = {"/": "index.html", "/game": "game.html",
         "/dashboard": "dashboard.html", "/demo": "demo.html"}
STATIC_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
                ".ico": "image/x-icon", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8"}


def _is_same_origin(origin, host):
    if not origin or not host:
        return False
    for scheme in ("http://", "https://"):
        if origin.startswith(scheme):
            return origin[len(scheme):].rstrip("/") == host
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:;")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        raw = self.path
        path, _, query = raw.partition("?")
        logger.info(json.dumps({"timestamp": now(), "method": method,
                                "path": path, "ip": self.client_address[0]}))
        if method == "POST":
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin not in ALLOWED_ORIGINS and not _is_same_origin(origin, host):
                return self._reply(403, json.dumps({"error": "CSRF validation failed"}).encode(),
                                   "application/json")
        if method == "GET" and path in PAGES:
            with open(os.path.join(HERE, PAGES[path]), "rb") as f:
                return self._reply(200, f.read(), "text/html; charset=utf-8")
        if method == "GET" and path.startswith("/static/"):
            rel = path[len("/static/"):]
            root = os.path.abspath(os.path.join(HERE, "static"))
            full = os.path.abspath(os.path.join(root, rel))
            if not full.startswith(root + os.sep) and full != root:
                return self._reply(403, b"forbidden", "text/plain")
            if os.path.isfile(full):
                ext = os.path.splitext(full)[1].lower()
                ctype = STATIC_TYPES.get(ext, "application/octet-stream")
                with open(full, "rb") as f:
                    return self._reply(200, f.read(), ctype)
            return self._reply(404, b"not found", "text/plain")

        db = connect()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(n) if n else b""
            body = json.loads(raw_body) if raw_body else {}
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            u = user_from(db, self)
            code, obj = 200, route(db, method, path, u, body,
                                   self.client_address[0], query, token)
        except ApiError as e:
            code, obj = e.code, {"error": e.msg}
        except json.JSONDecodeError:
            code, obj = 400, {"error": "request body must be JSON"}
        except Exception as e:
            code, obj = 500, {"error": "server error: " + str(e)}
        finally:
            db.close()
        self._reply(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def add_admin(email, password=None, name="Administrator"):
    if not os.path.exists(DB_PATH):
        db = connect()
        seed(db)
        db.close()
    password = password or secrets.token_urlsafe(10)
    db = connect()
    try:
        db.execute("INSERT INTO profiles (id, role, full_name, email, password_hash, is_verified) VALUES (?,?,?,?,?,1)",
                   (str(uuid.uuid4()), "admin", name, email, hash_pw(password)))
        db.commit()
        print(f"Admin account created: {email}")
        print(f"Temporary password:   {password}")
        print("Sign in at http://127.0.0.1:8000/dashboard")
    except sqlite3.IntegrityError:
        print("That email is already registered.")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] in ("add-admin", "--add-admin"):
        if len(argv) < 2:
            print('usage: python server.py add-admin <email> [password] ["Full Name"]')
            sys.exit(2)
        add_admin(argv[1], argv[2] if len(argv) > 2 else None,
                  argv[3] if len(argv) > 3 else "Administrator")
        sys.exit(0)
    if not os.path.exists(DB_PATH):
        db = connect()
        seed(db)
        db.close()
    else:
        db = connect()
        migrate(db)
        refresh_dmap(db)
        db.close()

    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    cert_file = os.path.join(HERE, "cert.pem")
    key_file = os.path.join(HERE, "key.pem")
    if os.path.exists(cert_file) and os.path.exists(key_file):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print("SkillBridge running (HTTPS)")
        print("  start page       https://127.0.0.1:8000")
    else:
        print("SkillBridge running (HTTP). Place cert.pem and key.pem in the folder to enable HTTPS.")
        print("  start page       http://127.0.0.1:8000")
    print("  student game     /game")
    print("  staff dashboard  /dashboard")
    print("  WHSG-DRA demo    /demo")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass