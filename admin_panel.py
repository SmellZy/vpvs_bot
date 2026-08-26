"""
Веб-адмін панель для monobank-bot.

Env vars:
  DB_PATH        — шлях до БД (default: bot.db)
  ADMIN_PASSWORD — пароль для входу (обов'язково встановити!)
  ADMIN_SECRET   — секретний ключ для Flask сесій
  PORT           — порт (default: 8080)
"""

import os
import sqlite3
import secrets
from datetime import date
from contextlib import contextmanager
from functools import wraps

from flask import Flask, render_template_string, request, redirect, url_for, session, flash

DB_PATH          = os.getenv("DB_PATH", "bot.db")
ADMIN_PASSWORD   = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY       = os.getenv("ADMIN_SECRET", secrets.token_hex(32))
PORT             = int(os.getenv("PORT", 8080))
BASE_DAILY_LIMIT = 1

app = Flask(__name__)
app.secret_key = SECRET_KEY

if not ADMIN_PASSWORD:
    import warnings
    warnings.warn("ADMIN_PASSWORD not set! Admin panel is insecure.", stacklevel=1)

# ── DB ────────────────────────────────────────────────────────────────────────

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
    today = date.today().isoformat()
    with _db() as conn:
        total_users      = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        active_today     = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM daily_usage WHERE date=?", (today,)
        ).fetchone()["n"]
        statements_today = conn.execute(
            "SELECT COALESCE(SUM(count),0) AS n FROM daily_usage WHERE date=?", (today,)
        ).fetchone()["n"]
        total_refs   = conn.execute("SELECT COUNT(*) AS n FROM referrals").fetchone()["n"]
        total_promos = conn.execute("SELECT COUNT(*) AS n FROM promo_codes").fetchone()["n"]
    return {
        "total_users":      total_users,
        "active_today":     active_today,
        "statements_today": statements_today,
        "total_refs":       total_refs,
        "total_promos":     total_promos,
    }


