# Langston Student MVP — Phase 2.1 + 2.2

FastAPI + Jinja2 + SQLite starter for student registration/login and Phase 2.2 profile, interests, and career/college goals.

## What this version includes

- Secure student registration, login, logout, Argon2 password hashing, signed session cookies, CSRF protection, and a basic development login throttle.
- Protected student dashboard.
- Edit/save student profile: first name, last name, grade, graduation year.
- Add, edit, list, and delete student interests (category, interest level, notes).
- Add, edit, list, and delete career or college goals (description, related career, institution, major, degree/training level, priority, target year, status).
- Uses the existing `careers` table for a related-career dropdown when it has compatible `career_id` and `career_name` columns.
- Keeps data tied to the signed-in student's `student_id`; update/delete statements are scoped to that student.

## Database safety and expected schema

Set `DB_PATH` to your **development backup** (`proact-backup.db`). Do not point this at a production database.

Required existing columns:

- `users`: `user_id`, `email`, `password_hash`, `role`, `is_active`
- `students`: `student_id`, `user_id`, `first_name`, `last_name`, `school_name`, `current_grade`, `graduation_year`

On startup, the app creates `student_interests` and `student_goals` **only if they do not already exist**. It does not drop, rename, or alter existing tables. If either table already exists but has an incompatible column layout, the app stops with a schema error rather than silently changing it. Back up the database before testing and reconcile any schema mismatch intentionally.

Phase 2.2 table columns expected by this app:

- `student_interests`: `interest_id`, `student_id`, `interest_category`, `interest_name`, `interest_level`, `notes`, `created_at`
- `student_goals`: `goal_id`, `student_id`, `goal_type`, `title`, `description`, `career_id`, `institution_name`, `intended_major`, `degree_level`, `priority`, `target_year`, `status`, `created_at`, `updated_at`

The app does not populate or change your course, pathway, prerequisite, or career reference data.

## Setup in Windows Command Prompt (CMD)

Open CMD in the folder containing `main.py` and `requirements.txt`, then run each command:

```cmd
py -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
for /f "delims=" %i in ('python -c "import secrets; print(secrets.token_urlsafe(48))"') do set "SESSION_SECRET=%i"
set "DB_PATH=C:\path\to\proact-backup.db"
set "APP_ENV=development"
set "COOKIE_SECURE=false"
uvicorn main:app --reload
```

Replace `C:\path\to\proact-backup.db` with the actual absolute path. These variables last only for the current CMD window. If you place the secret-generation command in a `.bat` file, use `%%i` instead of `%i`.

Open these local pages after the server starts:

- `http://127.0.0.1:8000/register`
- `http://127.0.0.1:8000/login`
- `http://127.0.0.1:8000/dashboard`
- `http://127.0.0.1:8000/profile`
- `http://127.0.0.1:8000/interests`
- `http://127.0.0.1:8000/goals`

## Smoke-test checklist

1. Register a practice student and verify one row appears in `users` and one in `students`.
2. Open **Student profile**, change a field, save, refresh, and confirm it persists.
3. Add at least two interests; edit one; delete one; refresh and confirm the remaining interest persists.
4. Add a career goal and a college goal; edit each; sign out and sign back in; confirm both remain.
5. Confirm one student's records are not shown while signed in as another student.
6. Run `PRAGMA foreign_key_check;` in SQLite.

## Security notes and limitations

- Passwords are hashed using Argon2; plaintext passwords are not stored.
- Session cookie is signed, HTTP-only, SameSite=Lax, and lasts eight hours.
- CSRF tokens protect form submissions. SQL values are parameterized.
- Login throttling is in-process and intended for development, not a multi-worker production deployment.
- For public production use, add HTTPS, persistent secret management, shared rate limiting, email verification, account recovery, logging/monitoring, and a security review. Set `APP_ENV=production` and `COOKIE_SECURE=true` behind HTTPS.
- This milestone does not yet implement parent/counselor access, course browsing, or four-year planning.


### Canonical `title` and `description` columns

The app and templates use only `student_goals.title` and `student_goals.description`. Legacy columns `goal_title` and `goal_description` should be removed by rebuilding the table with the supplied migration script; the app deliberately fails validation if those legacy columns remain. The migration backs up the database, copies legacy values into the canonical columns, preserves goal IDs/records, and removes the legacy columns. Run it only against a stopped development app and a backed-up database:

```cmd
python migrate_student_goals.py
```

Or pass a specific database path:

```cmd
python migrate_student_goals.py "C:\your\actual\path\proact-backup.db"
```

Any future child table that needs a goal should store `goal_id` as a foreign key and read `title`/`description` by joining to `student_goals`; do not duplicate goal title or description columns in child tables. The migration stops if it detects other database objects that directly reference the legacy column names, so those can be reviewed rather than silently broken.

### Existing student_goals status constraint

Some existing databases restrict `student_goals.status` to `active`, `achieved`, or `paused`. The goal form and validation use those exact values to remain compatible with that CHECK constraint.
