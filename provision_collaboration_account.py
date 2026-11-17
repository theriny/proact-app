"""Provision a parent/counselor login and explicitly link it to one student.

Run from the project folder after setting DB_PATH to the development database.
This script never changes an existing user's role or any student/course records.
"""
from getpass import getpass
import re
import sqlite3
import sys

import main


def main_cli():
    role = input('Account role (parent/counselor): ').strip().lower()
    if role not in ('parent', 'counselor'):
        print('Role must be parent or counselor.')
        return 2
    email = input('Account email: ').strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        print('Enter a valid email address.')
        return 2
    try:
        student_id = int(input('Student ID to link: ').strip())
    except ValueError:
        print('Student ID must be an integer.')
        return 2
    password = getpass('Temporary password (minimum 12 characters): ')
    confirm = getpass('Confirm temporary password: ')
    if len(password) < 12 or password != confirm:
        print('Passwords must match and contain at least 12 characters.')
        return 2

    main.ensure_phase_5_tables()
    with main.db_connection() as conn:
        student = conn.execute('SELECT student_id,first_name,last_name FROM students WHERE student_id=?', (student_id,)).fetchone()
        if not student:
            print('Student ID not found; no changes were made.')
            return 2
        existing = conn.execute('SELECT user_id,role FROM users WHERE email=? COLLATE NOCASE', (email,)).fetchone()
        if existing:
            if existing['role'] != role:
                print(f"That email already belongs to a '{existing['role']}' account. No role changes were made.")
                return 2
            user_id = existing['user_id']
            # Do not reset a known user's password implicitly.
            print('An account with this email and role already exists. Link it without changing its password.')
        else:
            user_id = conn.execute('INSERT INTO users(email,password_hash,role,is_active) VALUES(?,?,?,1)',
                (email, main.password_hash.hash(password), role)).lastrowid
        conn.execute('''INSERT OR IGNORE INTO student_relationships(student_id,related_user_id,relationship_type,status)
            VALUES(?,?,?,'active')''', (student_id, user_id, role))
        conn.commit()
    print(f"Linked {role} account {email} to {student['first_name']} {student['last_name']} (student ID {student_id}).")
    print('Deliver initial credentials securely and follow the school’s approved account-management process.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main_cli())
