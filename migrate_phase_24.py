"""Safely add the missing updated_at column to academic_plan_courses.

Usage (Windows CMD):
    python migrate_phase_24.py
    python migrate_phase_24.py "C:\\path\\to\\proact-backup.db"

If no path is supplied, DB_PATH must be set in the environment.
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    if len(sys.argv) > 2:
        print("Usage: python migrate_phase_24.py [path-to-database]")
        return 2

    raw_path = sys.argv[1] if len(sys.argv) == 2 else os.environ.get("DB_PATH")
    if not raw_path:
        print("ERROR: Set DB_PATH or pass the database path as an argument.")
        return 2

    db_path = Path(raw_path).expanduser().resolve()
    if not db_path.is_file():
        print(f"ERROR: Database file not found: {db_path}")
        return 2

    # Verify the target schema before making a backup or changing anything.
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            table = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='academic_plan_courses'"
            ).fetchone()
            if table is None:
                print("ERROR: Table 'academic_plan_courses' does not exist. No changes made.")
                return 1
            columns = {row["name"] for row in conn.execute('PRAGMA table_info("academic_plan_courses")')}
            required = {
                "plan_course_id", "plan_id", "course_id", "planned_grade", "school_year",
                "term", "requirement_category", "status", "notes", "created_at"
            }
            missing = required - columns
            if missing:
                print("ERROR: Unexpected schema. Missing required existing columns: " + ", ".join(sorted(missing)))
                print("No changes made. Inspect the schema before attempting a broader migration.")
                return 1
            if "updated_at" in columns:
                print("No migration needed: academic_plan_courses.updated_at already exists.")
                return 0

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}.before_phase24_{timestamp}.bak")
        shutil.copy2(db_path, backup_path)
        print(f"Backup created: {backup_path}")

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute('ALTER TABLE academic_plan_courses ADD COLUMN updated_at TEXT')
            conn.execute("UPDATE academic_plan_courses SET updated_at = COALESCE(NULLIF(updated_at, ''), CURRENT_TIMESTAMP)")
            conn.commit()

            columns_after = {row["name"] for row in conn.execute('PRAGMA table_info("academic_plan_courses")')}
            if "updated_at" not in columns_after:
                raise RuntimeError("Migration verification failed: updated_at column is missing.")
            null_count = conn.execute(
                "SELECT COUNT(*) FROM academic_plan_courses WHERE updated_at IS NULL OR updated_at = ''"
            ).fetchone()[0]
            fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if null_count:
                raise RuntimeError(f"Migration verification failed: {null_count} rows have no updated_at value.")
            if fk_errors:
                raise RuntimeError(f"Foreign-key check found {len(fk_errors)} issue(s) after migration. Backup is at {backup_path}")

        print("SUCCESS: Added academic_plan_courses.updated_at and populated existing rows.")
        print("Existing plan/course rows, IDs, constraints, and indexes were not intentionally changed.")
        print("You can now restart the FastAPI application.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        print("Do not rerun blindly. Check the backup and inspect the database state first.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
