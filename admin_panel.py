"""
Веб-адмін панель для monobank-bot.
Запускається окремо від бота, спільна SQLite база даних.

Env vars:
  DB_PATH        — шлях до БД (default: bot.db)
  ADMIN_PASSWORD — пароль для входу (обов'язково встановити!)
  ADMIN_SECRET   — секретний ключ для Flask сесій (або генерується автоматично)
  PORT           — порт (default: 8080)
"""

import os
import sqlite3
import secrets
from datetime import date, datetime
from contextlib import contextmanager
from functools import wraps

from flask import (
    Flask, render_template_string, request, redirect,
    url_for, session, flash, jsonify,
)

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH        = os.getenv("DB_PATH", "bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY     = os.getenv("ADMIN_SECRET", secrets.token_hex(32))
PORT           = int(os.getenv("PORT", 8080))
BASE_DAILY_LIMIT = 1

app = Flask(__name__)
app.secret_key = SECRET_KEY

if not ADMIN_PASSWORD:
    import warnings
    warnings.warn("ADMIN_PASSWORD not set! Admin panel is insecure.", stacklevel=1)

# ── DB helpers ────────────────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_stats() -> dict:
    with _db() as conn:
        total_users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        today       = date.today().isoformat()
        active_today = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM daily_usage WHERE date=?", (today,)
        ).fetchone()["n"]
        statements_today = conn.execute(
            "SELECT COALESCE(SUM(count),0) AS n FROM daily_usage WHERE date=?", (today,)
        ).fetchone()["n"]
        total_refs = conn.execute("SELECT COUNT(*) AS n FROM referrals").fetchone()["n"]
        total_promos = conn.execute("SELECT COUNT(*) AS n FROM promo_codes").fetchone()["n"]
    return {
        "total_users":     total_users,
        "active_today":    active_today,
        "statements_today": statements_today,
        "total_refs":      total_refs,
        "total_promos":    total_promos,
    }


def get_users(search: str = "", page: int = 1, per_page: int = 25) -> tuple[list, int]:
    offset = (page - 1) * per_page
    with _db() as conn:
        if search:
            like = f"%{search}%"
            rows = conn.execute(
                "SELECT u.user_id, u.username, u.first_name, u.extra_limit, u.created_at, "
                "  (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id=u.user_id) AS refs, "
                "  (SELECT COALESCE(SUM(bonus),0) FROM user_promos p WHERE p.user_id=u.user_id) AS promo_bonus, "
                "  (SELECT COALESCE(count,0) FROM daily_usage d WHERE d.user_id=u.user_id AND d.date=?) AS today_usage "
                "FROM users u "
                "WHERE u.username LIKE ? OR u.first_name LIKE ? OR CAST(u.user_id AS TEXT) LIKE ? "
                "ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
                (date.today().isoformat(), like, like, like, per_page, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?",
                (like, like, like),
            ).fetchone()["n"]
        else:
            rows = conn.execute(
                "SELECT u.user_id, u.username, u.first_name, u.extra_limit, u.created_at, "
                "  (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id=u.user_id) AS refs, "
                "  (SELECT COALESCE(SUM(bonus),0) FROM user_promos p WHERE p.user_id=u.user_id) AS promo_bonus, "
                "  (SELECT COALESCE(count,0) FROM daily_usage d WHERE d.user_id=u.user_id AND d.date=?) AS today_usage "
                "FROM users u "
                "ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
                (date.today().isoformat(), per_page, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    users = []
    for r in rows:
        daily_limit = BASE_DAILY_LIMIT + r["refs"] + r["extra_limit"] + r["promo_bonus"]
        users.append({
            "user_id":    r["user_id"],
            "username":   r["username"] or "",
            "first_name": r["first_name"] or "",
            "extra_limit": r["extra_limit"],
            "created_at": str(r["created_at"])[:10],
            "refs":        r["refs"],
            "promo_bonus": r["promo_bonus"],
            "today_usage": r["today_usage"],
            "daily_limit": daily_limit,
        })
    return users, total


def get_promos() -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT p.code, p.bonus, p.uses_left, p.created_at, "
            "  (SELECT COUNT(*) FROM user_promos up WHERE up.code=p.code) AS used_count "
            "FROM promo_codes p ORDER BY p.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def db_set_extra(user_id: int, value: int):
    with _db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, extra_limit) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET extra_limit=excluded.extra_limit",
            (user_id, value),
        )


def db_add_extra(user_id: int, delta: int):
    with _db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, extra_limit) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET extra_limit=MAX(0, extra_limit+?)",
            (user_id, max(0, delta), delta),
        )


