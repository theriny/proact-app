
import sqlite3
from pathlib import Path

DB_PATH = Path("proact-backup.db")

if not DB_PATH.exists():
    raise FileNotFoundError(
        f"Database not found: {DB_PATH.resolve()}"
    )

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = OFF")

try:
    # Verify the expected source column exists.
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(courses)")
    }

    required = {
        "course_id", "sem1_course_code", "sem2_course_code",
        "course_name", "department", "description", "credits",
        "course_level", "pathway", "is_ap", "is_honors",
        "is_dual_enrollment", "created_at", "updated_at"
    }

    missing = required - columns
    if missing:
        raise RuntimeError(
            f"Unexpected courses schema; missing columns: {missing}"
        )

    if "department_id" in columns:
        raise RuntimeError(
            "courses.department_id already exists. "
            "Inspect the database before rerunning."
        )

    conn.execute("BEGIN")

    original_count = conn.execute(
        "SELECT COUNT(*) FROM courses"
    ).fetchone()[0]

    # 1. Create the department lookup table.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS departments (
            department_id INTEGER PRIMARY KEY AUTOINCREMENT,
            department_name TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2. Copy distinct department names from courses.
    conn.execute("""
        INSERT OR IGNORE INTO departments (department_name)
        SELECT DISTINCT TRIM(department)
        FROM courses
        WHERE department IS NOT NULL
          AND TRIM(department) <> ''
    """)

    # Refuse to continue if any course lacks a department name.
    missing_departments = conn.execute("""
        SELECT course_id, course_name
        FROM courses
        WHERE department IS NULL
           OR TRIM(department) = ''
    """).fetchall()

    if missing_departments:
        raise RuntimeError(
            "Some courses have missing department names: "
            f"{missing_departments[:10]}"
        )

    # 3. Create replacement courses table.
    conn.execute("""
        CREATE TABLE courses_new (
            course_id INTEGER PRIMARY KEY,
            sem1_course_code FLOAT UNIQUE,
            sem2_course_code FLOAT UNIQUE,
            course_name VARCHAR(150) NOT NULL,
            department_id INTEGER NOT NULL,
            description TEXT NULL,
            credits INTEGER,
            course_level INTEGER NULL,
            pathway BOOLEAN NULL,
            is_ap BOOLEAN NULL,
            is_honors BOOLEAN NULL,
            is_dual_enrollment BOOLEAN NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NULL,

            FOREIGN KEY (department_id)
                REFERENCES departments(department_id)
                ON DELETE RESTRICT
        )
    """)

    # 4. Copy courses and map department names to IDs.
    conn.execute("""
        INSERT INTO courses_new (
            course_id,
            sem1_course_code,
            sem2_course_code,
            course_name,
            department_id,
            description,
            credits,
            course_level,
            pathway,
            is_ap,
            is_honors,
            is_dual_enrollment,
            created_at,
            updated_at
        )
        SELECT
            c.course_id,
            c.sem1_course_code,
            c.sem2_course_code,
            c.course_name,
            d.department_id,
            c.description,
            c.credits,
            c.course_level,
            c.pathway,
            c.is_ap,
            c.is_honors,
            c.is_dual_enrollment,
            c.created_at,
            c.updated_at
        FROM courses AS c
        JOIN departments AS d
          ON d.department_name = TRIM(c.department)
    """)

    # 5. Validate the replacement before swapping tables.
    new_count = conn.execute(
        "SELECT COUNT(*) FROM courses_new"
    ).fetchone()[0]

    if new_count != original_count:
        raise RuntimeError(
            f"Course count mismatch: original={original_count}, "
            f"replacement={new_count}"
        )

    # Verify no course IDs were lost.
    missing_ids = conn.execute("""
        SELECT course_id FROM courses
        EXCEPT
        SELECT course_id FROM courses_new
    """).fetchall()

    if missing_ids:
        raise RuntimeError(
            f"Course IDs missing from replacement: {missing_ids[:10]}"
        )

    # 6. Replace the old table.
    # Existing tables that reference courses should be checked afterward.
    conn.execute("DROP TABLE courses")
    conn.execute(
        "ALTER TABLE courses_new RENAME TO courses"
    )

    # 7. Recreate any indexes/triggers specific to courses here,
    # if your database has custom ones.

    conn.commit()

    # Re-enable FK enforcement on this connection.
    conn.execute("PRAGMA foreign_keys = ON")

    fk_errors = conn.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()

    integrity = conn.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0]

    print(f"Migration completed: {DB_PATH.resolve()}")
    print(f"Courses preserved: {new_count}")

    print("\nDepartments:")
    for row in conn.execute("""
        SELECT department_id, department_name
        FROM departments
        ORDER BY department_name
    """):
        print(row)

    print(f"\nIntegrity check: {integrity}")
    print(f"Foreign-key violations: {len(fk_errors)}")

    if integrity != "ok" or fk_errors:
        print("WARNING: Investigate the database before using it.")

except Exception:
    conn.rollback()
    raise

finally:
    conn.close()