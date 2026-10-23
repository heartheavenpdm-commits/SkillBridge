"""End-to-end check of the security fixes. Run while the server is up."""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


print("== 1. Log in as admin ==")
code, admin = call("POST", "/api/login",
                   {"email": "admin@sb.ph", "password": "Admin1234"})
print(code, admin.get("role"), admin.get("name"))
assert code == 200, admin
tok = admin["token"]

print("\n== 2. Try to permanently delete a student ==")
code, res = call("POST", "/api/admin/users/permanent-delete",
                 {"id": "s2"}, token=tok)
print(code, res)
# Before the fix this was: 500 server error: FOREIGN KEY constraint failed
assert code == 200 and res.get("ok") is True, res

print("\n== 3. The deleted user's session (if any) is gone ==")
# make a student first, log them in, then delete
call("POST", "/api/admin/users/create",
     {"name": "Smoke Student", "email": "smoke@sb.ph",
      "role": "student", "password": "Smoke1234"}, token=tok)
code, sm = call("POST", "/api/login",
                {"email": "smoke@sb.ph", "password": "Smoke1234"})
assert code == 200, sm
stok = sm["token"]
code, res = call("POST", "/api/admin/users/permanent-delete",
                 {"id": sm["id"]}, token=tok)
print("delete:", code, res)
# Now the old student token must be rejected
code, me = call("GET", "/api/me", token=stok)
print("me with deleted token:", code, me)
# Before the fix this could return the profile; after the fix it is 401.
assert code == 401, me

print("\n== 4. Practice on a locked chapter is refused ==")
code, stu = call("POST", "/api/login",
                 {"email": "maria@sb.ph", "password": "Demo1234"})
ptok = stu["token"]
code, res = call("GET", "/api/student/practice/5", token=ptok)
print("practice chapter 6:", code, res)
# Maria has only chapter 1 cleared in the seed, so 6 is locked.
assert code == 403, res

print("\n== 5. /api/verify now returns a clean 400 ==")
code, res = call("GET", "/api/verify?token=anything")
print(code, res)
assert code == 400 and "token" in res.get("error", ""), res

print("\n== 6. Logout invalidates the token ==")
code, res = call("POST", "/api/logout", token=ptok)
print("logout:", code, res)
code, me = call("GET", "/api/me", token=ptok)
print("me after logout:", code, me)
assert code == 401, me

print("\nALL SMOKE CHECKS PASSED")