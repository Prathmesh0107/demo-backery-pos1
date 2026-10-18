"""Rush Counter POS (independent concept demo) - FastAPI + PostgreSQL.

Not connected to, authorised by, or integrated with any franchise billing software.
"""
import hmac
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/rushpos")
OWNER_PIN = os.environ.get("OWNER_PIN", "").strip()  # optional: protects Stock, Printer settings and Reset
STATIC = Path(__file__).resolve().parent.parent / "static"
CATS = ["Cakes", "Pastries", "Puffs & Savouries", "Bread & Bakery", "Cookies & Chocolates", "Candles & Birthday Accessories"]

# Sample data only (used when the products table is empty) - not official prices.
SEED = [
    ("Chocolate Truffle Cake 500g", CATS[0], 450, 8), ("Black Forest Cake 500g", CATS[0], 400, 6),
    ("Pineapple Cake 500g", CATS[0], 380, 5), ("Black Forest Pastry", CATS[1], 60, 24),
    ("Pineapple Pastry", CATS[1], 55, 20), ("Chocolate Pastry", CATS[1], 65, 18),
    ("Veg Puff", CATS[2], 25, 30), ("Paneer Puff", CATS[2], 35, 20), ("Veg Patty", CATS[2], 30, 3),
    ("Sandwich Bread", CATS[3], 45, 15), ("Pav (6 pcs)", CATS[3], 30, 12), ("Cupcake", CATS[3], 30, 25),
    ("Butter Cookies 200g", CATS[4], 90, 14), ("Choco Bar", CATS[4], 40, 0),
    ("Birthday Candles", CATS[5], 20, 40), ("Number Candle", CATS[5], 30, 15), ("Party Cap (pack)", CATS[5], 50, 10),
]

# Additive migrations only: existing products, sales and stock are never dropped or overwritten.
SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
  price INTEGER NOT NULL CHECK (price >= 0), stock INTEGER NOT NULL CHECK (stock >= 0),
  available BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS sales(
  id SERIAL PRIMARY KEY, bill_no TEXT UNIQUE NOT NULL, idem_key TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), total INTEGER NOT NULL,
  pay TEXT NOT NULL, received INTEGER, items JSONB NOT NULL);
CREATE SEQUENCE IF NOT EXISTS bill_seq START 1001;
ALTER TABLE products ADD COLUMN IF NOT EXISTS image TEXT;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS payments JSONB;
CREATE TABLE IF NOT EXISTS stock_log(
  id SERIAL PRIMARY KEY, product_id INTEGER, product_name TEXT NOT NULL,
  change INTEGER NOT NULL, before_qty INTEGER NOT NULL, after_qty INTEGER NOT NULL,
  reason TEXT NOT NULL, ref TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value JSONB NOT NULL);
