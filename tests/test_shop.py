import os
import tempfile
from datetime import datetime, timedelta

import pytest

from app import DT_FMT, app, get_db, init_db, send_rental_reminders


@pytest.fixture
def client(tmp_path):
    fd, path = tempfile.mkstemp()
    app.config.update(TESTING=True, DATABASE=path, OUTBOX=str(tmp_path / "outbox.log"))
    with app.app_context():
        init_db()
    with app.test_client() as c:
        yield c
    os.close(fd)
    os.unlink(path)


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p})


def make_user(c, name="reader"):
    c.post("/register", data={"username": name, "email": f"{name}@mail.ru", "password": "secret1"})
    login(c, name, "secret1")


def test_catalog_sort_and_filter(client):
    page = client.get("/?category=Фантастика&sort=year_asc").get_data(as_text=True)
    assert "Пикник на обочине" in page and "1984" not in page
    assert page.index("Солярис") < page.index("Трудно быть богом") < page.index("Пикник на обочине")
    page = client.get("/?author=Фёдор Достоевский").get_data(as_text=True)
    assert "Идиот" in page and "Солярис" not in page
    page = client.get("/?year_from=2000").get_data(as_text=True)
    assert "Чистый код" in page and "Идиот" not in page


def test_buy_and_rent(client):
    make_user(client)
    client.post("/book/1/order", data={"kind": "buy"})
    client.post("/book/2/order", data={"kind": "rent", "days": "30"})
    lib = client.get("/library").get_data(as_text=True)
    assert "Мастер и Маргарита" in lib and "1 месяц" in lib
    with app.app_context():
        row = get_db().execute("SELECT price FROM orders WHERE kind='rent'").fetchone()
        assert row["price"] == round(119 * 1.8)


def test_unavailable_book_cannot_be_ordered(client):
    make_user(client)
    r = client.post("/book/6/order", data={"kind": "buy"}, follow_redirects=True)  # «Солярис»: ожидается
    assert "недоступна" in r.get_data(as_text=True)


def test_wrong_rent_period(client):
    make_user(client)
    assert client.post("/book/1/order", data={"kind": "rent", "days": "7"}).status_code == 400


def test_admin_only(client):
    make_user(client)
    assert client.get("/admin").status_code == 403


def test_admin_manage_books(client):
    login(client, "admin", "admin123")
    client.post("/admin/book/new", data={"title": "Новая", "author": "Автор", "category": "Проза", "year": "2020",
                                          "price_buy": "300", "price_rent": "60", "stock": "2", "status": "new"})
    assert "Новая" in client.get("/admin").get_data(as_text=True)
    client.post("/admin/book/1/quick", data={"status": "withdrawn", "price_buy": "999", "price_rent": "150", "stock": "1"})
    with app.app_context():
        b = get_db().execute("SELECT * FROM books WHERE id=1").fetchone()
        assert b["price_buy"] == 999 and b["status"] == "withdrawn"
    assert "Мастер и Маргарита" not in client.get("/").get_data(as_text=True)


def test_admin_validation(client):
    login(client, "admin", "admin123")
    r = client.post("/admin/book/new", data={"title": "X", "author": "Y", "category": "Z", "year": "2020",
                                              "price_buy": "-5", "price_rent": "1", "stock": "1", "status": "new"})
    assert "неотрицательными" in r.get_data(as_text=True)


def test_reminders_sent_once(client):
    make_user(client)
    client.post("/book/1/order", data={"kind": "rent", "days": "14"})
    with app.app_context():
        db = get_db()
        soon = (datetime.now() + timedelta(days=1)).strftime(DT_FMT)
        db.execute("UPDATE orders SET ends_at=?", (soon,)); db.commit()
        assert send_rental_reminders(db) == 1
        assert send_rental_reminders(db) == 0          # повторно не шлём
        later = datetime.now() + timedelta(days=2)
        assert send_rental_reminders(db, at=later) == 1  # теперь уже просрочка
    notes = client.get("/notifications").get_data(as_text=True)
    assert "заканчивается" in notes and "истёк" in notes
