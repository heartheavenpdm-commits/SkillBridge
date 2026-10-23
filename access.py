"""Authorization rules for the API layer. SQLite has no row-level security,
so every read or write goes through these checks.

assignments: set of (student_id, tutor_id) pairs with an active tutor assignment.
"""


def can_read_attempt(viewer, attempt, assignments):
    role = viewer["role"]
    if role == "admin":
        return True
    if role == "student":
        return attempt["student_id"] == viewer["id"]
    if role == "tutor":
        return (attempt["student_id"], viewer["id"]) in assignments
    return False


def can_answer(viewer, attempt):
    """Students answer only their own attempts while they are still in progress."""
    return (viewer["role"] == "student" and attempt["student_id"] == viewer["id"]
            and attempt["status"] == "in_progress")


def can_rate(viewer, retest):
    """A student rates only a submitted retest of their own."""
    return (viewer["role"] == "student" and retest["student_id"] == viewer["id"]
            and retest["kind"] == "retest" and retest["status"] == "submitted")


def can_change_role(viewer):
    return viewer["role"] == "admin"


def can_see_answer_key(viewer):
    """Answer keys and distractor evidence are staff-only, never shown to students."""
    return viewer["role"] in ("tutor", "admin")