def db_create_promo(code: str, bonus: int, uses_left: int):
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO promo_codes (code, bonus, uses_left) VALUES (?,?,?)",
            (code.upper().strip(), bonus, uses_left),
        )


def db_delete_promo(code: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM promo_codes WHERE code=?", (code.upper().strip(),))
        return cur.rowcount > 0


# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated

# ── Templates ─────────────────────────────────────────────────────────────────

BASE_HTML = """<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Адмін{% endblock %} — BankBot</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    body { background: #f0f2f5; }
    .sidebar { min-height: 100vh; background: #1a1d23; }
    .sidebar .nav-link { color: #adb5bd; border-radius: 8px; margin: 2px 0; }
    .sidebar .nav-link:hover, .sidebar .nav-link.active { background: #2c3040; color: #fff; }
    .sidebar .brand { color: #fff; font-weight: 700; font-size: 1.1rem; }
    .stat-card { border: none; border-radius: 12px; }
    .stat-icon { width: 48px; height: 48px; border-radius: 12px; display:flex; align-items:center; justify-content:center; font-size:1.4rem; }
    .table th { font-size: .78rem; text-transform: uppercase; letter-spacing: .05em; color: #6c757d; font-weight: 600; }
    .badge-limit { background: #e8f5e9; color: #2e7d32; }
    .badge-used { background: #fff3e0; color: #e65100; }
  </style>
</head>
<body>
<div class="d-flex">
  <div class="sidebar p-3 d-flex flex-column" style="width:220px;min-width:220px;">
    <div class="brand mb-4 px-2 pt-1">🏦 BankBot Admin</div>
    <nav class="nav flex-column gap-1 flex-grow-1">
      <a href="{{ url_for('dashboard') }}" class="nav-link {% if request.endpoint=='dashboard' %}active{% endif %}">
        <i class="bi bi-speedometer2 me-2"></i>Дашборд
      </a>
      <a href="{{ url_for('users_page') }}" class="nav-link {% if request.endpoint=='users_page' %}active{% endif %}">
        <i class="bi bi-people me-2"></i>Користувачі
      </a>
      <a href="{{ url_for('promos_page') }}" class="nav-link {% if request.endpoint=='promos_page' %}active{% endif %}">
        <i class="bi bi-ticket-perforated me-2"></i>Промокоди
      </a>
    </nav>
    <a href="{{ url_for('logout') }}" class="nav-link text-danger mt-2">
      <i class="bi bi-box-arrow-left me-2"></i>Вийти
    </a>
  </div>

  <div class="flex-grow-1 p-4" style="min-width:0">
    {% with msgs = get_flashed_messages(with_categories=true) %}
      {% for cat, msg in msgs %}
        <div class="alert alert-{{ 'success' if cat=='success' else 'danger' }} alert-dismissible fade show" role="alert">
          {{ msg }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
      {% endfor %}
    {% endwith %}
    {% block content %}{% endblock %}
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>"""

LOGIN_HTML = """<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вхід — BankBot Admin</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>body{background:#f0f2f5;}</style>
</head>
<body class="d-flex align-items-center justify-content-center" style="min-height:100vh">
  <div class="card shadow-sm p-4" style="width:340px;border-radius:16px">
    <div class="text-center mb-4">
      <div style="font-size:2.5rem">🏦</div>
      <h5 class="fw-bold mt-1">BankBot Admin</h5>
      <div class="text-muted small">Введи пароль для входу</div>
    </div>
    {% if error %}
      <div class="alert alert-danger py-2 small">{{ error }}</div>
    {% endif %}
    <form method="POST">
      <div class="mb-3">
        <input type="password" name="password" class="form-control" placeholder="Пароль" autofocus required>
      </div>
      <button type="submit" class="btn btn-primary w-100">Увійти</button>
    </form>
  </div>
</body>
</html>"""

DASHBOARD_HTML = """{% extends base %}
{% block title %}Дашборд{% endblock %}
{% block content %}
<h4 class="fw-bold mb-4">📊 Дашборд</h4>

<div class="row g-3 mb-4">
  {% set cards = [
    ('bi-people-fill', '#4f46e5', '#ede9fe', 'Всього користувачів', stats.total_users),
    ('bi-person-check-fill', '#0891b2', '#e0f2fe', 'Активних сьогодні', stats.active_today),
    ('bi-file-earmark-text-fill', '#059669', '#d1fae5', 'Виписок сьогодні', stats.statements_today),
    ('bi-share-fill', '#d97706', '#fef3c7', 'Всього рефералів', stats.total_refs),
    ('bi-ticket-perforated-fill', '#7c3aed', '#f3e8ff', 'Промокодів', stats.total_promos),
  ] %}
  {% for icon, color, bg, label, value in cards %}
  <div class="col-sm-6 col-xl-4">
    <div class="card stat-card shadow-sm p-3 h-100">
      <div class="d-flex align-items-center gap-3">
        <div class="stat-icon" style="background:{{ bg }};color:{{ color }}">
          <i class="bi {{ icon }}"></i>
        </div>
        <div>
          <div class="text-muted small">{{ label }}</div>
          <div class="fw-bold fs-4">{{ value }}</div>
        </div>
      </div>
    </div>
  </div>
  {% endfor %}
</div>

<div class="row g-3">
  <div class="col-lg-6">
    <div class="card shadow-sm p-3" style="border-radius:12px">
      <h6 class="fw-bold mb-3">⚡ Швидкі дії</h6>
      <div class="d-flex flex-wrap gap-2">
        <a href="{{ url_for('users_page') }}" class="btn btn-outline-primary btn-sm">
          <i class="bi bi-people me-1"></i>Всі користувачі
        </a>
        <a href="{{ url_for('promos_page') }}" class="btn btn-outline-purple btn-sm" style="color:#7c3aed;border-color:#7c3aed">
          <i class="bi bi-ticket-perforated me-1"></i>Промокоди
        </a>
        <a href="{{ url_for('promos_page') }}#add-promo" class="btn btn-success btn-sm">
          <i class="bi bi-plus-circle me-1"></i>Новий промокод
        </a>
      </div>
    </div>
  </div>
  <div class="col-lg-6">
    <div class="card shadow-sm p-3" style="border-radius:12px">
      <h6 class="fw-bold mb-3">ℹ️ Базовий ліміт</h6>
      <p class="text-muted small mb-0">
        Кожен користувач отримує <strong>{{ base_limit }}</strong> виписку/день базово.<br>
        +1 за кожного залученого реферала.<br>
        +N за адмін-бонус або промокод.
      </p>
    </div>
  </div>
</div>
{% endblock %}"""

USERS_HTML = """{% extends base %}
{% block title %}Користувачі{% endblock %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="fw-bold mb-0">👥 Користувачі</h4>
  <span class="text-muted small">Всього: {{ total }}</span>
</div>

<div class="card shadow-sm mb-3" style="border-radius:12px">
  <div class="card-body p-3">
    <form class="d-flex gap-2" method="GET">
      <input type="text" name="q" value="{{ search }}" class="form-control form-control-sm" placeholder="Пошук за username, ім'ям або ID...">
      <button class="btn btn-primary btn-sm px-3">Пошук</button>
      {% if search %}<a href="{{ url_for('users_page') }}" class="btn btn-outline-secondary btn-sm">✕</a>{% endif %}
    </form>
  </div>
</div>

<div class="card shadow-sm" style="border-radius:12px;overflow:hidden">
  <div class="table-responsive">
    <table class="table table-hover align-middle mb-0">
      <thead class="table-light">
        <tr>
          <th>ID</th><th>Користувач</th><th>Ліміт</th>
          <th>Сьогодні</th><th>Реф.</th><th>Реєстрація</th><th></th>
        </tr>
      </thead>
      <tbody>
        {% for u in users %}
        <tr>
          <td><code class="small">{{ u.user_id }}</code></td>
          <td>
            <div class="fw-semibold">{{ u.first_name or '—' }}</div>
            {% if u.username %}<div class="text-muted small">@{{ u.username }}</div>{% endif %}
          </td>
          <td>
            <span class="badge badge-limit rounded-pill">{{ u.daily_limit }}/день</span>
            {% if u.extra_limit or u.promo_bonus %}
            <div class="text-muted" style="font-size:.72rem">
              {% if u.extra_limit %}+{{ u.extra_limit }} адмін{% endif %}
              {% if u.promo_bonus %}+{{ u.promo_bonus }} промо{% endif %}
            </div>
            {% endif %}
          </td>
          <td>
            <span class="{% if u.today_usage >= u.daily_limit %}text-danger{% else %}text-success{% endif %} fw-semibold">
              {{ u.today_usage }}/{{ u.daily_limit }}
            </span>
          </td>
          <td>{{ u.refs }}</td>
          <td class="text-muted small">{{ u.created_at }}</td>
          <td>
            <button class="btn btn-outline-secondary btn-sm" data-bs-toggle="modal" data-bs-target="#editModal"
              data-uid="{{ u.user_id }}" data-name="{{ u.first_name or u.username or u.user_id }}"
              data-extra="{{ u.extra_limit }}">
              <i class="bi bi-pencil"></i>
            </button>
          </td>
        </tr>
        {% endfor %}
        {% if not users %}
        <tr><td colspan="7" class="text-center text-muted py-4">Нічого не знайдено</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>
</div>

{% if total_pages > 1 %}
<nav class="mt-3">
  <ul class="pagination pagination-sm justify-content-center mb-0">
    {% for p in range(1, total_pages+1) %}
    <li class="page-item {% if p == page %}active{% endif %}">
      <a class="page-link" href="{{ url_for('users_page', q=search, page=p) }}">{{ p }}</a>
    </li>
    {% endfor %}
  </ul>
</nav>
{% endif %}

<!-- Edit modal -->
<div class="modal fade" id="editModal" tabindex="-1">
  <div class="modal-dialog modal-sm">
    <div class="modal-content" style="border-radius:12px">
      <div class="modal-header border-0 pb-0">
        <h6 class="modal-title fw-bold">Редагувати ліміт</h6>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <form method="POST" action="{{ url_for('set_user_limit') }}">
        <div class="modal-body">
          <input type="hidden" name="user_id" id="modalUserId">
          <p class="text-muted small mb-3" id="modalUserName"></p>
          <label class="form-label small fw-semibold">Адмін-бонус (extra_limit)</label>
          <input type="number" name="extra_limit" id="modalExtraLimit" class="form-control" min="0" required>
          <div class="text-muted mt-2" style="font-size:.75rem">
            Загальний ліміт = базовий ({{ base_limit }}) + реферали + extra_limit + промо-бонус
          </div>
        </div>
        <div class="modal-footer border-0 pt-0">
          <button class="btn btn-primary btn-sm w-100">Зберегти</button>
        </div>
      </form>
    </div>
  </div>
</div>

<script>
document.getElementById('editModal').addEventListener('show.bs.modal', function(e) {
  var btn = e.relatedTarget;
  document.getElementById('modalUserId').value = btn.dataset.uid;
  document.getElementById('modalUserName').textContent = 'ID: ' + btn.dataset.uid + ' — ' + btn.dataset.name;
  document.getElementById('modalExtraLimit').value = btn.dataset.extra;
});
</script>
{% endblock %}"""

PROMOS_HTML = """{% extends base %}
{% block title %}Промокоди{% endblock %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="fw-bold mb-0">🎟️ Промокоди</h4>
</div>

<div class="row g-3">
  <!-- Add promo form -->
  <div class="col-lg-4" id="add-promo">
    <div class="card shadow-sm p-3" style="border-radius:12px">
      <h6 class="fw-bold mb-3">➕ Новий промокод</h6>
      <form method="POST" action="{{ url_for('add_promo') }}">
        <div class="mb-2">
          <label class="form-label small fw-semibold">Код</label>
          <input type="text" name="code" class="form-control form-control-sm text-uppercase" placeholder="PROMO2024" required>
        </div>
        <div class="mb-2">
          <label class="form-label small fw-semibold">Бонус (виписок)</label>
          <input type="number" name="bonus" class="form-control form-control-sm" placeholder="5" min="1" required>
        </div>
        <div class="mb-3">
          <label class="form-label small fw-semibold">К-сть використань (-1 = без ліміту)</label>
          <input type="number" name="uses_left" class="form-control form-control-sm" value="-1" min="-1" required>
        </div>
        <button type="submit" class="btn btn-success btn-sm w-100">Створити промокод</button>
      </form>
    </div>
  </div>

  <!-- Promos list -->
  <div class="col-lg-8">
    <div class="card shadow-sm" style="border-radius:12px;overflow:hidden">
      <div class="table-responsive">
        <table class="table table-hover align-middle mb-0">
          <thead class="table-light">
            <tr>
              <th>Код</th><th>Бонус</th><th>Залишилось</th><th>Використано</th><th>Створено</th><th></th>
            </tr>
          </thead>
          <tbody>
            {% for p in promos %}
            <tr>
              <td><code class="fw-semibold">{{ p.code }}</code></td>
              <td><span class="badge" style="background:#e8f5e9;color:#2e7d32">+{{ p.bonus }} вип.</span></td>
              <td>{{ '∞' if p.uses_left < 0 else p.uses_left }}</td>
              <td>{{ p.used_count }}</td>
              <td class="text-muted small">{{ p.created_at[:10] if p.created_at else '—' }}</td>
              <td>
                <form method="POST" action="{{ url_for('delete_promo', code=p.code) }}"
                      onsubmit="return confirm('Видалити промокод {{ p.code }}?')">
                  <button class="btn btn-outline-danger btn-sm"><i class="bi bi-trash"></i></button>
                </form>
              </td>
            </tr>
            {% endfor %}
            {% if not promos %}
            <tr><td colspan="6" class="text-center text-muted py-4">Промокодів ще немає</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>
{% endblock %}"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if ADMIN_PASSWORD and pwd == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        else:
            error = "Невірний пароль."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    stats = get_stats()
    return render_template_string(
        DASHBOARD_HTML, base=BASE_HTML, stats=stats, base_limit=BASE_DAILY_LIMIT
    )


@app.route("/users")
@login_required
def users_page():
    search   = request.args.get("q", "").strip()
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 25
    users, total = get_users(search, page, per_page)
    total_pages  = (total + per_page - 1) // per_page
    return render_template_string(
        USERS_HTML,
        base=BASE_HTML,
        users=users,
        total=total,
        total_pages=total_pages,
        page=page,
        search=search,
        base_limit=BASE_DAILY_LIMIT,
    )


@app.route("/users/setlimit", methods=["POST"])
@login_required
def set_user_limit():
    try:
        user_id     = int(request.form["user_id"])
        extra_limit = int(request.form["extra_limit"])
        if extra_limit < 0:
            raise ValueError("extra_limit must be >= 0")
        db_set_extra(user_id, extra_limit)
        flash(f"✅ Ліміт для {user_id} встановлено: extra={extra_limit}", "success")
    except (KeyError, ValueError, Exception) as e:
        flash(f"❌ Помилка: {e}", "danger")
    return redirect(request.referrer or url_for("users_page"))


@app.route("/promos")
@login_required
def promos_page():
    promos = get_promos()
    return render_template_string(PROMOS_HTML, base=BASE_HTML, promos=promos)


@app.route("/promos/add", methods=["POST"])
@login_required
def add_promo():
    try:
        code      = request.form["code"].upper().strip()
        bonus     = int(request.form["bonus"])
        uses_left = int(request.form["uses_left"])
        if not code:
            raise ValueError("Код не може бути порожнім")
        if bonus < 1:
            raise ValueError("Бонус має бути ≥ 1")
        db_create_promo(code, bonus, uses_left)
        uses_str = str(uses_left) if uses_left >= 0 else "∞"
        flash(f"✅ Промокод {code} створено (+{bonus} вип., використань: {uses_str})", "success")
    except Exception as e:
        flash(f"❌ Помилка: {e}", "danger")
    return redirect(url_for("promos_page"))


@app.route("/promos/<code>/delete", methods=["POST"])
@login_required
def delete_promo(code: str):
    ok = db_delete_promo(code)
    if ok:
        flash(f"✅ Промокод {code} видалено.", "success")
    else:
        flash(f"❌ Промокод {code} не знайдено.", "danger")
    return redirect(url_for("promos_page"))


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Admin panel starting on http://0.0.0.0:{PORT}")
    if not ADMIN_PASSWORD:
        print("⚠️  WARNING: ADMIN_PASSWORD is not set!")
    app.run(host="0.0.0.0", port=PORT, debug=False)
