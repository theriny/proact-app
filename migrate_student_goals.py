"""Normalize student_goals to canonical title/description columns.

Usage from Windows CMD:
    python migrate_student_goals.py
    python migrate_student_goals.py "C:\\path\\to\\proact-backup.db"

Creates a timestamped file backup, then rebuilds only student_goals. The canonical
schema contains title/description and does not contain goal_title/goal_description.
The migration preserves goal IDs and goal records, maps legacy columns, normalizes
statuses to the application's accepted values, and checks foreign-key integrity.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

CANONICAL_COLUMNS = {
    "goal_id", "student_id", "goal_type", "title", "description", "career_id",
    "institution_name", "intended_major", "degree_level", "priority", "target_year",
    "status", "created_at", "updated_at",
}
LEGACY_COLUMNS = {"goal_title", "goal_description"}


def main():
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else os.getenv("DB_PATH", "proact-backup.db")).expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}\nSet DB_PATH or pass the database path as an argument.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_before_goal_normalization_{stamp}{db_path.suffix}.bak")
    shutil.copy2(db_path, backup_path)
    print(f"Database backup created: {backup_path}")

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # Collect metadata before rebuilding. Foreign keys must be disabled before
        # beginning the transaction for a SQLite table rebuild.
        conn.execute("PRAGMA foreign_keys=OFF")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='student_goals'").fetchone()
        if not exists:
            raise SystemExit("Table student_goals does not exist; no migration was performed.")

        info = list(conn.execute("PRAGMA table_info(student_goals)"))
        old_cols = {r["name"] for r in info}
        required = {"goal_id", "student_id", "goal_type"}
        missing = required - old_cols
        if missing:
            raise SystemExit(f"Cannot safely migrate; missing required columns: {', '.join(sorted(missing))}. Backup is available at {backup_path}")

        # Do not silently discard columns outside the known legacy/current schemas.
        unknown = old_cols - CANONICAL_COLUMNS - LEGACY_COLUMNS
        if unknown:
            raise SystemExit(
                "Migration stopped before changing the database because student_goals has additional columns "
                f"that need explicit handling: {', '.join(sorted(unknown))}. Backup is available at {backup_path}"
            )

        saved_objects = list(conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name='student_goals' AND type IN ('index','trigger') AND sql IS NOT NULL"
        ))
        # An external view/trigger referencing legacy names needs a manual update;
        # don't leave it silently broken after dropping those columns.
        legacy_refs = list(conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "AND (lower(sql) LIKE '%goal_title%' OR lower(sql) LIKE '%goal_description%') "
            "AND NOT (type='table' AND name='student_goals')"
        ))
        external_legacy_refs = [r for r in legacy_refs if not (r["type"] in ("index", "trigger") and r["name"] in {o["name"] for o in saved_objects})]
        if external_legacy_refs:
            refs = ", ".join(f"{r['type']} {r['name']}" for r in external_legacy_refs)
            raise SystemExit(
                "Migration stopped because other database objects reference legacy goal_title/goal_description: "
                f"{refs}. Update those objects to use student_goals.title/description first. Backup: {backup_path}"
            )

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS student_goals__phase22_new")
        conn.execute('''
            CREATE TABLE student_goals__phase22_new (
                goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                goal_type TEXT NOT NULL CHECK (goal_type IN ('career','college')),
                title TEXT NOT NULL,
                description TEXT,
                career_id TEXT,
                institution_name TEXT,
                intended_major TEXT,
                degree_level TEXT,
                priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
                target_year INTEGER,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','achieved','paused')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
            )
        ''')

        def expr(col, fallback="NULL"):
            return f'"{col}"' if col in old_cols else fallback

        title_exprs = []
        if "title" in old_cols:
            title_exprs.append("NULLIF(TRIM(CAST(\"title\" AS TEXT)), '')")
        if "goal_title" in old_cols:
            title_exprs.append("NULLIF(TRIM(CAST(\"goal_title\" AS TEXT)), '')")
        title_expr = "COALESCE(" + ", ".join(title_exprs + [
            "CASE WHEN lower(COALESCE(goal_type,''))='career' THEN 'Career goal ' "
            "WHEN lower(COALESCE(goal_type,''))='college' THEN 'College goal ' "
            "ELSE 'Goal ' END || CAST(goal_id AS TEXT)"
        ]) + ")"

        desc_exprs = []
        if "description" in old_cols:
            desc_exprs.append('NULLIF(TRIM(CAST("description" AS TEXT)), \'\')')
        if "goal_description" in old_cols:
            desc_exprs.append('NULLIF(TRIM(CAST("goal_description" AS TEXT)), \'\')')
        desc_expr = "COALESCE(" + ", ".join(desc_exprs + ["NULL"]) + ")"

        status_raw = expr("status", "'active'")
        status_expr = (
            f"CASE lower(COALESCE(CAST({status_raw} AS TEXT), 'active')) "
            "WHEN 'achieved' THEN 'achieved' WHEN 'completed' THEN 'achieved' "
            "WHEN 'paused' THEN 'paused' ELSE 'active' END"
        )
        priority_raw = expr("priority", "2")
        priority_expr = f"CASE WHEN CAST({priority_raw} AS INTEGER) BETWEEN 1 AND 3 THEN CAST({priority_raw} AS INTEGER) ELSE 2 END"

        fields = ["goal_id", "student_id", "goal_type", "title", "description", "career_id", "institution_name",
                  "intended_major", "degree_level", "priority", "target_year", "status", "created_at", "updated_at"]
        select_exprs = [
            expr("goal_id"), expr("student_id"), expr("goal_type", "'career'"), title_expr, desc_expr,
            expr("career_id"), expr("institution_name"), expr("intended_major"), expr("degree_level"),
            priority_expr, expr("target_year"), status_expr, expr("created_at", "CURRENT_TIMESTAMP"),
            expr("updated_at", "CURRENT_TIMESTAMP"),
        ]
        conn.execute(
            f"INSERT INTO student_goals__phase22_new ({', '.join(fields)}) "
            f"SELECT {', '.join(select_exprs)} FROM student_goals"
        )
        old_count = conn.execute("SELECT COUNT(*) FROM student_goals").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM student_goals__phase22_new").fetchone()[0]
        if old_count != new_count:
            raise RuntimeError(f"Record count changed during migration ({old_count} -> {new_count}); transaction rolled back.")

        conn.execute("DROP TABLE student_goals")
        conn.execute("ALTER TABLE student_goals__phase22_new RENAME TO student_goals")
        # Recreate custom indexes/triggers that do not depend on removed legacy columns.
        for obj in saved_objects:
            sql = obj["sql"]
            if "goal_title" in sql.lower() or "goal_description" in sql.lower():
                print(f"Skipped legacy {obj['type']} {obj['name']} because it references a removed column.")
                continue
            try:
                conn.execute(sql)
            except sqlite3.Error as exc:
                raise RuntimeError(f"Could not recreate {obj['type']} {obj['name']}: {exc}") from exc

        index_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_student_goals_student'").fetchone()
        if not index_exists:
            conn.execute("CREATE INDEX idx_student_goals_student ON student_goals(student_id)")

        fk_issues = list(conn.execute("PRAGMA foreign_key_check"))
        if fk_issues:
            raise RuntimeError(f"Foreign-key check found {len(fk_issues)} issue(s); transaction rolled back. First issue: {tuple(fk_issues[0])}")
        conn.commit()

        final_cols = [r["name"] for r in conn.execute("PRAGMA table_info(student_goals)")]
        if "goal_title" in final_cols or "goal_description" in final_cols:
            raise RuntimeError("Legacy goal-prefixed title/description columns remain after migration.")
        print(f"Migration complete. Preserved {new_count} goal record(s).")
        print("Canonical title/description columns are in place; goal_title and goal_description are removed.")
        print("Final columns: " + ", ".join(final_cols))
        print("The application and templates should read/write only student_goals.title and student_goals.description.")
        print(f"Backup: {backup_path}")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        print(f"Migration failed. Original backup remains at: {backup_path}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
