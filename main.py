import hmac, os, re, secrets, sqlite3, time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pwdlib import PasswordHash
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DB_PATH', str(BASE_DIR / 'proact-backup.db')).strip()).resolve()
APP_ENV = os.getenv('APP_ENV', 'development').lower()
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
SESSION_SECRET = os.getenv('SESSION_SECRET', '')
if len(SESSION_SECRET) < 32:
    if APP_ENV == 'production':
        raise RuntimeError('Set SESSION_SECRET to a random secret of at least 32 characters.')
    SESSION_SECRET = secrets.token_urlsafe(48)  # Development only; changes on restart.

app = FastAPI(title='Langston Student MVP')
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
    session_cookie='langston_session', max_age=60 * 60 * 8,
    same_site='lax', https_only=(COOKIE_SECURE or APP_ENV == 'production'))
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')
templates = Jinja2Templates(directory=BASE_DIR / 'templates')
password_hash = PasswordHash.recommended()

# Simple in-process rate limit for development. Not shared across workers/restarts.
WINDOW_SECONDS, MAX_ATTEMPTS = 900, 10
attempts = defaultdict(deque)

@contextmanager
def db_connection():
    if not DB_PATH.exists():
        raise RuntimeError(f'Database not found: {DB_PATH}. Set DB_PATH to your test database path.')
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
    finally:
        conn.close()

