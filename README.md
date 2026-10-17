# Langston Student MVP — FastAPI Authentication Starter

Provides registration, login, logout, and a protected student dashboard using FastAPI, Jinja2, SQLite, and Argon2 password hashing. The starter does **not** create, drop, or alter your tables. It expects your existing database schema.

## Expected columns

- `users`: `user_id`, `email`, `password_hash`, `role`, `is_active`
- `students`: `student_id`, `user_id`, `first_name`, `last_name`, `school_name`, `current_grade`, `graduation_year`

It assumes `users.role` accepts `student`, `users.is_active` uses `1` for active, and `students.user_id` references `users.user_id`. Registration inserts both records in one transaction. If your actual schema differs, adapt the INSERT statements to match; do not alter the database blindly.

## Setup (Windows PowerShell)

```CMD
py -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
for /f "delims=" %i in ('python -c "import secrets; print(secrets.token_urlsafe(48))"') do set "SESSION_SECRET=%i"$env:DB_PATH = "C:\\path\\to\\proact-backup.db"
set "DB_PATH=C:\path\to\proact-backup.db"
set "APP_ENV=development"
set "COOKIE_SECURE=false"
uvicorn main:app --reload


```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DB_PATH="/path/to/proact-backup.db"
export APP_ENV=development
export COOKIE_SECURE=false
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/register` to create a test account. Visit `/login` to sign in.

## Security included

- Argon2 password hashing; plaintext passwords are not stored.
- Signed, HTTP-only, SameSite=Lax session cookie with an eight-hour lifetime.
- CSRF token validation for registration, login, and logout forms.
- Generic login failure messages.
- Basic in-process IP-based login throttling. It resets on restart and is not shared across workers, so use a shared limiter or reverse-proxy protections for production.
- `PRAGMA foreign_keys=ON` for every database connection.
- Required-schema checks at startup; no schema changes are made.
- Security headers and no-store cache header.

For production, use HTTPS, set `APP_ENV=production`, keep secrets out of source control, set `COOKIE_SECURE=true`, restrict database file permissions, and add account recovery, email verification, monitoring, and shared rate limiting before public launch. Starlette SessionMiddleware stores signed session data in the cookie, so do not put sensitive profile data in the session.

## Test checklist

1. Register a test student.
2. Confirm one row was added to `users` and one to `students`.
3. Sign out and sign back in.
4. Open `/dashboard` in a private window; it should redirect to `/login`.
5. Try an incorrect password; the response should not disclose whether the email exists.
6. Confirm `password_hash` contains an Argon2 hash, not the original password.
7. Run `PRAGMA foreign_key_check;` in SQLite.

## Limitations

This is an authentication starter, not a complete student planning app. It does not yet include password reset, email verification, parent/counselor access, or persistent multi-worker rate limiting. It assumes the `students.school_name` column accepts the value `Langston High School`.
