"""Демо-покупатели и аренды для проверки напоминаний: python seed_demo.py"""
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from app import DT_FMT, app, get_db, init_db

with app.app_context():
    init_db()
    db = get_db()
    db.execute("INSERT INTO users(username,email,password_hash) VALUES ('ivan','ivan@example.com',?)",
               (generate_password_hash("secret1"),))
    uid = db.execute("SELECT id FROM users WHERE username='ivan'").fetchone()["id"]
    now = datetime.now()
    demo = [(1, "buy", None, 690, -20, None), (7, "rent", 14, 109, -12, 2), (4, "rent", 30, 178, -35, -5),
            (11, "rent", 90, 1120, -10, 80)]
    for book_id, kind, days, price, start_off, end_off in demo:
        st = (now + timedelta(days=start_off)).strftime(DT_FMT)
        en = (now + timedelta(days=end_off)).strftime(DT_FMT) if end_off is not None else None
        db.execute("INSERT INTO orders(user_id,book_id,kind,days,price,started_at,ends_at) VALUES (?,?,?,?,?,?,?)",
                   (uid, book_id, kind, days, price, st, en))
    db.commit()
    print("Готово: покупатель ivan / secret1, администратор admin / admin123")
