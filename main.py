import hmac, os, secrets, sqlite3, time
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
