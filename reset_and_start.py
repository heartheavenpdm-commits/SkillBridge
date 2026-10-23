import os
import sys
import subprocess

# 1. Delete the old database
db_file = "skillbridge.db"
if os.path.exists(db_file):
    os.remove(db_file)
    print("Old database deleted.")

# 2. Import server and force the database to seed
import server as S
db = S.connect()
S.seed(db)
db.close()
print("Database rebuilt successfully with all questions!")

# 3. Create the 5 tutors
tutors = [
    ("ana@sb.ph", "Ana Reyes", "Ana1234"),
    ("ben@sb.ph", "Ben Cruz", "Ben1234"),
    ("cara@sb.ph", "Cara Diaz", "Cara1234"),
    ("dan@sb.ph", "Dan Enriquez", "Dan1234"),
    ("elena@sb.ph", "Elena Flores", "Elena1234")
]

db = S.connect()
for email, name, pw in tutors:
    try:
        pid = "t_" + S.secrets.token_hex(6)
        db.execute(
            "INSERT INTO profiles (id, role, full_name, email, password_hash, is_verified) VALUES (?,?,?,?,?,1)", 
            (pid, "tutor", name, email, S.hash_pw(pw))
        )
    except Exception:
        pass
db.commit()
db.close()
print("5 tutors created successfully!")

# 4. Start the server automatically
print("Starting server...")
subprocess.run([sys.executable, "server.py"])