def ensure_phase_22_tables():
    """Add Phase 2.2 tables only when absent; never alter existing tables."""
    with db_connection() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS student_interests (
            interest_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            interest_category TEXT NOT NULL DEFAULT 'subject',
            interest_name TEXT NOT NULL,
            interest_level INTEGER,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS student_goals (
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
        );
        CREATE INDEX IF NOT EXISTS idx_student_interests_student ON student_interests(student_id);
        CREATE INDEX IF NOT EXISTS idx_student_goals_student ON student_goals(student_id);
        ''')
        conn.commit()

def validate_phase_22_schema():
    expected = {
        'student_interests': {'interest_id','student_id','interest_category','interest_name','interest_level','notes','created_at'},
        'student_goals': {'goal_id','student_id','goal_type','title','description','career_id','institution_name','intended_major','degree_level','priority','target_year','status','created_at','updated_at'},
    }
    with db_connection() as conn:
        for table, required in expected.items():
            columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
            missing = required - columns
            if missing:
                raise RuntimeError(f"Table '{table}' already exists but is missing Phase 2.2 columns: {', '.join(sorted(missing))}. Back up the database and reconcile the schema before running this version.")
            if table == 'student_goals':
                legacy = columns & {'goal_title', 'goal_description'}
                if legacy:
                    raise RuntimeError(
                        "Table 'student_goals' still has legacy columns: " + ', '.join(sorted(legacy)) +
                        ". Stop the server, back up the database, and run migrate_student_goals.py to normalize to title/description."
                    )


# ------------------------- Phase 2.3: course browser & current courses -------------------------
# Normalize human-readable/legacy term labels to the values enforced by the existing DB.
def _normalize_course_term(term: str) -> str:
    mapping = {
        "fall": "fall", "spring": "spring", "summer": "summer", "full_year": "full_year",
        "Full year": "full_year", "full year": "full_year",
        "Semester 1": "fall", "semester 1": "fall",
        "Semester 2": "spring", "semester 2": "spring",
    }
    return mapping.get((term or "").strip(), "")

def ensure_phase_23_tables():
    """Create the student-course tracking table if absent; never alter existing tables."""
    with db_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS student_courses (
            student_course_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            school_year TEXT NOT NULL,
            grade_level INTEGER NOT NULL CHECK (grade_level BETWEEN 9 AND 12),
            term TEXT NOT NULL DEFAULT 'full_year' CHECK (term IN ('fall','spring','summer','full_year')),
            status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','completed','withdrawn')),
            final_grade TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE,
            FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT,
            UNIQUE (student_id, course_id, school_year, term)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_student_courses_student ON student_courses(student_id)")
        conn.commit()

def validate_phase_23_schema():
    with db_connection() as conn:
        expected = {'student_course_id','student_id','course_id','school_year','grade_level','term','status','final_grade','created_at','updated_at'}
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='student_courses'").fetchone()
        if not exists:
            raise RuntimeError("Required Phase 2.3 table 'student_courses' is missing.")
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(student_courses)')}
        missing = expected - cols
        if missing:
            raise RuntimeError("Table 'student_courses' already exists but is missing Phase 2.3 columns: " + ', '.join(sorted(missing)) + ". Back up the database and reconcile the schema.")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='courses'").fetchone()
        if not exists:
            raise RuntimeError("Required reference table 'courses' is missing. Load the Langston course catalog before using Phase 2.3.")
        course_cols = {r['name'] for r in conn.execute('PRAGMA table_info(courses)')}
        if not {'course_id','course_name'} <= course_cols:
            raise RuntimeError("The courses table must include course_id and course_name.")

def _table_columns(conn, table_name):
    return {r['name'] for r in conn.execute(f'PRAGMA table_info("{table_name}")')}

def _department_join(conn):
    cols = _table_columns(conn, 'courses')
    if 'department' in cols:
        return 'c.department', ''
    if 'department_id' in cols and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='departments'").fetchone():
        dcols = _table_columns(conn, 'departments')
        if {'department_id','department_name'} <= dcols:
            return 'd.department_name', ' LEFT JOIN departments d ON d.department_id=c.department_id'
    return "''", ''

def _course_select_sql(conn):
    cols = _table_columns(conn, 'courses')
    dept_expr, join = _department_join(conn)
    fields = ['c.course_id AS course_id', 'c.course_name AS course_name', f'{dept_expr} AS department']
    for col in ('sem1_course_code','sem2_course_code','description','credits','course_level','pathway','is_ap','is_honors','is_dual_enrollment'):
        fields.append(f'c."{col}" AS "{col}"' if col in cols else f'NULL AS "{col}"')
    return 'SELECT ' + ', '.join(fields) + ' FROM courses c' + join

def _get_course_departments(conn):
    dept_expr, join = _department_join(conn)
    rows = conn.execute(f"SELECT DISTINCT {dept_expr} AS department FROM courses c{join} WHERE {dept_expr} IS NOT NULL AND TRIM({dept_expr})<>'' ORDER BY department").fetchall()
    return [r['department'] for r in rows]

def _search_courses(conn, q='', department='', course_type=''):
    """Search the course catalog using only columns present in this database.

    Some Langston database copies have the normalized department_id schema while
    older copies store department text directly. Optional catalog columns can
    also differ between copies, so never reference one unless it exists.
    """
    cols = _table_columns(conn, 'courses')
    dept_expr, _ = _department_join(conn)
    sql = _course_select_sql(conn)
    where, args = [], []

    if q and q.strip():
        needle = '%' + q.strip()[:100] + '%'
        searchable = []
        if 'course_name' in cols:
            searchable.append("COALESCE(c.course_name, '') LIKE ?")
        if 'description' in cols:
            searchable.append("COALESCE(c.description, '') LIKE ?")
        if dept_expr != "''":
            searchable.append("COALESCE(" + dept_expr + ", '') LIKE ?")
        if 'course_id' in cols:
            searchable.append("CAST(c.course_id AS TEXT) LIKE ?")
        for code_col in ('sem1_course_code', 'sem2_course_code'):
            if code_col in cols:
                searchable.append(f'CAST(c."{code_col}" AS TEXT) LIKE ?')
        if searchable:
            where.append('(' + ' OR '.join(searchable) + ')')
            args.extend([needle] * len(searchable))
        else:
            # A valid catalog table should always have course_name, but return
            # no rows instead of raising an SQL error if the schema is damaged.
            where.append('1=0')

    if department and dept_expr != "''":
        where.append(f'{dept_expr}=?')
        args.append(department)

    type_columns = {
        'ap': 'is_ap',
        'honors': 'is_honors',
        'dual': 'is_dual_enrollment',
        'pathway': 'pathway',
    }
    if course_type in type_columns:
        col = type_columns[course_type]
        if col in cols:
            where.append(f"COALESCE(c.\"{col}\", 0) IN (1, '1', 'TRUE', 'true', 'Yes', 'yes')")
        else:
            # An unavailable optional filter should return no results, not 500.
            where.append('1=0')

    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY c.course_name COLLATE NOCASE LIMIT 300'
    return conn.execute(sql, args).fetchall()

# ------------------------- Phase 3.5: prerequisite validation -------------------------
def _normalize_course_id(value):
    """Normalize numeric IDs imported as integers, strings, or values like '1000.0'."""
    if value is None:
        return ''
    text = str(value).strip()
    if not text:
        return ''
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError, OverflowError):
        pass
    return text.casefold()


def _evaluate_prerequisite_course(conn, student_id, course_id):
    """Evaluate a course's AND/OR prerequisite tree against one student's records.

    The function is read-only. A current prerequisite is treated as in-progress,
    and non-course/unresolved requirements require human verification rather than
    being incorrectly marked as failed or satisfied.
    """
    tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required_tables = {'prerequisite_groups', 'prerequisites', 'student_courses', 'courses'}
    if not required_tables <= tables:
        missing = ', '.join(sorted(required_tables - tables))
        return {
            'status': 'review_required', 'eligible': None,
            'summary': f'Prerequisite validation is unavailable because these tables are missing: {missing}.',
            'requirements': [], 'course': None,
        }

    target_key = _normalize_course_id(course_id)
    course = next((r for r in conn.execute('SELECT course_id, course_name FROM courses')
                   if _normalize_course_id(r['course_id']) == target_key), None)
    if not course:
        return {
            'status': 'review_required', 'eligible': None,
            'summary': 'The selected course could not be found in the course catalog.',
            'requirements': [], 'course': None,
        }

    groups = [dict(r) for r in conn.execute('SELECT * FROM prerequisite_groups')
              if _normalize_course_id(r['course_id']) == target_key]
    if not groups:
        return {
            'status': 'no_prerequisites', 'eligible': None,
            'summary': 'No prerequisite rules are recorded for this course. Confirm requirements with the current school catalog or counselor.',
            'requirements': [], 'course': course,
        }

    group_ids = {str(g['prerequisite_group_id']) for g in groups}
    all_requirements = [dict(r) for r in conn.execute('SELECT * FROM prerequisites')
                        if str(r['prerequisite_group_id']) in group_ids]
    requirements_by_group = defaultdict(list)
    for item in all_requirements:
        requirements_by_group[str(item['prerequisite_group_id'])].append(item)
    groups_by_id = {str(g['prerequisite_group_id']): g for g in groups}
    children_by_parent = defaultdict(list)
    roots = []
    for group in groups:
        parent = group.get('parent_group_id')
        if parent is None or str(parent).strip() == '' or str(parent) not in groups_by_id:
            roots.append(group)
        else:
            children_by_parent[str(parent)].append(group)
    for children in children_by_parent.values():
        children.sort(key=lambda g: (int(g.get('group_order') or 0), str(g['prerequisite_group_id'])))
    roots.sort(key=lambda g: (int(g.get('group_order') or 0), str(g['prerequisite_group_id'])))

    student_courses = {}
    for record in conn.execute('SELECT course_id, status FROM student_courses WHERE student_id=?', (student_id,)):
        key = _normalize_course_id(record['course_id'])
        status = str(record['status'] or '').strip().casefold()
        if key not in student_courses:
            student_courses[key] = set()
        student_courses[key].add(status)

    catalog_names = {
        _normalize_course_id(r['course_id']): r['course_name']
        for r in conn.execute('SELECT course_id, course_name FROM courses')
    }
    details = []
    visiting = set()

    def evaluate_requirement(item, group_id):
        kind = str(item.get('requirement_type') or '').strip().casefold()
        resolution = str(item.get('resolution_status') or '').strip().casefold()
        raw_prereq_id = item.get('prerequisite_course_id')
        label = (item.get('requirement_text') or item.get('source_requirement') or '').strip()
        if not label and raw_prereq_id:
            label = catalog_names.get(_normalize_course_id(raw_prereq_id), f'Course {raw_prereq_id}')
        if not label:
            label = 'Unspecified prerequisite'

        detail = {
            'text': label, 'state': 'pending', 'status_label': 'Needs verification',
            'explanation': '', 'group_id': group_id,
        }
        if kind == 'course' and resolution == 'resolved' and _normalize_course_id(raw_prereq_id):
            prereq_key = _normalize_course_id(raw_prereq_id)
            if prereq_key not in catalog_names:
                detail['explanation'] = 'This prerequisite points to a course that is not in the current catalog; counselor review is needed.'
            elif 'completed' in student_courses.get(prereq_key, set()):
                detail.update(state='met', status_label='Satisfied',
                              explanation=f'Completed: {catalog_names[prereq_key]}.')
            elif 'current' in student_courses.get(prereq_key, set()):
                detail.update(state='pending', status_label='In progress',
                              explanation=f'{catalog_names[prereq_key]} is marked current; completion has not yet been confirmed.')
            else:
                detail.update(state='unmet', status_label='Not satisfied',
                              explanation=f'Complete {catalog_names[prereq_key]} before taking this course.')
        elif resolution == 'unresolved' or kind == 'unresolved_reference':
            detail['explanation'] = 'This source requirement could not be matched to a catalog course; counselor review is needed.'
        else:
            friendly = label.replace('_', ' ').strip().capitalize()
            detail['text'] = friendly
            detail['explanation'] = 'This requirement must be confirmed by a teacher, counselor, exam result, audition, or other school process.'
        details.append(detail)
        return detail['state']

    def combine(states, operator):
        if not states:
            return 'pending'
        op = str(operator or 'AND').strip().upper()
        if op == 'OR':
            if 'met' in states:
                return 'met'
            if all(state == 'unmet' for state in states):
                return 'unmet'
            return 'pending'
        if 'unmet' in states:
            return 'unmet'
        if all(state == 'met' for state in states):
            return 'met'
        return 'pending'

    def evaluate_group(group):
        group_id = str(group['prerequisite_group_id'])
        if group_id in visiting:
            details.append({
                'text': 'Prerequisite group structure', 'state': 'pending',
                'status_label': 'Needs verification',
                'explanation': 'A cycle was detected in the prerequisite groups; counselor review is needed.',
                'group_id': group_id,
            })
            return 'pending'
        visiting.add(group_id)
        states = []
        items = sorted(requirements_by_group.get(group_id, []),
                       key=lambda x: (int(x.get('requirement_order') or 0), str(x.get('prerequisite_id') or '')))
        for item in items:
            states.append(evaluate_requirement(item, group_id))
        for child in children_by_parent.get(group_id, []):
            states.append(evaluate_group(child))
        visiting.remove(group_id)
        if not states:
            details.append({
                'text': 'Prerequisite group', 'state': 'pending',
                'status_label': 'Needs verification',
                'explanation': 'This prerequisite group has no requirement records.', 'group_id': group_id,
            })
            return 'pending'
        return combine(states, group.get('group_operator', 'AND'))

    root_states = [evaluate_group(root) for root in roots]
    overall = combine(root_states, 'AND')
    if overall == 'met':
        status = 'eligible'
        summary = 'All recorded prerequisite requirements are satisfied through at least one valid path.'
    elif overall == 'unmet':
        status = 'not_yet_eligible'
        summary = 'One or more required prerequisite conditions are not satisfied yet.'
    else:
        status = 'review_required'
        summary = 'Eligibility cannot be confirmed yet. Some prerequisites are in progress, unresolved, or require school staff verification.'

    return {
        'status': status,
        'eligible': True if overall == 'met' else (False if overall == 'unmet' else None),
        'summary': summary,
        'requirements': details,
        'course': course,
    }


@app.get('/prerequisites', response_class=HTMLResponse)
def prerequisite_check_page(request: Request, course_id: str = ''):
    user = current_user(request)
    if not user:
        return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student:
        return RedirectResponse('/login', status_code=303)

    with db_connection() as conn:
        catalog = conn.execute('SELECT course_id, course_name FROM courses ORDER BY course_name COLLATE NOCASE').fetchall()
        result = None
        selected_id = ''
        if course_id:
            selected_id = _normalize_course_id(course_id)
            result = _evaluate_prerequisite_course(conn, student['student_id'], course_id)
            if result.get('course'):
                selected_id = _normalize_course_id(result['course']['course_id'])
            else:
                selected_id = ''

    return render(request, 'prerequisites.html', student=student, catalog=catalog,
                  selected_course_id=selected_id, result=result)


def _student_course_rows(student_id, status=''):
    with db_connection() as conn:
        dept_expr, join = _department_join(conn)
        # Use a dynamic department expression so both legacy text and normalized department schemas work.
        sql = f"""SELECT sc.student_course_id, sc.student_id, sc.course_id, sc.school_year,
                  sc.grade_level, sc.term, sc.status, sc.final_grade, c.course_name,
                  c.sem1_course_code, c.sem2_course_code, c.credits, c.description,
                  c.is_ap, c.is_honors, c.is_dual_enrollment, {dept_expr} AS department
                  FROM student_courses sc JOIN courses c ON c.course_id=sc.course_id
                  {join} WHERE sc.student_id=?"""
        args = [student_id]
        if status in ('current','completed','withdrawn'):
            sql += ' AND sc.status=?'; args.append(status)
        sql += ' ORDER BY sc.school_year DESC, sc.grade_level DESC, c.course_name COLLATE NOCASE'
        return conn.execute(sql, args).fetchall()

@app.get('/courses', response_class=HTMLResponse)
def course_browser(request: Request, q: str = '', department: str = '', course_type: str = ''):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    with db_connection() as conn:
        departments = _get_course_departments(conn)
        courses = _search_courses(conn, q, department if department in departments else '', course_type)
        grade_levels = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='course_grade_levels'").fetchone():
            gc = _table_columns(conn, 'course_grade_levels')
            course_key = 'course_id' if 'course_id' in gc else None
            grade_key = next((x for x in ('grade_level','grade','grade_level_id') if x in gc), None)
            if course_key and grade_key:
                try:
                    for r in conn.execute(f'SELECT "{course_key}" AS course_id, GROUP_CONCAT("{grade_key}", ", ") AS grades FROM course_grade_levels GROUP BY "{course_key}"'):
                        grade_levels[r['course_id']] = r['grades']
                except sqlite3.Error:
                    pass
    return render(request, 'courses.html', student=student, courses=courses, departments=departments,
                  q=q, selected_department=department, course_type=course_type, grade_levels=grade_levels,
                  error=None, success=None)

@app.get('/current-courses', response_class=HTMLResponse)
def current_courses_page(request: Request, status: str = ''):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    selected = status if status in ('current','completed','withdrawn') else ''
    return render(request, 'current_courses.html', student=student,
                  courses=_student_course_rows(student['student_id'], selected), selected_status=selected,
                  error=None, success=None, edit_course=None)

@app.get('/current-courses/{student_course_id}/edit', response_class=HTMLResponse)
def edit_current_course_page(request: Request, student_course_id: int):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    with db_connection() as conn:
        item = conn.execute('SELECT * FROM student_courses WHERE student_course_id=? AND student_id=?', (student_course_id, student['student_id'])).fetchone()
    if not item: return RedirectResponse('/current-courses', status_code=303)
    return render(request, 'current_courses.html', student=student,
                  courses=_student_course_rows(student['student_id']), selected_status='',
                  error=None, success=None, edit_course=item)

@app.post('/current-courses/add')
def add_current_course(request: Request, csrf: str = Form(...), course_id: int = Form(...),
                       school_year: str = Form(...), grade_level: int = Form(...),
                       term: str = Form('full_year'), status: str = Form('current')):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    school_year = school_year.strip()
    term = _normalize_course_term(term)
    if not csrf_ok(request, csrf): return HTMLResponse('Your form expired. Refresh and try again.', status_code=400)
    if not 9 <= grade_level <= 12 or term not in ('fall','spring','summer','full_year') or status not in ('current','completed','withdrawn'):
        return HTMLResponse('Please select a valid grade, term, and course status.', status_code=400)
    if not re.fullmatch(r'\d{4}-\d{4}', school_year) or int(school_year[:4]) + 1 != int(school_year[5:]):
        return HTMLResponse('School year must use the format YYYY-YYYY, such as 2026-2027.', status_code=400)
    with db_connection() as conn:
        if not conn.execute('SELECT 1 FROM courses WHERE course_id=?', (course_id,)).fetchone():
            return HTMLResponse('That course was not found in the catalog.', status_code=404)
        try:
            conn.execute('INSERT INTO student_courses (student_id,course_id,school_year,grade_level,term,status) VALUES (?,?,?,?,?,?)',
                          (student['student_id'], course_id, school_year, grade_level, term, status))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            print(f"[Phase 2.3] Failed to add course: {exc}")
            if 'UNIQUE constraint failed' in str(exc):
                return HTMLResponse('A uniqueness constraint was violated. Check the server terminal for the exact database constraint.', status_code=400)
            return HTMLResponse(f'Database constraint error: {exc}. Check the server terminal for details.', status_code=400)
    return RedirectResponse('/current-courses', status_code=303)

@app.post('/current-courses/{student_course_id}/save')
def save_current_course(request: Request, student_course_id: int, csrf: str = Form(...),
                        school_year: str = Form(...), grade_level: int = Form(...), term: str = Form('full_year'),
                        status: str = Form('current'), final_grade: str = Form('')):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    if not csrf_ok(request, csrf): return HTMLResponse('Invalid form token. Refresh and try again.', status_code=400)
    school_year = school_year.strip()
    term = _normalize_course_term(term)
    if not 9 <= grade_level <= 12 or term not in ('fall','spring','summer','full_year') or status not in ('current','completed','withdrawn'):
        return HTMLResponse('Please select a valid grade, term, and status.', status_code=400)
    if not re.fullmatch(r'\d{4}-\d{4}', school_year) or int(school_year[:4]) + 1 != int(school_year[5:]):
        return HTMLResponse('School year must use the format YYYY-YYYY.', status_code=400)
    with db_connection() as conn:
        cur = conn.execute('UPDATE student_courses SET school_year=?,grade_level=?,term=?,status=?,final_grade=?,updated_at=CURRENT_TIMESTAMP WHERE student_course_id=? AND student_id=?',
                           (school_year, grade_level, term, status, final_grade.strip()[:20] or None, student_course_id, student['student_id']))
        if not cur.rowcount: return RedirectResponse('/current-courses', status_code=303)
        conn.commit()
    return RedirectResponse('/current-courses', status_code=303)

@app.post('/current-courses/{student_course_id}/delete')
def delete_current_course(request: Request, student_course_id: int, csrf: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    if not csrf_ok(request, csrf): return HTMLResponse('Invalid form token. Refresh and try again.', status_code=400)
    with db_connection() as conn:
        conn.execute('DELETE FROM student_courses WHERE student_course_id=? AND student_id=?', (student_course_id, student['student_id']))
        conn.commit()
    return RedirectResponse('/current-courses', status_code=303)


def ensure_phase_24_tables():
    """Create planner tables only when absent. Existing tables are never altered."""
    with db_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS academic_plans (
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL UNIQUE,
            plan_name TEXT NOT NULL DEFAULT 'My Four-Year Plan',
            starting_grade INTEGER NOT NULL DEFAULT 9 CHECK (starting_grade BETWEEN 9 AND 12),
            graduation_year INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','finalized','archived')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS academic_plan_courses (
            plan_course_id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            planned_grade INTEGER NOT NULL CHECK (planned_grade BETWEEN 9 AND 12),
            school_year TEXT NOT NULL,
            term TEXT NOT NULL DEFAULT 'full_year' CHECK (term IN ('fall','spring','summer','full_year')),
            requirement_category TEXT NOT NULL DEFAULT 'elective' CHECK (requirement_category IN ('core','elective','pathway','other')),
            status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','approved','completed','removed')),
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (plan_id) REFERENCES academic_plans(plan_id) ON DELETE CASCADE,
            FOREIGN KEY (course_id) REFERENCES courses(course_id) ON DELETE RESTRICT,
            UNIQUE (plan_id, course_id, planned_grade, term)
        )
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_academic_plans_student ON academic_plans(student_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_academic_plan_courses_plan ON academic_plan_courses(plan_id)')
        conn.commit()

def validate_phase_24_schema():
    expected = {
        'academic_plans': {'plan_id','student_id','plan_name','starting_grade','graduation_year','status','created_at','updated_at'},
        'academic_plan_courses': {'plan_course_id','plan_id','course_id','planned_grade','school_year','term','requirement_category','status','notes','created_at','updated_at'},
    }
    with db_connection() as conn:
        for table, required in expected.items():
            columns = {r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            missing = required - columns
            if missing:
                raise RuntimeError(f"Table '{table}' already exists but is missing Phase 2.4 columns: {', '.join(sorted(missing))}. Back up the database and reconcile its schema before running this version.")
        for table in ('academic_plans','academic_plan_courses'):
            foreign = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            # A legacy table with unexpected FK definitions should not be silently rebuilt.
            if not foreign:
                raise RuntimeError(f"Table '{table}' exists without foreign-key relationships. Inspect its schema before continuing.")

def validate_schema():
    expected = {
        'users': {'user_id', 'email', 'password_hash', 'role', 'is_active'},
        'students': {'student_id', 'user_id', 'first_name', 'last_name', 'school_name', 'current_grade', 'graduation_year'},
    }
    with db_connection() as conn:
        for table, required in expected.items():
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                raise RuntimeError(f"Required table '{table}' is missing. Create the Phase 2 schema first.")
            columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
            missing = required - columns
            if missing:
                raise RuntimeError(f"Table '{table}' is missing columns: {', '.join(sorted(missing))}")

@app.on_event('startup')
def startup_check():
    validate_schema()
    ensure_phase_22_tables()
    validate_phase_22_schema()
    ensure_phase_23_tables()
    validate_phase_23_schema()
    ensure_phase_24_tables()
    validate_phase_24_schema()

@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Cache-Control', 'no-store')
    if APP_ENV == 'production':
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response

def get_csrf(request: Request) -> str:
    token = request.session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        request.session['csrf_token'] = token
    return token

def csrf_ok(request: Request, supplied: str) -> bool:
    expected = request.session.get('csrf_token', '')
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))

def render(request: Request, template: str, **context):
    context.setdefault('csrf_token', get_csrf(request))
    return templates.TemplateResponse(request=request, name=template, context=context)

def current_user(request: Request) -> Optional[sqlite3.Row]:
    user_id = request.session.get('user_id')
    if not user_id:
        return None
    with db_connection() as conn:
        return conn.execute('SELECT user_id, email, role FROM users WHERE user_id=? AND is_active=1', (user_id,)).fetchone()

def limited(key: str) -> bool:
    now = time.time(); events = attempts[key]
    while events and now - events[0] > WINDOW_SECONDS:
        events.popleft()
    return len(events) >= MAX_ATTEMPTS

def record_attempt(key: str):
    attempts[key].append(time.time())

@app.get('/')
def home(request: Request):
    return RedirectResponse('/dashboard' if current_user(request) else '/login', status_code=303)

@app.get('/register', response_class=HTMLResponse)
def register_page(request: Request):
    if current_user(request): return RedirectResponse('/dashboard', status_code=303)
    return render(request, 'register.html', error=None, values={})

@app.post('/register', response_class=HTMLResponse)
def register(request: Request, csrf: str = Form(...), first_name: str = Form(...), last_name: str = Form(...),
             email: str = Form(...), password: str = Form(...), confirm_password: str = Form(...),
             current_grade: int = Form(...), graduation_year: int = Form(...)):
    values = {'first_name': first_name.strip(), 'last_name': last_name.strip(), 'email': email.strip().lower(),
              'current_grade': current_grade, 'graduation_year': graduation_year}
    def fail(message): return render(request, 'register.html', error=message, values=values)
    if not csrf_ok(request, csrf): return fail('Your form expired. Please try again.')
    if not values['first_name'] or not values['last_name']: return fail('Enter your first and last name.')
    if len(values['email']) > 254 or '@' not in values['email'] or values['email'].startswith('@'):
        return fail('Enter a valid email address.')
    if not 12 <= len(password) <= 128: return fail('Use a password between 12 and 128 characters.')
    if password != confirm_password: return fail('The passwords do not match.')
    if current_grade not in (9, 10, 11, 12): return fail('Choose a grade from 9 through 12.')
    if not 2026 <= graduation_year <= 2045: return fail('Enter a valid graduation year.')
    hashed = password_hash.hash(password)
    try:
        with db_connection() as conn:
            conn.execute('BEGIN')
            cur = conn.execute("INSERT INTO users (email,password_hash,role,is_active) VALUES (?,?,'student',1)", (values['email'], hashed))
            user_id = cur.lastrowid
            conn.execute('''INSERT INTO students (user_id,first_name,last_name,school_name,current_grade,graduation_year)
                            VALUES (?,?,?,'Langston High School',?,?)''',
                         (user_id, values['first_name'], values['last_name'], current_grade, graduation_year))
            conn.commit()
    except sqlite3.IntegrityError:
        return fail("We couldn't create an account with those details. If you already registered, sign in.")
    except sqlite3.Error:
        return fail("We couldn't create your account right now. Please try again.")
    request.session.clear(); request.session['user_id'] = user_id; request.session['csrf_token'] = secrets.token_urlsafe(32)
    return RedirectResponse('/dashboard', status_code=303)

@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request): return RedirectResponse('/dashboard', status_code=303)
    return render(request, 'login.html', error=None, email='')

@app.post('/login', response_class=HTMLResponse)
def login(request: Request, csrf: str = Form(...), email: str = Form(...), password: str = Form(...)):
    if not csrf_ok(request, csrf):
        return render(request, 'login.html', error='Your form expired. Please try again.', email=email.strip())
    key = request.client.host if request.client else 'unknown'
    if limited(key):
        return render(request, 'login.html', error='Too many sign-in attempts. Please wait 15 minutes and try again.', email=email.strip())
    with db_connection() as conn:
        user = conn.execute('SELECT user_id,email,password_hash,role,is_active FROM users WHERE email=? COLLATE NOCASE', (email.strip(),)).fetchone()
    verified = False
    if user:
        try: verified = password_hash.verify(password, user['password_hash'])
        except Exception: verified = False
    if not user or not verified or not user['is_active']:
        record_attempt(key)
        return render(request, 'login.html', error='Email or password is incorrect.', email=email.strip())
    attempts.pop(key, None)
    request.session.clear(); request.session['user_id'] = user['user_id']; request.session['csrf_token'] = secrets.token_urlsafe(32)
    return RedirectResponse('/dashboard', status_code=303)

@app.get('/dashboard', response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    with db_connection() as conn:
        student = conn.execute('''SELECT first_name,last_name,school_name,current_grade,graduation_year
                                  FROM students WHERE user_id=?''', (user['user_id'],)).fetchone()
    if not student:
        request.session.clear(); return RedirectResponse('/login', status_code=303)
    return render(request, 'dashboard.html', student=student, email=user['email'])

@app.post('/logout')
def logout(request: Request, csrf: str = Form(...)):
    if not csrf_ok(request, csrf): return HTMLResponse('Invalid form token. Refresh and try again.', status_code=400)
    request.session.clear()
    return RedirectResponse('/login', status_code=303)


def get_student_for_user(user_id: int):
    with db_connection() as conn:
        return conn.execute('SELECT * FROM students WHERE user_id=?', (user_id,)).fetchone()

# ------------------------- Phase 2.4: four-year planner -------------------------

def _school_year_for_grade(graduation_year: int, grade: int) -> str:
    start_year = int(graduation_year) - (12 - int(grade)) - 1
    return f"{start_year}-{start_year + 1}"

def _get_or_create_plan(student):
    with db_connection() as conn:
        plan = conn.execute('SELECT * FROM academic_plans WHERE student_id=?', (student['student_id'],)).fetchone()
        if plan:
            # Keep graduation-year changes on the student profile reflected in the plan.
            if int(plan['graduation_year']) != int(student['graduation_year']):
                conn.execute('UPDATE academic_plans SET graduation_year=?, updated_at=CURRENT_TIMESTAMP WHERE plan_id=?',
                             (student['graduation_year'], plan['plan_id']))
                conn.commit()
                plan = conn.execute('SELECT * FROM academic_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone()
            return plan
        cur = conn.execute("INSERT INTO academic_plans (student_id,plan_name,starting_grade,graduation_year,status) VALUES (?,?,?,?, 'draft')",
                           (student['student_id'], 'My Four-Year Plan', student['current_grade'], student['graduation_year']))
        conn.commit()
        return conn.execute('SELECT * FROM academic_plans WHERE plan_id=?', (cur.lastrowid,)).fetchone()

def _planner_course_rows(plan_id):
    with db_connection() as conn:
        dept_expr, join = _department_join(conn)
        ccols = _table_columns(conn, 'courses')
        credits = 'c.credits' if 'credits' in ccols else 'NULL'
        sql = f'''SELECT apc.plan_course_id, apc.plan_id, apc.course_id, apc.planned_grade,
                         apc.school_year, apc.term, apc.requirement_category, apc.status,
                         apc.notes, c.course_name, {credits} AS credits, {dept_expr} AS department
                  FROM academic_plan_courses apc JOIN courses c ON c.course_id=apc.course_id
                  {join} WHERE apc.plan_id=?
                  ORDER BY apc.planned_grade, CASE apc.term WHEN 'fall' THEN 1 WHEN 'full_year' THEN 2 WHEN 'spring' THEN 3 WHEN 'summer' THEN 4 ELSE 5 END, c.course_name COLLATE NOCASE'''
        return conn.execute(sql, (plan_id,)).fetchall()

def _planner_catalog():
    with db_connection() as conn:
        return _search_courses(conn, '', '', '')

def _planner_context(student, plan, error=None, success=None):
    rows = _planner_course_rows(plan['plan_id'])
    grouped = {g: [] for g in (9,10,11,12)}
    totals = {g: 0.0 for g in (9,10,11,12)}
    for row in rows:
        grouped[int(row['planned_grade'])].append(row)
        try:
            totals[int(row['planned_grade'])] += float(row['credits'] or 0)
        except (ValueError, TypeError):
            pass
    courses = _planner_catalog()
    return dict(student=student, plan=plan, grouped_courses=grouped, credit_totals=totals,
                catalog_courses=courses, school_years={g: _school_year_for_grade(plan['graduation_year'],g) for g in (9,10,11,12)},
                error=error, success=success)

@app.get('/planner', response_class=HTMLResponse)
def four_year_planner(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    plan = _get_or_create_plan(student)
    return render(request, 'planner.html', **_planner_context(student, plan))

@app.post('/planner/settings', response_class=HTMLResponse)
def save_planner_settings(request: Request, csrf: str = Form(...), plan_name: str = Form(...), status: str = Form('draft')):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    plan = _get_or_create_plan(student)
    name = plan_name.strip()[:100]
    if not csrf_ok(request, csrf):
        return render(request, 'planner.html', **_planner_context(student, plan, error='Your form expired. Refresh and try again.'))
    if not name or status not in ('draft','finalized','archived'):
        return render(request, 'planner.html', **_planner_context(student, plan, error='Enter a plan name and choose a valid plan status.'))
    with db_connection() as conn:
        conn.execute('UPDATE academic_plans SET plan_name=?,status=?,graduation_year=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=? AND student_id=?',
                     (name, status, student['graduation_year'], plan['plan_id'], student['student_id']))
        conn.commit()
        plan = conn.execute('SELECT * FROM academic_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone()
    return render(request, 'planner.html', **_planner_context(student, plan, success='Your plan settings have been saved.'))

@app.post('/planner/courses/add', response_class=HTMLResponse)
def add_planned_course(request: Request, csrf: str = Form(...), course_id: int = Form(...),
                       planned_grade: int = Form(...), term: str = Form('full_year'),
                       requirement_category: str = Form('elective'), notes: str = Form('')):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    plan = _get_or_create_plan(student)
    if not csrf_ok(request, csrf):
        return render(request, 'planner.html', **_planner_context(student, plan, error='Your form expired. Refresh and try again.'))
    if planned_grade not in (9,10,11,12) or term not in ('fall','spring','summer','full_year') or requirement_category not in ('core','elective','pathway','other'):
        return render(request, 'planner.html', **_planner_context(student, plan, error='Choose a valid grade, term, and requirement category.'))
    school_year = _school_year_for_grade(plan['graduation_year'], planned_grade)
    with db_connection() as conn:
        if not conn.execute('SELECT 1 FROM courses WHERE course_id=?', (course_id,)).fetchone():
            return render(request, 'planner.html', **_planner_context(student, plan, error='That course is not in the catalog.'))
        try:
            conn.execute('''INSERT INTO academic_plan_courses
                (plan_id,course_id,planned_grade,school_year,term,requirement_category,status,notes)
                VALUES (?,?,?,?,?,?,'planned',?)''',
                (plan['plan_id'], course_id, planned_grade, school_year, term, requirement_category, notes.strip()[:500] or None))
            conn.execute("UPDATE academic_plans SET updated_at=CURRENT_TIMESTAMP WHERE plan_id=?", (plan['plan_id'],))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            message = 'That course is already planned for this grade and term.' if 'UNIQUE constraint failed' in str(exc) else f'Could not save this course: {exc}'
            return render(request, 'planner.html', **_planner_context(student, plan, error=message))
    plan = _get_or_create_plan(student)
    return render(request, 'planner.html', **_planner_context(student, plan, success='Course added to your four-year plan.'))

@app.post('/planner/courses/{plan_course_id}/update', response_class=HTMLResponse)
def update_planned_course(request: Request, plan_course_id: int, csrf: str = Form(...),
                          status: str = Form('planned'), notes: str = Form('')):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    plan = _get_or_create_plan(student)
    if not csrf_ok(request, csrf):
        return render(request, 'planner.html', **_planner_context(student, plan, error='Your form expired. Refresh and try again.'))
    if status not in ('planned','approved','completed','removed'):
        return render(request, 'planner.html', **_planner_context(student, plan, error='Choose a valid course status.'))
    with db_connection() as conn:
        cur = conn.execute('''UPDATE academic_plan_courses SET status=?,notes=?,updated_at=CURRENT_TIMESTAMP
                              WHERE plan_course_id=? AND plan_id=?''',
                           (status, notes.strip()[:500] or None, plan_course_id, plan['plan_id']))
        conn.commit()
        if cur.rowcount == 0:
            return RedirectResponse('/planner', status_code=303)
    return RedirectResponse('/planner', status_code=303)

@app.post('/planner/courses/{plan_course_id}/delete')
def delete_planned_course(request: Request, plan_course_id: int, csrf: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    plan = _get_or_create_plan(student)
    if not csrf_ok(request, csrf): return HTMLResponse('Invalid form token. Refresh and try again.', status_code=400)
    with db_connection() as conn:
        conn.execute('DELETE FROM academic_plan_courses WHERE plan_course_id=? AND plan_id=?', (plan_course_id, plan['plan_id']))
        conn.commit()
    return RedirectResponse('/planner', status_code=303)

def get_careers():
    # Read career choices only when the existing careers table has compatible columns.
    with db_connection() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='careers'").fetchone()
        if not exists:
            return []
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(careers)')}
        id_col = next((c for c in ('career_id', 'id') if c in cols), None)
        name_col = next((c for c in ('career_name', 'name', 'title') if c in cols), None)
        if not id_col or not name_col:
            return []
        # Column names are selected only from the fixed allow-list above.
        return conn.execute(f'SELECT "{id_col}" AS career_id, "{name_col}" AS career_name FROM careers ORDER BY "{name_col}"').fetchall()

@app.get('/profile', response_class=HTMLResponse)
def profile_page(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    return render(request, 'profile.html', student=student, error=None, success=None)

@app.post('/profile', response_class=HTMLResponse)
def save_profile(request: Request, csrf: str = Form(...), first_name: str = Form(...), last_name: str = Form(...),
                 current_grade: int = Form(...), graduation_year: int = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    first_name, last_name = first_name.strip(), last_name.strip()
    if not csrf_ok(request, csrf):
        return render(request, 'profile.html', student=student, error='Your form expired. Please refresh and try again.', success=None)
    if not first_name or not last_name or current_grade not in (9,10,11,12) or not 2026 <= graduation_year <= 2045:
        return render(request, 'profile.html', student={**dict(student), 'first_name': first_name, 'last_name': last_name, 'current_grade': current_grade, 'graduation_year': graduation_year}, error='Enter a first and last name, grade 9–12, and a valid graduation year.', success=None)
    with db_connection() as conn:
        conn.execute('UPDATE students SET first_name=?, last_name=?, current_grade=?, graduation_year=?, updated_at=CURRENT_TIMESTAMP WHERE student_id=?',
                     (first_name, last_name, current_grade, graduation_year, student['student_id']))
        conn.commit()
    student = get_student_for_user(user['user_id'])
    return render(request, 'profile.html', student=student, error=None, success='Your profile was saved.')

@app.get('/interests', response_class=HTMLResponse)
def interests_page(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    with db_connection() as conn:
        interests = conn.execute('SELECT * FROM student_interests WHERE student_id=? ORDER BY created_at DESC, interest_id DESC', (student['student_id'],)).fetchall()
    return render(request, 'interests.html', student=student, interests=interests, error=None, success=None, edit_interest=None)

@app.get('/interests/{interest_id}/edit', response_class=HTMLResponse)
def edit_interest_page(request: Request, interest_id: int):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    with db_connection() as conn:
        interests = conn.execute('SELECT * FROM student_interests WHERE student_id=? ORDER BY created_at DESC, interest_id DESC', (student['student_id'],)).fetchall()
        edit_interest = conn.execute('SELECT * FROM student_interests WHERE interest_id=? AND student_id=?', (interest_id, student['student_id'])).fetchone()
    if not edit_interest: return RedirectResponse('/interests', status_code=303)
    return render(request, 'interests.html', student=student, interests=interests, error=None, success=None, edit_interest=edit_interest)

@app.post('/interests/save', response_class=HTMLResponse)
def save_interest(request: Request, csrf: str = Form(...), interest_name: str = Form(...),
                  interest_category: str = Form('subject'), interest_level: int = Form(2), notes: str = Form(''),
                  interest_id: int = Form(0)):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    allowed = {'subject','activity','skill','topic','other'}
    name = interest_name.strip(); category = interest_category if interest_category in allowed else 'other'
    if not csrf_ok(request, csrf): error = 'Your form expired. Please refresh and try again.'
    elif not name or len(name) > 120: error = 'Enter an interest between 1 and 120 characters.'
    elif interest_level not in (1,2,3): error = 'Choose an interest level.'
    else: error = None
    if not error:
        with db_connection() as conn:
            if interest_id:
                cur = conn.execute('UPDATE student_interests SET interest_category=?, interest_name=?, interest_level=?, notes=? WHERE interest_id=? AND student_id=?',
                                   (category,name,interest_level,notes.strip()[:500],interest_id,student['student_id']))
                if cur.rowcount == 0: error = 'That interest could not be found.'
            else:
                conn.execute('INSERT INTO student_interests (student_id,interest_category,interest_name,interest_level,notes) VALUES (?,?,?,?,?)',
                             (student['student_id'],category,name,interest_level,notes.strip()[:500]))
            conn.commit()
    with db_connection() as conn:
        interests = conn.execute('SELECT * FROM student_interests WHERE student_id=? ORDER BY created_at DESC, interest_id DESC', (student['student_id'],)).fetchall()
    return render(request, 'interests.html', student=student, interests=interests, error=error, success=None if error else 'Interest saved.', edit_interest=None)

@app.post('/interests/{interest_id}/delete')
def delete_interest(request: Request, interest_id: int, csrf: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    if not csrf_ok(request, csrf): return HTMLResponse('Invalid form token. Refresh and try again.', status_code=400)
    with db_connection() as conn:
        conn.execute('DELETE FROM student_interests WHERE interest_id=? AND student_id=?', (interest_id, student['student_id']))
        conn.commit()
    return RedirectResponse('/interests', status_code=303)

@app.get('/goals', response_class=HTMLResponse)
def goals_page(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    with db_connection() as conn:
        goals = conn.execute('SELECT * FROM student_goals WHERE student_id=? ORDER BY priority ASC, goal_id DESC', (student['student_id'],)).fetchall()
    return render(request, 'goals.html', student=student, goals=goals, careers=get_careers(), error=None, success=None, edit_goal=None)

@app.get('/goals/{goal_id}/edit', response_class=HTMLResponse)
def edit_goal_page(request: Request, goal_id: int):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    with db_connection() as conn:
        goals = conn.execute('SELECT * FROM student_goals WHERE student_id=? ORDER BY priority ASC, goal_id DESC', (student['student_id'],)).fetchall()
        edit_goal = conn.execute('SELECT * FROM student_goals WHERE goal_id=? AND student_id=?', (goal_id, student['student_id'])).fetchone()
    if not edit_goal: return RedirectResponse('/goals', status_code=303)
    return render(request, 'goals.html', student=student, goals=goals, careers=get_careers(), error=None, success=None, edit_goal=edit_goal)

@app.post('/goals/save', response_class=HTMLResponse)
def save_goal(request: Request, csrf: str = Form(...), goal_type: str = Form(...), title: str = Form(...),
              description: str = Form(''), career_id: str = Form(''), institution_name: str = Form(''),
              intended_major: str = Form(''), degree_level: str = Form(''), priority: int = Form(2),
              target_year: str = Form(''), status: str = Form('active'), goal_id: int = Form(0)):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    title = title.strip(); goal_type = goal_type.strip(); status = status.strip()
    try: target = int(target_year) if target_year.strip() else None
    except ValueError: target = -1
    if not csrf_ok(request, csrf): error = 'Your form expired. Please refresh and try again.'
    elif goal_type not in ('career','college'): error = 'Choose a career or college goal.'
    elif not title or len(title) > 160: error = 'Enter a goal title between 1 and 160 characters.'
    elif priority not in (1,2,3): error = 'Choose a valid priority.'
    elif status not in ('active','achieved','paused'): error = 'Choose a valid status.'
    elif target == -1 or (target is not None and not 2026 <= target <= 2045): error = 'Enter a target year between 2026 and 2045.'
    else: error = None
    if not error:
        career_value = career_id.strip() or None
        # Avoid storing a career ID that does not exist when a career catalog is available.
        careers = get_careers()
        if career_value and careers and career_value not in {str(c['career_id']) for c in careers}:
            error = 'Choose a career from the list.'
        else:
            with db_connection() as conn:
                goal_values = {
                    'goal_type': goal_type,
                    'title': title,
                    'description': description.strip()[:2000],
                    'career_id': career_value,
                    'institution_name': institution_name.strip()[:160] or None,
                    'intended_major': intended_major.strip()[:160] or None,
                    'degree_level': degree_level.strip()[:80] or None,
                    'priority': priority,
                    'target_year': target,
                    'status': status,
                }
                if goal_id:
                    assignments = ', '.join(f"{column}=?" for column in goal_values)
                    values = list(goal_values.values()) + [goal_id, student['student_id']]
                    cur = conn.execute(
                        f"UPDATE student_goals SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE goal_id=? AND student_id=?",
                        values,
                    )
                    if cur.rowcount == 0:
                        error = 'That goal could not be found.'
                else:
                    insert_values = {'student_id': student['student_id'], **goal_values}
                    names = ', '.join(insert_values)
                    placeholders = ', '.join('?' for _ in insert_values)
                    conn.execute(
                        f"INSERT INTO student_goals ({names}) VALUES ({placeholders})",
                        list(insert_values.values()),
                    )
                conn.commit()
    with db_connection() as conn:
        goals = conn.execute('SELECT * FROM student_goals WHERE student_id=? ORDER BY priority ASC, goal_id DESC', (student['student_id'],)).fetchall()
    return render(request, 'goals.html', student=student, goals=goals, careers=get_careers(), error=error,
                  success=None if error else 'Goal saved.', edit_goal=None)

@app.post('/goals/{goal_id}/delete')
def delete_goal(request: Request, goal_id: int, csrf: str = Form(...)):
    user = current_user(request)
    if not user: return RedirectResponse('/login', status_code=303)
    student = get_student_for_user(user['user_id'])
    if not student: return RedirectResponse('/login', status_code=303)
    if not csrf_ok(request, csrf): return HTMLResponse('Invalid form token. Refresh and try again.', status_code=400)
    with db_connection() as conn:
        conn.execute('DELETE FROM student_goals WHERE goal_id=? AND student_id=?', (goal_id, student['student_id']))
        conn.commit()
    return RedirectResponse('/goals', status_code=303)
