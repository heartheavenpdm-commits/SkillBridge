"""Bulk import of student accounts from CSV.

Pure functions only: parsing, validation, and duplicate detection. No database
access here, so the rules can be tested without SQLite. server.py does the
writes and calls into this module.

Expected CSV: a header row with 'name' and 'email' columns, and optionally
'password'. Blank passwords get a generated temporary password. Extra columns
are ignored. Every account is created with the student role; the file cannot
create tutors or admins.
"""
import csv
import io
import re
import secrets

MAX_ROWS = 300                 # each row costs one password hash (~60 ms), keeps requests short
MAX_BYTES = 512 * 1024
NAME_MIN, NAME_MAX = 2, 120    # matches profiles.full_name CHECK constraint
EMAIL_MAX = 254
PASSWORD_MIN, PASSWORD_MAX = 8, 128   # matches self-registration rule
GENERATED_LENGTH = 10
# Omits look-alike characters (0/o, 1/l/i) so handed-out passwords are easy to read.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BulkImportError(Exception):
    """File-level problem that stops the whole import (as opposed to a bad row)."""


def make_password():
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(GENERATED_LENGTH))


def _normalise_header(cell):
    return cell.strip().lower().replace(" ", "_")


def parse_csv(text):
    """Return a list of row dicts: {line, name, email, password}. Raises BulkImportError."""
    if not isinstance(text, str) or not text.strip():
        raise BulkImportError("the CSV is empty")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise BulkImportError("the file is larger than 512 KB")

    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    cols = None
    rows = []
    for record in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in record):
            continue  # skip blank lines

        if cols is None:
            header = [_normalise_header(c) for c in record]
            cols = {}
            for idx, h in enumerate(header):
                if h in ("name", "full_name") and "name" not in cols:
                    cols["name"] = idx
                elif h == "email" and "email" not in cols:
                    cols["email"] = idx
                elif h == "password" and "password" not in cols:
                    cols["password"] = idx
            if "name" not in cols or "email" not in cols:
                raise BulkImportError("the first row must be a header with 'name' and 'email' columns")
            continue

        def cell(key):
            i = cols.get(key)
            return record[i].strip() if i is not None and i < len(record) else ""

        rows.append({"line": line, "name": cell("name"), "email": cell("email"), "password": cell("password")})
        if len(rows) > MAX_ROWS:
            raise BulkImportError(f"a file can hold at most {MAX_ROWS} students; split it into smaller files")

    if cols is None:
        raise BulkImportError("the file has no header row")
    if not rows:
        raise BulkImportError("the file has a header but no student rows")
    return rows


def check_row(row):
    """Return the first problem with a row, or None if the row is well formed."""
    name, email, password = row["name"], row["email"], row["password"]
    if not name:
        return "name is missing"
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        return f"name must be {NAME_MIN} to {NAME_MAX} characters"
    if not email:
        return "email is missing"
    if len(email) > EMAIL_MAX or not EMAIL_RE.match(email):
        return "email is not valid"
    # Supplied passwords must meet the same rule as self-registration.
    # A blank password means "generate one", which is always strong.
    if password:
        if len(password) < PASSWORD_MIN:
            return f"password must be at least {PASSWORD_MIN} characters, or left blank to generate one"
        if len(password) > PASSWORD_MAX:
            return f"password must be {PASSWORD_MAX} characters or less"
        if not re.search(r"[A-Z]", password):
            return "password must contain an uppercase letter, or be left blank"
        if not re.search(r"[a-z]", password):
            return "password must contain a lowercase letter, or be left blank"
        if not re.search(r"[0-9]", password):
            return "password must contain a number, or be left blank"
    return None


def plan(rows, existing_emails):
    """Classify every row without writing anything.

    existing_emails: lowercase email strings already in the database.
    Returns one result dict per row, aligned with `rows`. Status is 'ready' or 'error'.
    The first occurrence of an email in the file wins; later repeats are errors.
    """
    seen = {}
    results = []
    for row in rows:
        problem = check_row(row)
        key = row["email"].lower()
        if problem is None:
            if key in seen:
                problem = f"duplicate of line {seen[key]} in this file"
            elif key in existing_emails:
                problem = "email is already registered"
        if problem is None:
            seen[key] = row["line"]
        results.append({
            "line": row["line"], "name": row["name"], "email": row["email"],
            "has_password": bool(row["password"]),
            "status": "error" if problem else "ready",
            "message": problem or "",
        })
    return results


def summary(results, dry_run):
    return {
        "dry_run": dry_run,
        "total": len(results),
        "ready": sum(r["status"] == "ready" for r in results),
        "created": sum(r["status"] == "created" for r in results),
        "failed": sum(r["status"] == "error" for r in results),
        "rows": results,
    }