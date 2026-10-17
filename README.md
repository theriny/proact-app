# Langston Student MVP — Phase 2.1, 2.2, and 2.3

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

## Phase 2.3 — Course browser and current courses

This milestone adds:

- `/courses`: authenticated catalog browser with keyword search, department filter, and optional AP / Honors / dual-enrollment / pathway filters when those catalog flags exist.
- `/current-courses`: each student's own course records, filterable by current/completed/withdrawn status.
- Add a catalog course to a student's records with school year, grade, term, and status.
- Edit or remove a student's own course record; record an optional final grade.
- `student_courses` is created only when it does not exist. Existing catalog tables are treated as read-only reference data.
- Course catalog queries support the legacy `courses.department` text column and the normalized `courses.department_id` plus `departments.department_name` layout.
- If `course_grade_levels` has recognizable `course_id` and grade columns, the browser displays the source grade-level values as catalog information. This milestone does not enforce prerequisite or grade eligibility rules; those belong in a later planning/validation milestone.

### Phase 2.3 database table

When absent, the app creates `student_courses` with `student_course_id`, `student_id`, `course_id`, `school_year`, `grade_level`, `term`, `status`, `final_grade`, `created_at`, and `updated_at`. It references the existing `students` and `courses` tables and prevents duplicate records for the same student/course/year/term. If a `student_courses` table already exists with a different schema, the app stops and reports the missing columns instead of modifying it automatically.

### Phase 2.3 test checklist

1. Sign in with a practice student and open `/courses`.
2. Search by a course name or code; filter by department and course type.
3. Add a course to the student's current-course records.
4. Open `/current-courses`, edit the year/grade/term/status, optionally add a final grade, then save.
5. Remove a test record and confirm it disappears.
6. Sign out and sign back in; confirm records persist.
7. Register or use a second practice student and confirm their current-course list is separate.
8. Confirm catalog reference rows were not changed.

Use a backed-up development database for testing. The application code and templates were smoke-tested against a temporary SQLite database matching the expected catalog schema; the user's actual database has not been inspected or tested here.


## Phase 2.3 term-value compatibility

The course form submits the canonical term values expected by the existing student_courses table: `fall`, `spring`, `summer`, and `full_year`. The backend also normalizes older labels (`Full year`, `Semester 1`, `Semester 2`) before inserting or updating. No database migration is required for the term mismatch.


## Phase 2.4 — Four-year planner with saving

This version adds `/planner` and the `academic_plans` and `academic_plan_courses` tables. The app creates these tables only when absent and validates an existing table instead of silently altering it. It does not replace the course catalog or existing student data.

### Install
1. Stop Uvicorn with `Ctrl+C`.
2. Back up the development database: `copy "%DB_PATH%" "%DB_PATH%.before-phase-24.bak"` (or copy the actual database path).
3. Copy the updated `main.py`, `templates`, `static`, `README.md` into the existing project directory. Keep your existing database and environment variables.
4. Restart with `uvicorn main:app --reload`.
5. Sign in and open `http://127.0.0.1:8000/planner`.

The planner saves a student's plan name/status and planned course rows, groups courses into Grades 9–12, estimates school years from the student's expected graduation year, and calculates catalog-credit totals where `courses.credits` is available. Duplicate course/grade/term entries are blocked. Each student's plan is scoped to their own authenticated student ID. This is a planning aid, not an official graduation audit: prerequisite and graduation-requirement validation still need counselor/catalog rule verification.


## Fix for an existing Phase 2.4 database missing `academic_plan_courses.updated_at`

If startup reports that `academic_plan_courses` is missing `updated_at`, stop Uvicorn and back up the database first. This project includes `migrate_phase_24.py`, which makes a timestamped backup and adds only the missing column, then populates existing rows. It does not drop or rebuild the table.

In Windows CMD, with the virtual environment active and from this project folder:

```cmd
python migrate_phase_24.py
```

This uses the `DB_PATH` environment variable. Or pass the full database path explicitly:

```cmd
python migrate_phase_24.py "C:\path\to\proact-backup.db"
```

After the script reports success, restart the app:

```cmd
uvicorn main:app --reload
```

The migration was tested against a copy of the supplied database. It added the missing column, verified that no rows had a blank `updated_at`, and passed `PRAGMA foreign_key_check`. The supplied database had no rows in `academic_plans` or `academic_plan_courses` at the time of inspection.
