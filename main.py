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