"""

PRINTER_DEFAULTS = {
    "shop_name": "Sweet Counter", "address": "", "phone": "", "tax_id": "",
    "footer": "Thank you! Please visit again.", "paper_mm": 58, "font_px": 12,
    "show_demo_note": True, "auto_print": False,
}


def db():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def seed(c):
    with c.cursor() as cur:
        cur.executemany("INSERT INTO products(name,category,price,stock) VALUES(%s,%s,%s,%s)", SEED)


@asynccontextmanager
async def lifespan(_):
    with db() as c:
        c.execute(SCHEMA)
        if c.execute("SELECT count(*) AS n FROM products").fetchone()["n"] == 0:
            seed(c)
    yield


app = FastAPI(title="Rush Counter POS demo", lifespan=lifespan)


def owner(x_owner_pin: Optional[str] = Header(default=None)):
    """Optional owner PIN (env OWNER_PIN). Unset = open demo."""
    if OWNER_PIN and not hmac.compare_digest(x_owner_pin or "", OWNER_PIN):
        raise HTTPException(401, "Owner PIN required")


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cat: str = Field(min_length=1, max_length=60)
    price: int = Field(ge=0, le=1_000_000)
    stock: int = Field(ge=0, le=1_000_000)
    on: bool = True
    image: Optional[str] = Field(default=None, max_length=500)

    @field_validator("image")
    @classmethod
    def _img(cls, v):
        v = (v or "").strip()
        if not v:
            return None
        if not re.match(r"^(https?://|/)\S+$", v):
            raise ValueError("Image must be an http(s) link or a /path")
        return v


class Line(BaseModel):
    id: int
    q: int = Field(ge=1, le=999)


class SaleIn(BaseModel):
    key: Optional[str] = Field(default=None, max_length=80)
    items: List[Line] = Field(min_length=1)
    pay: Literal["Cash", "UPI", "Card", "Split"]  # recorded only; no real payment is processed
    recv: Optional[int] = Field(default=None, ge=0)
    cash: Optional[int] = Field(default=None, ge=0)  # Split only
    upi: Optional[int] = Field(default=None, ge=0)   # Split only


class StockIn(BaseModel):
    add: Optional[int] = Field(default=None, ge=-1_000_000, le=1_000_000)
    set_to: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    note: Optional[str] = Field(default=None, max_length=60)


class PrinterIn(BaseModel):
    shop_name: str = Field(min_length=1, max_length=40)
    address: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=30)
    tax_id: str = Field(default="", max_length=30)
    footer: str = Field(default="", max_length=120)
    paper_mm: Literal[58, 80] = 58
    font_px: int = Field(default=12, ge=10, le=18)
    show_demo_note: bool = True
    auto_print: bool = False


def prod_out(r):
    return {"id": r["id"], "name": r["name"], "cat": r["category"], "price": r["price"],
            "stock": r["stock"], "on": r["available"], "image": r.get("image")}


def sale_out(r):
    pays = r.get("payments") or {r["pay"]: r["total"]}
    return {"no": r["bill_no"], "t": int(r["created_at"].timestamp() * 1000), "items": r["items"],
            "total": r["total"], "pay": r["pay"], "recv": r["received"], "payments": pays}


def log_stock(c, pid, name, before, after, reason, ref=None):
    if before != after:
        c.execute("INSERT INTO stock_log(product_id,product_name,change,before_qty,after_qty,reason,ref) "
                  "VALUES(%s,%s,%s,%s,%s,%s,%s)", (pid, name, after - before, before, after, reason, ref))


def get_printer(c):
    row = c.execute("SELECT value FROM settings WHERE key='printer'").fetchone()
    return {**PRINTER_DEFAULTS, **(row["value"] if row else {})}


@app.get("/healthz")
def healthz():
    with db() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/state")
def state():
    with db() as c:
        prods = c.execute("SELECT * FROM products ORDER BY id").fetchall()
        sales = c.execute("SELECT * FROM (SELECT * FROM sales ORDER BY id DESC LIMIT 2000) t ORDER BY id").fetchall()
        settings = get_printer(c)
    cats = list(dict.fromkeys(p["category"] for p in prods))  # categories come from the data, in first-seen order
    return {"products": [prod_out(p) for p in prods], "sales": [sale_out(s) for s in sales],
            "categories": cats, "printer": settings, "pin_required": bool(OWNER_PIN)}


@app.post("/api/products", dependencies=[Depends(owner)])
def add_product(p: ProductIn):
    with db() as c:
        r = c.execute("INSERT INTO products(name,category,price,stock,available,image) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                      (p.name.strip(), p.cat.strip(), p.price, p.stock, p.on, p.image)).fetchone()
        log_stock(c, r["id"], r["name"], 0, r["stock"], "opening")
    return prod_out(r)


@app.put("/api/products/{pid}", dependencies=[Depends(owner)])
def edit_product(pid: int, p: ProductIn):
    with db() as c:
        old = c.execute("SELECT * FROM products WHERE id=%s FOR UPDATE", (pid,)).fetchone()
        if not old:
            raise HTTPException(404, "Product not found")
        r = c.execute("UPDATE products SET name=%s,category=%s,price=%s,stock=%s,available=%s,image=%s WHERE id=%s RETURNING *",
                      (p.name.strip(), p.cat.strip(), p.price, p.stock, p.on, p.image, pid)).fetchone()
        log_stock(c, pid, r["name"], old["stock"], r["stock"], "correction", "edit")
    return prod_out(r)


@app.post("/api/products/{pid}/stock", dependencies=[Depends(owner)])
def adjust_stock(pid: int, b: StockIn):
    """Quick replenishment (add) or correction (set_to). Logged; never goes below zero."""
    if (b.add is None) == (b.set_to is None):
        raise HTTPException(422, "Send either add or set_to")
    with db() as c:
        r = c.execute("SELECT * FROM products WHERE id=%s FOR UPDATE", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "Product not found")
        new = r["stock"] + b.add if b.add is not None else b.set_to
        if new < 0:
            raise HTTPException(409, f"Only {r['stock']} in stock - cannot remove {-b.add}")
        reason = "restock" if (b.add or 0) > 0 else "correction"
        r2 = c.execute("UPDATE products SET stock=%s WHERE id=%s RETURNING *", (new, pid)).fetchone()
        log_stock(c, pid, r["name"], r["stock"], new, reason, (b.note or "").strip() or None)
    return prod_out(r2)


@app.get("/api/stock-log", dependencies=[Depends(owner)])
def stock_log(limit: int = 40):
    limit = max(1, min(limit, 200))
    with db() as c:
        rows = c.execute("SELECT * FROM stock_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [{"id": r["id"], "name": r["product_name"], "change": r["change"], "after": r["after_qty"],
             "reason": r["reason"], "ref": r["ref"], "t": int(r["created_at"].timestamp() * 1000)} for r in rows]


@app.put("/api/printer", dependencies=[Depends(owner)])
def save_printer(b: PrinterIn):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('printer',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                  (Json(b.model_dump()),))
    return b.model_dump()


def _create_sale(b: SaleIn):
    qty = {}
    for i in b.items:
        qty[i.id] = qty.get(i.id, 0) + i.q
    with db() as c:
        if b.key:
            old = c.execute("SELECT * FROM sales WHERE idem_key=%s", (b.key,)).fetchone()
            if old:  # same request sent twice: return the original bill, never a duplicate (stock untouched)
                return sale_out(old)
        rows = {r["id"]: r for r in c.execute(
            "SELECT * FROM products WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (list(qty),))}
        items, total = [], 0
        for pid, q in qty.items():
            r = rows.get(pid)
            if not r:
                raise HTTPException(409, "A product on this bill no longer exists")
            if not r["available"]:
                raise HTTPException(409, f"{r['name']} is unavailable")
            if r["stock"] < q:
                raise HTTPException(409, f"Only {r['stock']} of {r['name']} in stock")
            items.append({"id": pid, "name": r["name"], "q": q, "price": r["price"], "before": r["stock"]})
            total += r["price"] * q
        if b.pay == "Cash" and b.recv is not None and b.recv < total:
            raise HTTPException(400, "Cash received is less than total")
        if b.pay == "Split":
            if b.cash is None or b.upi is None or b.cash + b.upi != total:
                raise HTTPException(400, "Split amounts must add up to the bill total")
            payments = {k: v for k, v in (("Cash", b.cash), ("UPI", b.upi)) if v > 0}
        else:
            payments = {b.pay: total}
        no = "DEMO-%d" % c.execute("SELECT nextval('bill_seq') AS n").fetchone()["n"]
        for it in items:  # stock leaves only here, once, in the same transaction as the bill
            c.execute("UPDATE products SET stock = stock - %s WHERE id=%s", (it["q"], it["id"]))
            log_stock(c, it["id"], it["name"], it["before"], it["before"] - it["q"], "sale", no)
            del it["before"]
        r = c.execute("INSERT INTO sales(bill_no,idem_key,total,pay,received,items,payments) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                      (no, b.key, total, b.pay, b.recv if b.pay == "Cash" else None, Json(items), Json(payments))).fetchone()
        return sale_out(r)


@app.post("/api/sales")
def create_sale(b: SaleIn):
    try:
        return _create_sale(b)
    except psycopg.errors.UniqueViolation:  # two identical requests raced; return the winner
        with db() as c:
            old = c.execute("SELECT * FROM sales WHERE idem_key=%s", (b.key,)).fetchone()
        if old:
            return sale_out(old)
        raise HTTPException(409, "Please try again")


@app.post("/api/restore-products", dependencies=[Depends(owner)])
def restore_products():
    """Additive only: re-adds any original sample product that is missing (matched by name). Existing products, stock and sales are untouched."""
    added = []
    with db() as c:
        have = {r["name"].strip().lower() for r in c.execute("SELECT name FROM products").fetchall()}
        for name, cat, price, stock in SEED:
            if name.lower() in have:
                continue
            r = c.execute("INSERT INTO products(name,category,price,stock) VALUES(%s,%s,%s,%s) RETURNING *", (name, cat, price, stock)).fetchone()
            log_stock(c, r["id"], r["name"], 0, r["stock"], "opening")
            added.append(name)
    return {"added": added}


@app.post("/api/reset", dependencies=[Depends(owner)])
def reset():
    """Demo-only: wipes products, sales and the stock log, then re-seeds samples. Printer settings are kept."""
    with db() as c:
        c.execute("TRUNCATE sales, products, stock_log RESTART IDENTITY")
        c.execute("ALTER SEQUENCE bill_seq RESTART WITH 1001")
        seed(c)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
