# Rush Counter POS — tablet-first concept demo (FastAPI + PostgreSQL)

Independent concept demo. **Not an official Monginis product**; not connected to, authorised by, or integrated with any franchise billing software.
Prices, stock and sales are sample data. Cash/UPI only *record* the method chosen; no real payment is processed.

## Screens (built for a 10–11" Android tablet, landscape, ~1280×800 / 1024×768)
- **Counter** — category rail (from your data) · large product tiles · sticky bill. One tap adds a unit; CASH / UPI complete the sale in one tap; SPLIT takes Cash + UPI amounts and only completes when they equal the total. After a sale the bill clears and you are straight back on the tiles.
- **Stock** — big quantity per product, one-tap +5 / +10 / +20, "Other…" (add, remove or set exact count), edit/hide product, low/out filter, and a log of recent stock changes (sales, restocks, corrections).
- **Sales** — day summary (split bills counted by part), bills list with **Reprint** (never changes stock).
- **Printer** — shop name, address, phone, GST/tax no., footer, 58/80 mm paper, text size, auto-open print after sale, demo-note line, live preview, Test print. Saved in the database, shared by every device.

## Deploy on Render
1. Push this folder to GitHub → Render **New → Blueprint**. `render.yaml` creates the web service + Postgres and wires `DATABASE_URL`.
2. Existing data is kept: startup only adds missing tables/columns (`stock_log`, `settings`, `sales.payments`, `products.image`). Nothing is dropped or reset.
3. Optional: set `OWNER_PIN` in the Render dashboard to protect Stock, Printer settings and Reset. Leave unset for an open demo.
4. Device viewer for a laptop demo: add `?viewer=1`.

Free-tier notes (check Render's current terms): free web services sleep when idle (open it before the demo) and free Postgres may expire.

## Run locally
```
docker run -d --name rushdb -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rushpos -p 5432:5432 postgres:16
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rushpos
uvicorn app.main:app --reload
```

## How stock stays correct
- An open bill never touches stock. Completing a sale runs in **one transaction**: product rows are locked, stock is checked, stock is deducted, the bill and stock-log rows are saved.
- Each checkout carries an idempotency key, so double taps / retries return the same bill and never deduct twice. Reprints are client-side only.
- Stock can never go below 0 (database CHECK + server validation). Tiles refresh every 20 s so a second device stays in step.

## Printing
Uses the browser print dialog (works with any printer the tablet can print to, incl. a paired 58 mm Bluetooth thermal printer). Page size follows the paper-width setting. Browsers cannot print silently; direct ESC/POS over Bluetooth would be a separate add-on.

## API
`GET /api/state` · `POST /api/sales` · `POST/PUT /api/products` · `POST /api/products/{id}/stock` · `GET /api/stock-log` · `PUT /api/printer` · `POST /api/reset` · `GET /healthz`
