"""Книжный магазин «Полка»: каталог, покупка и аренда книг,
кабинет администратора, автоматические напоминания об окончании аренды.

Запуск:  python app.py      (http://127.0.0.1:5001)
Администратор по умолчанию: admin / admin123 (сменить после первого входа!)
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SHOP_SECRET", "dev-only-change-me"),
    SESSION_COOKIE_NAME="shop_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    DATABASE=os.environ.get("SHOP_DB", os.path.join(BASE_DIR, "shop.sqlite3")),
    REMIND_DAYS_BEFORE=int(os.environ.get("REMIND_DAYS_BEFORE", 3)),
    REMINDER_INTERVAL=int(os.environ.get("REMINDER_INTERVAL", 3600)),
    OUTBOX=os.path.join(BASE_DIR, "outbox.log"),
)

# Срок аренды -> (название, коэффициент к базовой цене двухнедельной аренды)
RENT_PLANS = {
    14: ("2 недели", 1.0),
    30: ("1 месяц", 1.8),
    90: ("3 месяца", 4.5),
}
STATUSES = {
    "on_sale": "В продаже",
    "new": "Новинка",
    "expected": "Ожидается",
    "withdrawn": "Снята с продажи",
}
SORTS = {
    "category": "category COLLATE NOCASE, title",
    "author": "author COLLATE NOCASE, year",
    "year_desc": "year DESC, title",
    "year_asc": "year ASC, title",
    "price": "price_buy ASC",
    "title": "title COLLATE NOCASE",
}
DT_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- БД
def connect(path=None):
    conn = sqlite3.connect(path or app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(seed=True):
    db = get_db()
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        db.executescript(f.read())
    db.execute(
        "INSERT INTO users(username, password_hash, role) VALUES (?,?, 'admin')",
        ("admin", generate_password_hash("admin123")),
    )
    if seed:
        with open(os.path.join(BASE_DIR, "seed_books.sql"), encoding="utf-8") as f:
            db.executescript(f.read())
    db.commit()


def now():
    return datetime.now()


def rent_price(book, days):
    return round(book["price_rent"] * RENT_PLANS[days][1])


def is_available(book):
    return book["status"] in ("on_sale", "new") and book["stock"] > 0


# ---------------------------------------------------------- доступ
@app.before_request
def load_user():
    g.user = None
    g.unread = 0
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user:
            g.unread = get_db().execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (uid,)
            ).fetchone()[0]


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None or g.user["role"] != "admin":
            abort(403)
        return view(*a, **kw)
    return wrapped


@app.context_processor
def helpers():
    return dict(STATUSES=STATUSES, RENT_PLANS=RENT_PLANS, rent_price=rent_price,
                is_available=is_available)


# ------------------------------------------------------ авторизация
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        pwd = request.form.get("password", "")
        if len(name) < 3 or len(pwd) < 6 or "@" not in email:
            flash("Проверьте данные: логин от 3 символов, пароль от 6, корректный e-mail")
        else:
            try:
                get_db().execute(
                    "INSERT INTO users(username, email, password_hash) VALUES (?,?,?)",
                    (name, email, generate_password_hash(pwd)),
                )
                get_db().commit()
                flash("Аккаунт создан, войдите")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("Такой логин уже занят")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = get_db().execute("SELECT * FROM users WHERE username=?",
                             (request.form.get("username", "").strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            if u["role"] == "admin":
                return redirect(url_for("admin_books"))
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("catalog"))
        flash("Неверный логин или пароль")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("catalog"))


# ----------------------------------------------- интерфейс покупателя
@app.route("/")
def catalog():
    db = get_db()
    where, params = ["status <> 'withdrawn'"], []
    category = request.args.get("category", "")
    author = request.args.get("author", "")
    year_from = request.args.get("year_from", type=int)
    year_to = request.args.get("year_to", type=int)
    q = request.args.get("q", "").strip()
    if category:
        where.append("category = ?"); params.append(category)
    if author:
        where.append("author = ?"); params.append(author)
    if year_from is not None:
        where.append("year >= ?"); params.append(year_from)
    if year_to is not None:
        where.append("year <= ?"); params.append(year_to)
    if q:
        where.append("(title LIKE ? OR author LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    sort = request.args.get("sort", "category")
    books = db.execute(
        f"SELECT * FROM books WHERE {' AND '.join(where)} ORDER BY {SORTS.get(sort, SORTS['category'])}",
        params,
    ).fetchall()
    categories = [r[0] for r in db.execute("SELECT DISTINCT category FROM books ORDER BY 1")]
    authors = [r[0] for r in db.execute("SELECT DISTINCT author FROM books ORDER BY 1")]
    return render_template("catalog.html", books=books, categories=categories, authors=authors,
                           args=request.args, sort=sort)


@app.route("/book/<int:book_id>")
def book(book_id):
    b = get_db().execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if b is None:
        abort(404)
    owned = None
    if g.user:
        owned = get_db().execute(
            """SELECT * FROM orders WHERE user_id=? AND book_id=?
               AND (kind='buy' OR (returned=0 AND ends_at > ?))
               ORDER BY id DESC LIMIT 1""",
            (g.user["id"], book_id, now().strftime(DT_FMT)),
        ).fetchone()
    return render_template("book.html", b=b, owned=owned)


@app.route("/book/<int:book_id>/order", methods=["POST"])
@login_required
def order(book_id):
    db = get_db()
    b = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if b is None:
        abort(404)
    if not is_available(b):
        flash("Книга сейчас недоступна для заказа")
        return redirect(url_for("book", book_id=book_id))
    kind = request.form.get("kind")
    started = now()
    if kind == "buy":
        price, ends = b["price_buy"], None
    elif kind == "rent":
        days = request.form.get("days", type=int)
        if days not in RENT_PLANS:
            abort(400)
        price, ends = rent_price(b, days), (started + timedelta(days=days)).strftime(DT_FMT)
    else:
        abort(400)
    # Атомарное списание экземпляра: при одновременных заказах остаток не уйдёт в минус
    if db.execute("UPDATE books SET stock = stock - 1 WHERE id=? AND stock > 0", (book_id,)).rowcount == 0:
        flash("Экземпляры закончились")
        return redirect(url_for("book", book_id=book_id))
    db.execute(
        """INSERT INTO orders(user_id, book_id, kind, days, price, started_at, ends_at)
           VALUES (?,?,?,?,?,?,?)""",
        (g.user["id"], book_id, kind, request.form.get("days", type=int) if kind == "rent" else None,
         price, started.strftime(DT_FMT), ends),
    )
    db.commit()
    flash("Покупка оформлена" if kind == "buy" else f"Аренда оформлена до {ends[:10]}")
    return redirect(url_for("my_library"))


@app.route("/library")
@login_required
def my_library():
    rows = get_db().execute(
        """SELECT o.*, b.title, b.author FROM orders o JOIN books b ON b.id=o.book_id
           WHERE o.user_id=? ORDER BY o.id DESC""",
        (g.user["id"],),
    ).fetchall()
    return render_template("library.html", orders=rows, now=now().strftime(DT_FMT))


@app.route("/order/<int:order_id>/return", methods=["POST"])
@login_required
def return_book(order_id):
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, g.user["id"])).fetchone()
    if o is None or o["kind"] != "rent" or o["returned"]:
        abort(400)
    db.execute("UPDATE orders SET returned=1 WHERE id=?", (order_id,))
    db.execute("UPDATE books SET stock = stock + 1 WHERE id=?", (o["book_id"],))
    db.commit()
    flash("Книга возвращена")
    return redirect(url_for("my_library"))


@app.route("/notifications")
@login_required
def notifications():
    db = get_db()
    rows = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC",
                      (g.user["id"],)).fetchall()
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],))
    db.commit()
    return render_template("notifications.html", rows=rows)


# ------------------------------------------------ интерфейс администратора
def book_form_values():
    f = request.form
    data = {
        "title": f.get("title", "").strip(),
        "author": f.get("author", "").strip(),
        "category": f.get("category", "").strip(),
        "year": f.get("year", type=int),
        "annotation": f.get("annotation", "").strip(),
        "price_buy": f.get("price_buy", type=float),
        "price_rent": f.get("price_rent", type=float),
        "stock": f.get("stock", type=int),
        "status": f.get("status", "on_sale"),
    }
    errors = []
    if not data["title"] or not data["author"] or not data["category"]:
        errors.append("Название, автор и категория обязательны")
    if data["year"] is None or not (-3000 < data["year"] <= now().year):
        errors.append("Некорректный год написания")
    if data["price_buy"] is None or data["price_buy"] < 0 or data["price_rent"] is None or data["price_rent"] < 0:
        errors.append("Цены должны быть неотрицательными числами")
    if data["stock"] is None or data["stock"] < 0:
        errors.append("Количество экземпляров не может быть отрицательным")
    if data["status"] not in STATUSES:
        errors.append("Неизвестный статус")
    return data, errors


@app.route("/admin")
@admin_required
def admin_books():
    books = get_db().execute("SELECT * FROM books ORDER BY title").fetchall()
    return render_template("admin_books.html", books=books)


@app.route("/admin/book/new", methods=["GET", "POST"])
@app.route("/admin/book/<int:book_id>", methods=["GET", "POST"])
@admin_required
def admin_edit(book_id=None):
    db = get_db()
    b = None
    if book_id:
        b = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if b is None:
            abort(404)
    if request.method == "POST":
        data, errors = book_form_values()
        if errors:
            for e in errors:
                flash(e)
            return render_template("admin_edit.html", b=data | {"id": book_id})
        cols = list(data)
        if b:
            db.execute(f"UPDATE books SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                       [data[c] for c in cols] + [book_id])
        else:
            db.execute(f"INSERT INTO books({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [data[c] for c in cols])
        db.commit()
        flash("Сохранено")
        return redirect(url_for("admin_books"))
    return render_template("admin_edit.html", b=b)


@app.route("/admin/book/<int:book_id>/quick", methods=["POST"])
@admin_required
def admin_quick(book_id):
    """Быстрое изменение цены, статуса и остатка прямо из таблицы."""
    status = request.form.get("status")
    price_buy = request.form.get("price_buy", type=float)
    price_rent = request.form.get("price_rent", type=float)
    stock = request.form.get("stock", type=int)
    if status not in STATUSES or None in (price_buy, price_rent, stock) or min(price_buy, price_rent, stock) < 0:
        flash("Некорректные значения")
    else:
        get_db().execute("UPDATE books SET status=?, price_buy=?, price_rent=?, stock=? WHERE id=?",
                         (status, price_buy, price_rent, stock, book_id))
        get_db().commit()
        flash("Изменения применены")
    return redirect(url_for("admin_books"))


@app.route("/admin/book/<int:book_id>/delete", methods=["POST"])
@admin_required
def admin_delete(book_id):
    db = get_db()
    used = db.execute("SELECT COUNT(*) FROM orders WHERE book_id=?", (book_id,)).fetchone()[0]
    if used:
        db.execute("UPDATE books SET status='withdrawn' WHERE id=?", (book_id,))
        flash("По книге есть заказы — она снята с продажи вместо удаления")
    else:
        db.execute("DELETE FROM books WHERE id=?", (book_id,))
        flash("Книга удалена")
    db.commit()
    return redirect(url_for("admin_books"))


@app.route("/admin/rentals")
@admin_required
def admin_rentals():
    rows = get_db().execute(
        """SELECT o.*, b.title, u.username, u.email FROM orders o
           JOIN books b ON b.id=o.book_id JOIN users u ON u.id=o.user_id
           WHERE o.kind='rent' ORDER BY o.returned, o.ends_at"""
    ).fetchall()
    log = get_db().execute(
        """SELECT n.*, u.username FROM notifications n JOIN users u ON u.id=n.user_id
           ORDER BY n.id DESC LIMIT 20"""
    ).fetchall()
    return render_template("admin_rentals.html", rows=rows, log=log, now=now().strftime(DT_FMT))


@app.route("/admin/reminders/run", methods=["POST"])
@admin_required
def admin_run_reminders():
    sent = send_rental_reminders(get_db())
    flash(f"Проверка выполнена, отправлено напоминаний: {sent}")
    return redirect(url_for("admin_rentals"))


# ------------------------------------------- автоматические напоминания
def send_rental_reminders(db, at=None):
    """Находит аренды, которые скоро закончатся или уже просрочены,
    и создаёт уведомления. Каждое напоминание отправляется один раз
    (флаги reminded_soon / reminded_overdue)."""
    at = at or now()
    soon_border = (at + timedelta(days=app.config["REMIND_DAYS_BEFORE"])).strftime(DT_FMT)
    stamp = at.strftime(DT_FMT)
    sent = 0
    soon = db.execute(
        """SELECT o.id, o.user_id, o.ends_at, b.title, u.email FROM orders o
           JOIN books b ON b.id=o.book_id JOIN users u ON u.id=o.user_id
           WHERE o.kind='rent' AND o.returned=0 AND o.reminded_soon=0
             AND o.ends_at > ? AND o.ends_at <= ?""",
        (stamp, soon_border),
    ).fetchall()
    for r in soon:
        text = f"Аренда книги «{r['title']}» заканчивается {r['ends_at'][:16]}. Продлите или верните книгу."
        _notify(db, r, text, stamp)
        db.execute("UPDATE orders SET reminded_soon=1 WHERE id=?", (r["id"],))
        sent += 1
    overdue = db.execute(
        """SELECT o.id, o.user_id, o.ends_at, b.title, u.email FROM orders o
           JOIN books b ON b.id=o.book_id JOIN users u ON u.id=o.user_id
           WHERE o.kind='rent' AND o.returned=0 AND o.reminded_overdue=0 AND o.ends_at <= ?""",
        (stamp,),
    ).fetchall()
    for r in overdue:
        text = f"Срок аренды книги «{r['title']}» истёк {r['ends_at'][:16]}. Доступ к книге закрыт."
        _notify(db, r, text, stamp)
        db.execute("UPDATE orders SET reminded_overdue=1, reminded_soon=1 WHERE id=?", (r["id"],))
        sent += 1
    db.commit()
    return sent


def _notify(db, row, text, stamp):
    db.execute("INSERT INTO notifications(user_id, text, created_at) VALUES (?,?,?)",
               (row["user_id"], text, stamp))
    # Вместо реальной почты письмо пишется в журнал outbox.log.
    # Для боевого режима здесь подключается SMTP (smtplib) или сервис рассылок.
    with open(app.config["OUTBOX"], "a", encoding="utf-8") as f:
        f.write(f"{stamp}\tTO:{row['email'] or '-'}\t{text}\n")


def reminder_worker():
    while True:
        try:
            db = connect()
            send_rental_reminders(db)
            db.close()
        except sqlite3.Error as exc:  # БД может быть ещё не создана
            app.logger.warning("Напоминания не отправлены: %s", exc)
        time.sleep(app.config["REMINDER_INTERVAL"])


def start_scheduler():
    t = threading.Thread(target=reminder_worker, daemon=True, name="rental-reminders")
    t.start()
    return t


@app.errorhandler(403)
@app.errorhandler(404)
def http_error(err):
    return render_template("error.html", err=err), err.code


if __name__ == "__main__":
    with app.app_context():
        if not os.path.exists(app.config["DATABASE"]):
            init_db()
    start_scheduler()
    app.run(port=5001, debug=False)
