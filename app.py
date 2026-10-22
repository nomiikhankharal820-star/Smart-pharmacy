"""
Smart Pharmacy Management System  (Streamlit + SQLite)

Run:   streamlit run app.py

Features
- Medicines & injections with batch expiry, stock, low-stock and prescription-only flags
- Orders (counter or online) with stock + expiry checks
- Pharmacist approval queue: injections / prescription medicines cannot be paid or released until approved
- Payments: JazzCash, Easypaisa, Card, Cash on Delivery (DEMO MODE - see start_online_payment)
- Duplicate-payment protection (unique transaction reference)
- Dashboard, invoice download, audit log
"""
import sqlite3
import uuid
import datetime as dt

import pandas as pd
import streamlit as st

DB_FILE = "smart_pharmacy.db"
DEMO_MODE = True  # True = payments are simulated. Set False only after you wire a real gateway.


# ----------------------------------------------------------------------------
# Database helpers
# ----------------------------------------------------------------------------
def get_conn():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def query(sql, params=()):
    c = get_conn()
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def execute(sql, params=()):
    c = get_conn()
    try:
        cur = c.execute(sql, params)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def column_exists(c, table, col):
    return any(r["name"] == col for r in c.execute(f"PRAGMA table_info({table})").fetchall())


def migrate(c):
    """Upgrade an older smart_pharmacy.db (made before profit tracking) without losing data."""
    if not column_exists(c, "medicines", "cost_price"):
        c.execute("ALTER TABLE medicines ADD COLUMN cost_price REAL NOT NULL DEFAULT 0")
    if not column_exists(c, "order_items", "unit_cost"):
        c.execute("ALTER TABLE order_items ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0")
        c.execute("UPDATE order_items SET unit_cost = "
                  "(SELECT cost_price FROM medicines WHERE medicines.id = order_items.medicine_id)")
    c.commit()


