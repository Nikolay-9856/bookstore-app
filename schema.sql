DROP TABLE IF EXISTS notifications;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS books;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin'))
);

CREATE TABLE books (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    author     TEXT    NOT NULL,
    category   TEXT    NOT NULL,
    year       INTEGER NOT NULL,
    annotation TEXT    DEFAULT '',
    price_buy  REAL    NOT NULL CHECK (price_buy  >= 0),
    price_rent REAL    NOT NULL CHECK (price_rent >= 0),   -- базовая цена аренды на 2 недели
    stock      INTEGER NOT NULL DEFAULT 1 CHECK (stock >= 0),
    status     TEXT    NOT NULL DEFAULT 'on_sale'
               CHECK (status IN ('on_sale', 'new', 'expected', 'withdrawn'))
);

CREATE TABLE orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    book_id          INTEGER NOT NULL REFERENCES books(id),
    kind             TEXT    NOT NULL CHECK (kind IN ('buy', 'rent')),
    days             INTEGER CHECK (days IN (14, 30, 90)),
    price            REAL    NOT NULL,
    started_at       TEXT    NOT NULL,
    ends_at          TEXT,
    returned         INTEGER NOT NULL DEFAULT 0,
    reminded_soon    INTEGER NOT NULL DEFAULT 0,
    reminded_overdue INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_books_cat    ON books(category);
CREATE INDEX idx_books_author ON books(author);
CREATE INDEX idx_orders_rent  ON orders(kind, returned, ends_at);
