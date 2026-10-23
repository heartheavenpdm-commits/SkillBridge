# Changes for the 12 partly-done items

## Files to replace or add

| File | Action |
|---|---|
| `server.py` | Replace |
| `dashboard.html` | Replace |
| `game.html` | Replace |
| `test_features.py` | Add (optional, for running tests) |

Earlier files (`bulk_import.py`, `ratelimit.py`, `test_bulk_import.py`, `test_ratelimit.py`) are unchanged and still required.

## Urgent: existing bug fixed (not on the list)

The student game sends `letter` (and `skip: true` for skips). The server read only `chosen`. Every battle answer and every long-test answer recorded through the game was stored as a **skip**, and the screen then said "Skipped. No damage dealt." after each answer.

Both endpoints now accept the game's fields. Attempts taken before this fix have their answers stored as skips, which can't be recovered from the database, so their scores are wrong. **Those attempts need to be retaken.** Check which students are affected before you rely on any existing battle or long-test results.

## Security fix: answer key

`GET /api/student/practice/<module>` sent `correct_option` to the student. Those items are the same as the assessment, so a student could read the answer key. The field is removed. Practice answers are now checked on the server (`POST /api/student/practice/answer`), which returns only right or wrong.

## Done, by item

- **S14 Practice:** answers are saved to `practice_responses` and summarised in the student state. The UI doesn't show them yet.
- **A1 User management:** the edit form has a password field with a Generate button. The create form is prefilled with a generated password instead of `demo`. The server rejects passwords outside 4–128 characters.
- **A6 Skill structure:** a chapter → subtopic → skill tree sits above the prerequisite editor.
- **X1 Audit logging:** validation saves are audited. Individual answers are not, since they are high volume and attempt submission is already logged.
- **X12 Tests:** `test_features.py` has 26 tests covering the battle and practice answers, progress, subtopics, roll-up, graph data, the validation audit, passwords, versioned weights, staff access, tutor comments, and the pretest → retest flow. The tutor note route is not tested yet.
- **R1 Subtopics:** each skill row in the evaluation panel shows its subtopic.
- **R3 Sensitivity:** a button in the evaluation panel runs the analysis. Admins can run it for any student, and tutors only for students assigned to them. It is not shown to students.
- **R9 Validity:** the battle screen shows "Answered X of Y · N needed to submit". The server calculates these numbers.
- **R10 Roll-up:** a chapter roll-up table sits in the evaluation panel (staff only).
- **R13 Weights:** each attempt records its scoring version, and scoring reads the weights for that version from `scoring_weights`. The admin overview shows whether the stored weights match the engine constants. To change weights later, insert a new version and raise `CURRENT_SCORING_VERSION`; existing attempts keep their own version.
- **T10 Ratings:** the admin overview lists each tutor's comments with stars and date. Tutors don't see their own comments yet.

## Decisions still needed

- **R6 Retests:** the code allows repeated retests with no cap. The retest dialog doesn't mention any limit. Decide whether to cap retests and how many.
- **Practice and battle items:** practice uses the same items as the pretest, and battles already show right or wrong after each question. A student can learn the key by trial. Fixing this properly needs a separate practice question bank, which is content work.
- **Per-question feedback in assessments:** decide whether showing right or wrong after each pretest question is intended.

## Security issue to decide (not changed)

`/api/demo/students`, `/api/demo/diagnostic`, and `/api/demo/sensitivity` require no sign-in. Anyone who can reach the server can fetch any student's diagnostic report by ID. Restrict these routes to staff, or disable them outside a demo deployment. `demo.html` currently calls them without signing in, so this needs a decision.

## Testing

- All 79 tests pass: 34 bulk import, 19 rate limiting, 26 features.
- Deliberate breaks caught by the tests: the battle answer fix, the long-test answer fix, the practice key leak, the version-based scoring, and the staff assignment check.
- Browser checks on the staff dashboard: 13 passed, covering the password reset through real sign-in, the weight status, the skill tree, the comments card, the tutor's roll-up, subtopic, and sensitivity panels, with no JavaScript errors.
- **Not browser-tested:** the student game screens (battle progress, practice). These were syntax-checked, and the server behaviour they rely on is covered by tests.