def init_db():
    c = get_conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS medicines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        strength TEXT,
        form TEXT,                      -- Tablet, Capsule, Syrup, Injection ...
        requires_rx INTEGER DEFAULT 0,  -- 1 = prescription only (all injections)
        price REAL NOT NULL,
        cost_price REAL NOT NULL DEFAULT 0,   -- what the pharmacy paid per unit (needed for profit)
        stock INTEGER NOT NULL DEFAULT 0,
        batch_no TEXT,
        expiry TEXT NOT NULL,           -- YYYY-MM-DD
        reorder_level INTEGER DEFAULT 20
    );
    CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        address TEXT
    );
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL REFERENCES customers(id),
        source TEXT DEFAULT 'counter',          -- counter / online
        status TEXT NOT NULL,                   -- pending_approval, rejected, awaiting_payment, confirmed
        payment_status TEXT NOT NULL DEFAULT 'unpaid',  -- unpaid, paid, cod_pending
        payment_method TEXT,
        total REAL NOT NULL,
        approved_by TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        medicine_id INTEGER NOT NULL REFERENCES medicines(id),
        batch_no TEXT,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        unit_cost REAL NOT NULL DEFAULT 0     -- cost at the time of sale, so old profit never changes
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        method TEXT NOT NULL,
        txn_ref TEXT NOT NULL UNIQUE,           -- unique => the same payment can never be recorded twice
        amount REAL NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT,
        action TEXT,
        target TEXT,
        created_at TEXT
    );
    """)
    c.commit()
    migrate(c)
    if c.execute("SELECT COUNT(*) FROM medicines").fetchone()[0] == 0:
        today = dt.date.today()
        d = lambda n: (today + dt.timedelta(days=n)).isoformat()
        sample = [
            # name, strength, form, rx, sale price, cost price, stock, batch, expiry, reorder level
            ("Paracetamol", "500mg", "Tablet", 0, 3.0, 2.0, 400, "PCM-101", d(400), 100),
            ("ORS", "20.5g", "Sachet", 0, 35.0, 25.0, 120, "ORS-220", d(300), 30),
            ("Cetirizine", "10mg", "Tablet", 0, 6.0, 4.0, 15, "CTZ-310", d(250), 40),
            ("Amoxicillin", "500mg", "Capsule", 1, 25.0, 18.0, 200, "AMX-415", d(200), 50),
            ("Ceftriaxone", "1g", "Injection", 1, 280.0, 210.0, 60, "CEF-512", d(180), 20),
            ("Diclofenac", "75mg", "Injection", 1, 60.0, 42.0, 80, "DIC-618", d(25), 20),
            ("Vitamin B12", "1000mcg", "Injection", 1, 90.0, 65.0, 40, "B12-720", d(365), 15),
            ("Old Cough Syrup", "100ml", "Syrup", 0, 150.0, 100.0, 10, "CGH-001", d(-5), 10),  # expired on purpose
        ]
        c.executemany(
            "INSERT INTO medicines(name,strength,form,requires_rx,price,cost_price,stock,batch_no,expiry,reorder_level) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", sample)
        c.execute("INSERT INTO customers(name,phone,email,address) VALUES('Walk-in Customer','','','')")
        c.commit()
    c.close()


def log(user, action, target=""):
    execute("INSERT INTO audit_log(user,action,target,created_at) VALUES(?,?,?,?)",
            (user or "unknown", action, target, now()))


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_expired(expiry_str):
    return expiry_str < dt.date.today().isoformat()


# ----------------------------------------------------------------------------
# Business logic
# ----------------------------------------------------------------------------
def create_order(customer_id, cart, source="counter", note=""):
    """cart = [{'medicine_id': int, 'qty': int}, ...]. Returns (order_id, message)."""
    if not cart:
        return None, "Cart is empty."
    total, rows, needs_rx = 0.0, [], False
    for item in cart:
        m = query("SELECT * FROM medicines WHERE id=?", (item["medicine_id"],))[0]
        if is_expired(m["expiry"]):
            return None, f"{m['name']} is expired and cannot be sold."
        if item["qty"] > m["stock"]:
            return None, f"Not enough stock for {m['name']} (available {m['stock']})."
        needs_rx = needs_rx or bool(m["requires_rx"])
        total += m["price"] * item["qty"]
        rows.append((m["id"], m["batch_no"], item["qty"], m["price"], m["cost_price"]))
    status = "pending_approval" if needs_rx else "awaiting_payment"
    oid = execute(
        "INSERT INTO orders(customer_id,source,status,payment_status,total,note,created_at) VALUES(?,?,?,?,?,?,?)",
        (customer_id, source, status, "unpaid", total, note, now()))
    for r in rows:
        execute("INSERT INTO order_items(order_id,medicine_id,batch_no,quantity,unit_price,unit_cost) VALUES(?,?,?,?,?,?)",
                (oid, *r))
    log("system", "order_created", f"order #{oid}, status={status}")
    msg = ("Order needs pharmacist approval (injection / prescription medicine)."
           if needs_rx else "Order created. Waiting for payment.")
    return oid, msg


def decide_order(order_id, pharmacist, approve, note=""):
    if not pharmacist.strip():
        return False, "Enter the pharmacist name first."
    o = query("SELECT * FROM orders WHERE id=?", (order_id,))[0]
    if o["status"] != "pending_approval":
        return False, "This order is not waiting for approval."
    new_status = "awaiting_payment" if approve else "rejected"
    execute("UPDATE orders SET status=?, approved_by=?, note=? WHERE id=?",
            (new_status, pharmacist.strip(), note, order_id))
    log(pharmacist.strip(), "approved" if approve else "rejected", f"order #{order_id}: {note}")
    return True, "Order approved." if approve else "Order rejected."


def _deduct_stock(c, order_id):
    items = c.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    for it in items:
        m = c.execute("SELECT * FROM medicines WHERE id=?", (it["medicine_id"],)).fetchone()
        if is_expired(m["expiry"]):
            raise ValueError(f"{m['name']} expired before payment.")
        if m["stock"] < it["quantity"]:
            raise ValueError(f"Stock for {m['name']} is no longer enough.")
    for it in items:
        c.execute("UPDATE medicines SET stock = stock - ? WHERE id=?", (it["quantity"], it["medicine_id"]))


def pay_order(order_id, method, txn_ref):
    """Record a payment once. Safe to call twice with the same txn_ref."""
    c = get_conn()
    try:
        o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if o is None:
            return False, "Order not found."
        if o["status"] != "awaiting_payment":
            return False, "Order is not ready for payment (needs approval or already paid)."
        try:
            c.execute("INSERT INTO payments(order_id,method,txn_ref,amount,status,created_at) VALUES(?,?,?,?,?,?)",
                      (order_id, method, txn_ref, o["total"],
                       "cod_pending" if method == "Cash on Delivery" else "success", now()))
        except sqlite3.IntegrityError:
            return False, "This payment reference was already recorded. No double charge."
        _deduct_stock(c, order_id)
        pay_status = "cod_pending" if method == "Cash on Delivery" else "paid"
        c.execute("UPDATE orders SET status='confirmed', payment_status=?, payment_method=? WHERE id=?",
                  (pay_status, method, order_id))
        c.commit()
    except ValueError as e:
        c.rollback()
        return False, str(e)
    finally:
        c.close()
    log("system", "payment_recorded", f"order #{order_id} via {method} ref {txn_ref}")
    return True, "Payment recorded." if method != "Cash on Delivery" else "Order confirmed. Collect cash on delivery."


def mark_cod_received(order_id, user):
    execute("UPDATE orders SET payment_status='paid' WHERE id=? AND payment_status='cod_pending'", (order_id,))
    execute("UPDATE payments SET status='success' WHERE order_id=? AND method='Cash on Delivery'", (order_id,))
    log(user or "unknown", "cod_cash_received", f"order #{order_id}")


def start_online_payment(order, method):
    """
    DEMO MODE: returns a fake transaction reference so you can show the full flow.

    To go live with JazzCash / Easypaisa / a card gateway you must:
      1. Get a merchant account + sandbox credentials from the provider.
      2. Build the signed payment request using the provider's official documentation
         (merchant id, password, integrity salt are kept in environment variables, never in code).
      3. Redirect the customer to the provider's page.
      4. Receive the provider's callback on a SERVER endpoint (for example a small FastAPI route),
         verify its secure hash, and only then call pay_order(order_id, method, provider_txn_ref).
    """
    if DEMO_MODE:
        return f"DEMO-{uuid.uuid4().hex[:10].upper()}"
    raise NotImplementedError("Real payment gateway is not connected yet. See the docstring above.")


def get_report(start, end):
    """Sales, profit and stock left for confirmed orders between two dates (YYYY-MM-DD, inclusive)."""
    per_med = query(
        "SELECT m.id, m.name, m.strength, m.form, m.cost_price, m.stock AS stock_left, m.expiry, m.reorder_level, "
        "COALESCE(SUM(oi.quantity),0) AS units_sold, "
        "COALESCE(SUM(oi.quantity*oi.unit_price),0) AS sales, "
        "COALESCE(SUM(oi.quantity*(oi.unit_price-oi.unit_cost)),0) AS profit "
        "FROM medicines m LEFT JOIN ("
        "   SELECT x.* FROM order_items x JOIN orders o ON o.id=x.order_id "
        "   WHERE o.status='confirmed' AND date(o.created_at) BETWEEN ? AND ?"
        ") oi ON oi.medicine_id=m.id "
        "GROUP BY m.id ORDER BY sales DESC, m.name", (start, end))
    daily = query(
        "SELECT date(o.created_at) AS day, SUM(x.quantity*x.unit_price) AS sales, "
        "SUM(x.quantity*(x.unit_price-x.unit_cost)) AS profit "
        "FROM order_items x JOIN orders o ON o.id=x.order_id "
        "WHERE o.status='confirmed' AND date(o.created_at) BETWEEN ? AND ? GROUP BY day ORDER BY day", (start, end))
    pay = query(
        "SELECT payment_status, COUNT(*) AS n, SUM(total) AS amount FROM orders "
        "WHERE status='confirmed' AND date(created_at) BETWEEN ? AND ? GROUP BY payment_status", (start, end))
    sales = sum(r["sales"] for r in per_med)
    profit = sum(r["profit"] for r in per_med)
    totals = {
        "sales": sales,
        "profit": profit,
        "cost": sales - profit,
        "margin": (profit / sales * 100) if sales else 0.0,
        "units_sold": sum(r["units_sold"] for r in per_med),
        "orders": sum(r["n"] for r in pay),
        "received": sum(r["amount"] for r in pay if r["payment_status"] == "paid"),
        "cod_pending": sum(r["amount"] for r in pay if r["payment_status"] == "cod_pending"),
    }
    return {"per_med": per_med, "daily": daily, "totals": totals}


def get_stock_summary():
    """Current stock left (not limited by date)."""
    today = dt.date.today().isoformat()
    meds = query("SELECT * FROM medicines")
    return {
        "units_left": sum(m["stock"] for m in meds),
        "stock_value": sum(m["stock"] * m["cost_price"] for m in meds),
        "out_of_stock": sum(1 for m in meds if m["stock"] == 0),
        "low_stock": sum(1 for m in meds if 0 < m["stock"] <= m["reorder_level"]),
        "expired_units": sum(m["stock"] for m in meds if m["expiry"] < today),
        "missing_cost": sum(1 for m in meds if m["cost_price"] <= 0),
    }


def stock_status(m):
    if m["expiry"] < dt.date.today().isoformat():
        return "EXPIRED"
    if m["stock_left"] == 0:
        return "OUT OF STOCK"
    if m["stock_left"] <= m["reorder_level"]:
        return "LOW STOCK"
    return "OK"


def invoice_text(order_id):
    o = query("SELECT o.*, c.name AS cname, c.phone FROM orders o JOIN customers c ON c.id=o.customer_id "
              "WHERE o.id=?", (order_id,))[0]
    items = query("SELECT oi.*, m.name, m.strength, m.form FROM order_items oi "
                  "JOIN medicines m ON m.id=oi.medicine_id WHERE oi.order_id=?", (order_id,))
    lines = ["SMART PHARMACY - INVOICE", "=" * 44,
             f"Invoice #: {order_id:05d}", f"Date     : {o['created_at']}",
             f"Customer : {o['cname']} {o['phone'] or ''}", "-" * 44]
    for it in items:
        lines.append(f"{it['name']} {it['strength']} ({it['form']}) [{it['batch_no']}]")
        lines.append(f"   {it['quantity']} x {it['unit_price']:.2f} = {it['quantity'] * it['unit_price']:.2f}")
    lines += ["-" * 44, f"TOTAL (PKR): {o['total']:.2f}",
              f"Payment    : {o['payment_method'] or '-'} ({o['payment_status']})",
              f"Approved by: {o['approved_by'] or 'not required'}", "=" * 44,
              "Prescription medicines are dispensed only after pharmacist approval."]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# UI pages
# ----------------------------------------------------------------------------
def page_dashboard():
    st.header("Dashboard")
    today = dt.date.today().isoformat()
    rep = get_report(today, today)["totals"]
    pending = query("SELECT COUNT(*) AS n FROM orders WHERE status='pending_approval'")[0]["n"]
    low = query("SELECT * FROM medicines WHERE stock <= reorder_level ORDER BY stock")
    soon = (dt.date.today() + dt.timedelta(days=90)).isoformat()
    expiring = query("SELECT * FROM medicines WHERE expiry <= ? ORDER BY expiry", (soon,))
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Today's sales (PKR)", f"{rep['sales']:,.0f}")
    c2.metric("Today's profit (PKR)", f"{rep['profit']:,.0f}")
    c3.metric("Waiting for approval", pending)
    c4.metric("Low stock items", len(low))
    c5.metric("Expiring within 90 days", len(expiring))
    st.caption("Open the Reports page for full sales, profit and stock-left details.")
    st.subheader("Low stock")
    st.dataframe(pd.DataFrame(low)[["name", "strength", "form", "stock", "reorder_level"]]
                 if low else pd.DataFrame(), use_container_width=True)
    st.subheader("Expired or expiring soon")
    st.dataframe(pd.DataFrame(expiring)[["name", "strength", "form", "batch_no", "expiry", "stock"]]
                 if expiring else pd.DataFrame(), use_container_width=True)
    st.subheader("Top sellers")
    top = query("SELECT m.name, SUM(oi.quantity) AS units FROM order_items oi "
                "JOIN medicines m ON m.id=oi.medicine_id JOIN orders o ON o.id=oi.order_id "
                "WHERE o.status='confirmed' GROUP BY m.name ORDER BY units DESC LIMIT 5")
    if top:
        st.bar_chart(pd.DataFrame(top).set_index("name"))
    else:
        st.caption("No confirmed sales yet.")


def page_medicines():
    st.header("Medicines and injections")
    meds = query("SELECT * FROM medicines ORDER BY name")
    df = pd.DataFrame(meds)
    df["prescription_only"] = df["requires_rx"].map({1: "Yes", 0: "No"})
    df["status"] = df.apply(lambda r: "EXPIRED" if is_expired(r["expiry"])
                            else ("LOW STOCK" if r["stock"] <= r["reorder_level"] else "OK"), axis=1)
    df["profit_per_unit"] = df["price"] - df["cost_price"]
    st.dataframe(df[["id", "name", "strength", "form", "prescription_only", "cost_price", "price",
                     "profit_per_unit", "stock", "batch_no", "expiry", "status"]], use_container_width=True)

    with st.expander("Add a new medicine / injection"):
        with st.form("add_med"):
            name = st.text_input("Name")
            col = st.columns(3)
            strength = col[0].text_input("Strength (e.g. 500mg)")
            form = col[1].selectbox("Form", ["Tablet", "Capsule", "Syrup", "Injection", "Drip", "Sachet", "Other"])
            rx = col[2].checkbox("Prescription only", value=False)
            col2 = st.columns(5)
            cost = col2[0].number_input("Cost price (PKR)", min_value=0.0, step=1.0)
            price = col2[1].number_input("Sale price (PKR)", min_value=0.0, step=1.0)
            stock = col2[2].number_input("Stock", min_value=0, step=1)
            batch = col2[3].text_input("Batch no")
            expiry = col2[4].date_input("Expiry date", dt.date.today() + dt.timedelta(days=365))
            reorder = st.number_input("Reorder level", min_value=0, value=20)
            if st.form_submit_button("Save medicine"):
                if not name.strip() or price <= 0:
                    st.error("Name and a price above 0 are required.")
                else:
                    # Every injection is prescription-only in this system.
                    rx_flag = 1 if (rx or form == "Injection") else 0
                    execute("INSERT INTO medicines(name,strength,form,requires_rx,price,cost_price,stock,batch_no,expiry,reorder_level) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (name.strip(), strength, form, rx_flag, price, cost, int(stock), batch,
                             expiry.isoformat(), reorder))
                    log("admin", "medicine_added", name.strip())
                    st.success("Saved.")
                    st.rerun()

    with st.expander("Restock (new purchase)"):
        with st.form("restock"):
            names = {f"{m['name']} {m['strength']} ({m['form']}) - batch {m['batch_no']}": m["id"] for m in meds}
            pick = st.selectbox("Medicine", list(names))
            qty = st.number_input("Quantity received", min_value=1, step=1)
            if st.form_submit_button("Add stock"):
                execute("UPDATE medicines SET stock = stock + ? WHERE id=?", (int(qty), names[pick]))
                log("admin", "restock", f"{pick} +{int(qty)}")
                st.success("Stock updated.")
                st.rerun()

    page_update_prices(meds)


def page_update_prices(meds):
    with st.expander("Update sale price / cost price"):
        st.caption("Old sales keep the cost they were sold at, so past profit never changes.")
        with st.form("upd_price"):
            names = {f"{m['name']} {m['strength']} ({m['form']})": m for m in meds}
            pick = st.selectbox("Medicine", list(names))
            cur = names[pick]
            col = st.columns(2)
            new_cost = col[0].number_input("New cost price (PKR)", min_value=0.0, value=float(cur["cost_price"]), step=1.0)
            new_price = col[1].number_input("New sale price (PKR)", min_value=0.0, value=float(cur["price"]), step=1.0)
            if st.form_submit_button("Save prices"):
                if new_price <= 0:
                    st.error("Sale price must be above 0.")
                else:
                    execute("UPDATE medicines SET cost_price=?, price=? WHERE id=?", (new_cost, new_price, cur["id"]))
                    log("admin", "price_updated", f"{pick}: cost {new_cost}, price {new_price}")
                    st.success("Prices updated.")
                    st.rerun()


def page_new_order():
    st.header("New order")
    if "cart" not in st.session_state:
        st.session_state.cart = []

    customers = query("SELECT * FROM customers ORDER BY name")
    cmap = {f"{c['name']} {c['phone'] or ''}".strip(): c["id"] for c in customers}
    cust = st.selectbox("Customer", list(cmap))
    with st.expander("Add a new customer"):
        with st.form("add_cust"):
            n = st.text_input("Name")
            ph = st.text_input("Phone")
            em = st.text_input("Email")
            ad = st.text_input("Address")
            if st.form_submit_button("Save customer") and n.strip():
                execute("INSERT INTO customers(name,phone,email,address) VALUES(?,?,?,?)", (n.strip(), ph, em, ad))
                st.rerun()

    source = st.radio("Order type", ["counter", "online"], horizontal=True)
    available = [m for m in query("SELECT * FROM medicines WHERE stock>0 ORDER BY name") if not is_expired(m["expiry"])]
    mmap = {f"{m['name']} {m['strength']} ({m['form']}) - PKR {m['price']:.0f} - stock {m['stock']}"
            f"{' - RX' if m['requires_rx'] else ''}": m for m in available}
    c1, c2, c3 = st.columns([4, 1, 1])
    pick = c1.selectbox("Medicine", list(mmap))
    qty = c2.number_input("Qty", min_value=1, value=1, step=1)
    if c3.button("Add to cart"):
        st.session_state.cart.append({"medicine_id": mmap[pick]["id"], "qty": int(qty)})
        st.rerun()

    st.subheader("Cart")
    if st.session_state.cart:
        rows, total, rx = [], 0.0, False
        for it in st.session_state.cart:
            m = query("SELECT * FROM medicines WHERE id=?", (it["medicine_id"],))[0]
            rows.append({"medicine": f"{m['name']} {m['strength']} ({m['form']})", "qty": it["qty"],
                         "unit price": m["price"], "line total": m["price"] * it["qty"],
                         "prescription": "Yes" if m["requires_rx"] else "No"})
            total += m["price"] * it["qty"]
            rx = rx or bool(m["requires_rx"])
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        st.write(f"**Total: PKR {total:,.2f}**")
        if rx:
            st.warning("This cart has prescription medicine / injection. A pharmacist must approve it before payment.")
        note = st.text_input("Note (e.g. prescription reference)")
        b1, b2 = st.columns(2)
        if b1.button("Place order", type="primary"):
            oid, msg = create_order(cmap[cust], st.session_state.cart, source, note)
            if oid:
                st.session_state.cart = []
                st.success(f"Order #{oid}: {msg}")
            else:
                st.error(msg)
        if b2.button("Clear cart"):
            st.session_state.cart = []
            st.rerun()
    else:
        st.caption("Cart is empty.")


def page_approvals():
    st.header("Pharmacist approval queue")
    pharmacist = st.text_input("Pharmacist name (recorded in the audit log)")
    pending = query("SELECT o.*, c.name AS cname FROM orders o JOIN customers c ON c.id=o.customer_id "
                    "WHERE o.status='pending_approval' ORDER BY o.id")
    if not pending:
        st.info("Nothing is waiting for approval.")
    for o in pending:
        with st.container(border=True):
            st.write(f"**Order #{o['id']}** - {o['cname']} - PKR {o['total']:,.2f} - {o['created_at']}")
            items = query("SELECT m.name, m.strength, m.form, m.requires_rx, oi.quantity FROM order_items oi "
                          "JOIN medicines m ON m.id=oi.medicine_id WHERE oi.order_id=?", (o["id"],))
            st.dataframe(pd.DataFrame(items), use_container_width=True)
            if o["note"]:
                st.caption(f"Customer note: {o['note']}")
            reason = st.text_input("Pharmacist note", key=f"n{o['id']}")
            a, r = st.columns(2)
            if a.button("Approve", key=f"a{o['id']}"):
                ok, msg = decide_order(o["id"], pharmacist, True, reason)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
            if r.button("Reject", key=f"r{o['id']}"):
                ok, msg = decide_order(o["id"], pharmacist, False, reason)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()


def page_orders():
    st.header("Orders and payments")
    orders = query("SELECT o.id, c.name AS customer, o.source, o.status, o.payment_status, o.payment_method, "
                   "o.total, o.created_at FROM orders o JOIN customers c ON c.id=o.customer_id ORDER BY o.id DESC")
    if not orders:
        st.info("No orders yet.")
        return
    st.dataframe(pd.DataFrame(orders), use_container_width=True)

    st.subheader("Take payment")
    waiting = [o for o in orders if o["status"] == "awaiting_payment"]
    if waiting:
        pick = st.selectbox("Order", [f"#{o['id']} - {o['customer']} - PKR {o['total']:,.2f}" for o in waiting])
        oid = int(pick.split()[0].replace("#", ""))
        method = st.radio("Payment method", ["JazzCash", "Easypaisa", "Card", "Cash on Delivery"], horizontal=True)
        if DEMO_MODE and method != "Cash on Delivery":
            st.caption("DEMO MODE: the button below simulates a successful provider confirmation.")
        label = "Confirm order (COD)" if method == "Cash on Delivery" else f"Pay with {method}"
        if st.button(label, type="primary"):
            order = [o for o in orders if o["id"] == oid][0]
            ref = f"COD-{oid}" if method == "Cash on Delivery" else start_online_payment(order, method)
            ok, msg = pay_order(oid, method, ref)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()
    else:
        st.caption("No orders are waiting for payment.")

    cod = [o for o in orders if o["payment_status"] == "cod_pending"]
    if cod:
        st.subheader("Cash on delivery - waiting for cash")
        who = st.text_input("Received by")
        for o in cod:
            if st.button(f"Cash received for order #{o['id']}", key=f"cod{o['id']}"):
                mark_cod_received(o["id"], who)
                st.rerun()

    st.subheader("Invoice")
    done = [o["id"] for o in orders if o["status"] == "confirmed"]
    if done:
        inv = st.selectbox("Confirmed order", done)
        text = invoice_text(inv)
        st.code(text)
        st.download_button("Download invoice (.txt)", text, file_name=f"invoice_{inv:05d}.txt")


def page_reports():
    st.header("Sales, profit and stock report")
    today = dt.date.today()
    period = st.radio("Period", ["Today", "Last 7 days", "Last 30 days", "All time", "Custom dates"], horizontal=True)
    if period == "Today":
        start, end = today, today
    elif period == "Last 7 days":
        start, end = today - dt.timedelta(days=6), today
    elif period == "Last 30 days":
        start, end = today - dt.timedelta(days=29), today
    elif period == "All time":
        start, end = dt.date(2000, 1, 1), today
    else:
        a, b = st.columns(2)
        start = a.date_input("From", today - dt.timedelta(days=30))
        end = b.date_input("To", today)
        if start > end:
            st.error("The 'From' date must be before the 'To' date.")
            return

    rep = get_report(start.isoformat(), end.isoformat())
    t = rep["totals"]
    stock = get_stock_summary()

    st.subheader("Sales and profit")
    c = st.columns(4)
    c[0].metric("Total sales (PKR)", f"{t['sales']:,.0f}")
    c[1].metric("Total profit (PKR)", f"{t['profit']:,.0f}")
    c[2].metric("Profit margin", f"{t['margin']:.1f}%")
    c[3].metric("Items sold (units)", f"{t['units_sold']:,}")
    c = st.columns(4)
    c[0].metric("Confirmed orders", t["orders"])
    c[1].metric("Cost of goods sold (PKR)", f"{t['cost']:,.0f}")
    c[2].metric("Money received (PKR)", f"{t['received']:,.0f}")
    c[3].metric("COD cash still to collect (PKR)", f"{t['cod_pending']:,.0f}")

    st.subheader("Items left in stock (right now)")
    c = st.columns(4)
    c[0].metric("Total units left", f"{stock['units_left']:,}")
    c[1].metric("Stock value at cost (PKR)", f"{stock['stock_value']:,.0f}")
    c[2].metric("Low / out of stock", f"{stock['low_stock']} / {stock['out_of_stock']}")
    c[3].metric("Expired units", f"{stock['expired_units']:,}")
    if stock["missing_cost"]:
        st.warning(f"{stock['missing_cost']} medicine(s) have cost price 0, so their profit looks too high. "
                   "Set the real cost in Medicines > Update sale price / cost price.")

    st.subheader("Medicine-wise report")
    if rep["per_med"]:
        table = pd.DataFrame([{
            "Medicine": f"{m['name']} {m['strength']} ({m['form']})",
            "Units sold": m["units_sold"],
            "Sales (PKR)": round(m["sales"], 2),
            "Profit (PKR)": round(m["profit"], 2),
            "Stock left": m["stock_left"],
            "Status": stock_status(m),
        } for m in rep["per_med"]])
        st.dataframe(table, use_container_width=True)
        st.download_button("Download this report (.csv)", table.to_csv(index=False),
                           file_name=f"pharmacy_report_{start}_{end}.csv")
    if rep["daily"]:
        st.subheader("Day-by-day sales and profit")
        st.bar_chart(pd.DataFrame(rep["daily"]).set_index("day")[["sales", "profit"]])
    elif t["orders"] == 0:
        st.info("No confirmed sales in this period yet.")


def page_audit():
    st.header("Audit log")
    rows = query("SELECT created_at, user, action, target FROM audit_log ORDER BY id DESC LIMIT 200")
    st.dataframe(pd.DataFrame(rows), use_container_width=True)


def main():
    st.set_page_config(page_title="Smart Pharmacy", layout="wide")
    init_db()
    st.sidebar.title("Smart Pharmacy")
    if DEMO_MODE:
        st.sidebar.info("Payments are in DEMO MODE.")
    page = st.sidebar.radio("Menu", ["Dashboard", "Reports", "Medicines", "New order", "Approval queue",
                                     "Orders and payments", "Audit log"])
    {"Dashboard": page_dashboard, "Reports": page_reports, "Medicines": page_medicines,
     "New order": page_new_order, "Approval queue": page_approvals,
     "Orders and payments": page_orders, "Audit log": page_audit}[page]()


if __name__ == "__main__":
    main()