def get_users(search: str = "", page: int = 1, per_page: int = 25):
    offset = (page - 1) * per_page
    today  = date.today().isoformat()
    with _db() as conn:
        if search:
            like = f"%{search}%"
            rows = conn.execute(
                "SELECT u.user_id, u.username, u.first_name, u.extra_limit, u.created_at,"
                "  (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id=u.user_id) AS refs,"
                "  (SELECT COALESCE(SUM(bonus),0) FROM user_promos p WHERE p.user_id=u.user_id) AS promo_bonus,"
                "  COALESCE((SELECT d.count FROM daily_usage d WHERE d.user_id=u.user_id AND d.date=?), 0) AS today_usage"
                " FROM users u"
                " WHERE u.username LIKE ? OR u.first_name LIKE ? OR CAST(u.user_id AS TEXT) LIKE ?"
                " ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
                (today, like, like, like, per_page, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM users"
                " WHERE username LIKE ? OR first_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?",
                (like, like, like),
            ).fetchone()["n"]
        else:
            rows = conn.execute(
                "SELECT u.user_id, u.username, u.first_name, u.extra_limit, u.created_at,"
                "  (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id=u.user_id) AS refs,"
                "  (SELECT COALESCE(SUM(bonus),0) FROM user_promos p WHERE p.user_id=u.user_id) AS promo_bonus,"
                "  COALESCE((SELECT d.count FROM daily_usage d WHERE d.user_id=u.user_id AND d.date=?), 0) AS today_usage"
                " FROM users u"
                " ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
                (today, per_page, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    users = []
    for r in rows:
        refs        = r["refs"]        or 0
        extra       = r["extra_limit"] or 0
        promo       = r["promo_bonus"] or 0
        today_usage = r["today_usage"] or 0
        daily_limit = BASE_DAILY_LIMIT + refs + extra + promo
        users.append({
            "user_id":     r["user_id"],
            "username":    r["username"] or "",
            "first_name":  r["first_name"] or "",
            "extra_limit": extra,
            "created_at":  str(r["created_at"] or "")[:10],
            "refs":        refs,
            "promo_bonus": promo,
            "today_usage": today_usage,
            "daily_limit": daily_limit,
        })
    return users, total


def get_promos() -> list:
    with _db() as conn:
        rows = conn.execute(
            "SELECT p.code, p.bonus, p.uses_left, p.created_at,"
            "  (SELECT COUNT(*) FROM user_promos up WHERE up.code=p.code) AS used_count"
            " FROM promo_codes p ORDER BY p.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def db_set_extra(user_id: int, value: int):
    with _db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, extra_limit) VALUES (?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET extra_limit=excluded.extra_limit",
            (user_id, value),
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

# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated

# ── Layout helper ─────────────────────────────────────────────────────────────

def _layout(title: str, active: str, body: str) -> str:
    """Wrap a body HTML string in the common admin layout."""
    alerts = ""
    for category, msg in session.pop("_flashes", []):
        color = "success" if category == "success" else "danger"
        alerts += (
            f'<div class="alert alert-{color} alert-dismissible fade show" role="alert">'
            f'{msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>'
        )
    # Use Flask's get_flashed_messages instead of session directly
    from flask import get_flashed_messages
    for category, msg in get_flashed_messages(with_categories=True):
        color = "success" if category == "success" else "danger"
        alerts += (
            f'<div class="alert alert-{color} alert-dismissible fade show" role="alert">'
            f'{msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>'
        )

    nav_items = [
        ("dashboard", "bi-speedometer2", "Дашборд"),
        ("users_page", "bi-people", "Користувачі"),
        ("promos_page", "bi-ticket-perforated", "Промокоди"),
    ]
    nav_html = ""
    for endpoint, icon, label in nav_items:
        is_active = "active" if active == endpoint else ""
        nav_html += (
            f'<a href="{url_for(endpoint)}" class="nav-link {is_active}">'
            f'<i class="bi {icon} me-2"></i>{label}</a>'
        )

    return f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — BankBot Admin</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    body {{ background:#f0f2f5; }}
    .sidebar {{ min-height:100vh; background:#1a1d23; width:220px; min-width:220px; }}
    .sidebar .nav-link {{ color:#adb5bd; border-radius:8px; margin:2px 0; }}
    .sidebar .nav-link:hover, .sidebar .nav-link.active {{ background:#2c3040; color:#fff; }}
    .sidebar .brand {{ color:#fff; font-weight:700; font-size:1.1rem; }}
    .stat-card {{ border:none; border-radius:12px; }}
    .stat-icon {{ width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:1.4rem; }}
    .table th {{ font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:#6c757d;font-weight:600; }}
  </style>
</head>
<body>
<div class="d-flex">
  <div class="sidebar p-3 d-flex flex-column">
    <div class="brand mb-4 px-2 pt-1">🏦 BankBot Admin</div>
    <nav class="nav flex-column gap-1 flex-grow-1">{nav_html}</nav>
    <a href="{url_for('logout')}" class="nav-link text-danger mt-2">
      <i class="bi bi-box-arrow-left me-2"></i>Вийти
    </a>
  </div>
  <div class="flex-grow-1 p-4" style="min-width:0">
    {alerts}
    {body}
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("dashboard"))
    error = ""
    if request.method == "POST":
        if ADMIN_PASSWORD and request.form.get("password") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = '<div class="alert alert-danger py-2 small">Невірний пароль.</div>'

    return f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Вхід — BankBot Admin</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>body{{background:#f0f2f5;}}</style>
</head>
<body class="d-flex align-items-center justify-content-center" style="min-height:100vh">
  <div class="card shadow-sm p-4" style="width:340px;border-radius:16px">
    <div class="text-center mb-4">
      <div style="font-size:2.5rem">🏦</div>
      <h5 class="fw-bold mt-1">BankBot Admin</h5>
      <div class="text-muted small">Введи пароль для входу</div>
    </div>
    {error}
    <form method="POST">
      <div class="mb-3">
        <input type="password" name="password" class="form-control" placeholder="Пароль" autofocus required>
      </div>
      <button type="submit" class="btn btn-primary w-100">Увійти</button>
    </form>
  </div>
</body>
</html>"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    s = get_stats()
    cards = [
        ("#4f46e5", "#ede9fe", "bi-people-fill",              "Всього користувачів",  s["total_users"]),
        ("#0891b2", "#e0f2fe", "bi-person-check-fill",        "Активних сьогодні",    s["active_today"]),
        ("#059669", "#d1fae5", "bi-file-earmark-text-fill",   "Виписок сьогодні",     s["statements_today"]),
        ("#d97706", "#fef3c7", "bi-share-fill",               "Всього рефералів",     s["total_refs"]),
        ("#7c3aed", "#f3e8ff", "bi-ticket-perforated-fill",   "Промокодів",           s["total_promos"]),
    ]
    cards_html = ""
    for color, bg, icon, label, value in cards:
        cards_html += f"""
        <div class="col-sm-6 col-xl-4">
          <div class="card stat-card shadow-sm p-3 h-100">
            <div class="d-flex align-items-center gap-3">
              <div class="stat-icon" style="background:{bg};color:{color}">
                <i class="bi {icon}"></i>
              </div>
              <div>
                <div class="text-muted small">{label}</div>
                <div class="fw-bold fs-4">{value}</div>
              </div>
            </div>
          </div>
        </div>"""

    body = f"""
    <h4 class="fw-bold mb-4">📊 Дашборд</h4>
    <div class="row g-3 mb-4">{cards_html}</div>
    <div class="row g-3">
      <div class="col-lg-6">
        <div class="card shadow-sm p-3" style="border-radius:12px">
          <h6 class="fw-bold mb-3">⚡ Швидкі дії</h6>
          <div class="d-flex flex-wrap gap-2">
            <a href="{url_for('users_page')}" class="btn btn-outline-primary btn-sm">
              <i class="bi bi-people me-1"></i>Всі користувачі
            </a>
            <a href="{url_for('promos_page')}" class="btn btn-outline-secondary btn-sm">
              <i class="bi bi-ticket-perforated me-1"></i>Промокоди
            </a>
            <a href="{url_for('promos_page')}#add-promo" class="btn btn-success btn-sm">
              <i class="bi bi-plus-circle me-1"></i>Новий промокод
            </a>
          </div>
        </div>
      </div>
      <div class="col-lg-6">
        <div class="card shadow-sm p-3" style="border-radius:12px">
          <h6 class="fw-bold mb-3">ℹ️ Система лімітів</h6>
          <p class="text-muted small mb-0">
            Базовий ліміт: <strong>{BASE_DAILY_LIMIT}</strong> виписка/день<br>
            +1 за кожного реферала<br>
            +N за адмін-бонус або промокод
          </p>
        </div>
      </div>
    </div>"""
    return _layout("Дашборд", "dashboard", body)


@app.route("/users")
@login_required
def users_page():
    search   = request.args.get("q", "").strip()
    page     = max(1, int(request.args.get("page", 1) or 1))
    per_page = 25
    users, total = get_users(search, page, per_page)
    total_pages  = max(1, (total + per_page - 1) // per_page)

    search_clear = f'<a href="{url_for("users_page")}" class="btn btn-outline-secondary btn-sm">✕</a>' if search else ""

    rows_html = ""
    for u in users:
        edit_btn = (
            f'<button class="btn btn-outline-secondary btn-sm" data-bs-toggle="modal" '
            f'data-bs-target="#editModal" data-uid="{u["user_id"]}" '
            f'data-name="{u["first_name"] or u["username"] or u["user_id"]}" '
            f'data-extra="{u["extra_limit"]}"><i class="bi bi-pencil"></i></button>'
        )
        bonus_info = ""
        if u["extra_limit"] or u["promo_bonus"]:
            parts = []
            if u["extra_limit"]: parts.append(f'+{u["extra_limit"]} адмін')
            if u["promo_bonus"]: parts.append(f'+{u["promo_bonus"]} промо')
            bonus_info = f'<div class="text-muted" style="font-size:.72rem">{", ".join(parts)}</div>'
        usage_color = "text-danger" if u["today_usage"] >= u["daily_limit"] else "text-success"
        uname = f'<div class="text-muted small">@{u["username"]}</div>' if u["username"] else ""
        rows_html += f"""<tr>
          <td><code class="small">{u["user_id"]}</code></td>
          <td><div class="fw-semibold">{u["first_name"] or "—"}</div>{uname}</td>
          <td>
            <span class="badge rounded-pill" style="background:#e8f5e9;color:#2e7d32">{u["daily_limit"]}/день</span>
            {bonus_info}
          </td>
          <td><span class="{usage_color} fw-semibold">{u["today_usage"]}/{u["daily_limit"]}</span></td>
          <td>{u["refs"]}</td>
          <td class="text-muted small">{u["created_at"]}</td>
          <td>{edit_btn}</td>
        </tr>"""

    if not users:
        rows_html = '<tr><td colspan="7" class="text-center text-muted py-4">Нічого не знайдено</td></tr>'

    pagination = ""
    if total_pages > 1:
        pages_html = ""
        for p in range(1, total_pages + 1):
            active = "active" if p == page else ""
            pages_html += f'<li class="page-item {active}"><a class="page-link" href="{url_for("users_page", q=search, page=p)}">{p}</a></li>'
        pagination = f'<nav class="mt-3"><ul class="pagination pagination-sm justify-content-center mb-0">{pages_html}</ul></nav>'

    body = f"""
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h4 class="fw-bold mb-0">👥 Користувачі</h4>
      <span class="text-muted small">Всього: {total}</span>
    </div>
    <div class="card shadow-sm mb-3" style="border-radius:12px">
      <div class="card-body p-3">
        <form class="d-flex gap-2" method="GET">
          <input type="text" name="q" value="{search}" class="form-control form-control-sm" placeholder="Пошук за username, ім'ям або ID...">
          <button class="btn btn-primary btn-sm px-3">Пошук</button>
          {search_clear}
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
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>
    {pagination}

    <div class="modal fade" id="editModal" tabindex="-1">
      <div class="modal-dialog modal-sm">
        <div class="modal-content" style="border-radius:12px">
          <div class="modal-header border-0 pb-0">
            <h6 class="modal-title fw-bold">Редагувати ліміт</h6>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <form method="POST" action="{url_for('set_user_limit')}">
            <div class="modal-body">
              <input type="hidden" name="user_id" id="modalUserId">
              <p class="text-muted small mb-3" id="modalUserName"></p>
              <label class="form-label small fw-semibold">Адмін-бонус (extra_limit)</label>
              <input type="number" name="extra_limit" id="modalExtraLimit" class="form-control" min="0" required>
              <div class="text-muted mt-2" style="font-size:.75rem">
                Загальний = {BASE_DAILY_LIMIT} (базовий) + реферали + extra_limit + промо
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
    document.getElementById('editModal').addEventListener('show.bs.modal', function(e) {{
      var b = e.relatedTarget;
      document.getElementById('modalUserId').value = b.dataset.uid;
      document.getElementById('modalUserName').textContent = 'ID: ' + b.dataset.uid + ' — ' + b.dataset.name;
      document.getElementById('modalExtraLimit').value = b.dataset.extra;
    }});
    </script>"""
    return _layout("Користувачі", "users_page", body)


@app.route("/users/setlimit", methods=["POST"])
@login_required
def set_user_limit():
    try:
        user_id     = int(request.form["user_id"])
        extra_limit = int(request.form["extra_limit"])
        if extra_limit < 0:
            raise ValueError("extra_limit >= 0")
        db_set_extra(user_id, extra_limit)
        flash(f"✅ Ліміт для {user_id} встановлено: extra={extra_limit}", "success")
    except Exception as e:
        flash(f"❌ Помилка: {e}", "danger")
    return redirect(request.referrer or url_for("users_page"))


@app.route("/promos")
@login_required
def promos_page():
    promos = get_promos()

    rows_html = ""
    for p in promos:
        uses_str  = "∞" if p["uses_left"] < 0 else str(p["uses_left"])
        created   = str(p.get("created_at", "") or "")[:10]
        del_url   = url_for("delete_promo", code=p["code"])
        code_val  = p["code"]
        bonus_val = p["bonus"]
        used_val  = p["used_count"]
        rows_html += (
            f'<tr>'
            f'<td><code class="fw-semibold">{code_val}</code></td>'
            f'<td><span class="badge" style="background:#e8f5e9;color:#2e7d32">+{bonus_val} вип.</span></td>'
            f'<td>{uses_str}</td>'
            f'<td>{used_val}</td>'
            f'<td class="text-muted small">{created}</td>'
            f'<td><form method="POST" action="{del_url}" onsubmit="return confirm(\'Видалити {code_val}?\')">'
            f'<button class="btn btn-outline-danger btn-sm"><i class="bi bi-trash"></i></button>'
            f'</form></td>'
            f'</tr>'
        )
    if not promos:
        rows_html = '<tr><td colspan="6" class="text-center text-muted py-4">Промокодів ще немає</td></tr>'

    add_url = url_for("add_promo")
    body = f"""
    <h4 class="fw-bold mb-3">🎟️ Промокоди</h4>
    <div class="row g-3">
      <div class="col-lg-4" id="add-promo">
        <div class="card shadow-sm p-3" style="border-radius:12px">
          <h6 class="fw-bold mb-3">➕ Новий промокод</h6>
          <form method="POST" action="{add_url}">
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
            <button type="submit" class="btn btn-success btn-sm w-100">Створити</button>
          </form>
        </div>
      </div>
      <div class="col-lg-8">
        <div class="card shadow-sm" style="border-radius:12px;overflow:hidden">
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
              <thead class="table-light">
                <tr><th>Код</th><th>Бонус</th><th>Залишилось</th><th>Використано</th><th>Створено</th><th></th></tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>"""
    return _layout("Промокоди", "promos_page", body)


@app.route("/promos/add", methods=["POST"])
@login_required
def add_promo():
    try:
        code      = request.form["code"].upper().strip()
        bonus     = int(request.form["bonus"])
        uses_left = int(request.form["uses_left"])
        if not code:
            raise ValueError("Код порожній")
        if bonus < 1:
            raise ValueError("Бонус >= 1")
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
    flash(
        f"✅ Промокод {code} видалено." if ok else f"❌ Промокод {code} не знайдено.",
        "success" if ok else "danger",
    )
    return redirect(url_for("promos_page"))


if __name__ == "__main__":
    print(f"Admin panel → http://0.0.0.0:{PORT}")
    if not ADMIN_PASSWORD:
        print("⚠️  ADMIN_PASSWORD не встановлено!")
    app.run(host="0.0.0.0", port=PORT, debug=False)
