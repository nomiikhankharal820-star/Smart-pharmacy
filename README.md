# Smart Pharmacy Management System

Streamlit + SQLite project: medicines and injections, stock and expiry control, pharmacist approval,
online payment flow (demo mode), invoices, dashboard and audit log.

## Run in VS Code

1. Install Python 3.10+ and open this folder in VS Code (File > Open Folder).
2. Open the terminal (Ctrl + `) and run:

```
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac / Linux
pip install -r requirements.txt
streamlit run app.py
```

The browser opens at http://localhost:8501. The database file `smart_pharmacy.db` and sample medicines
are created automatically on first run. Delete the .db file to start fresh.

## Demo flow for your presentation

1. Medicines page: show stock, expiry, injections marked prescription-only, and the expired item.
2. New order: add Paracetamol only, place the order, then pay in Orders and payments (JazzCash demo).
3. New order: add Ceftriaxone Injection. It goes to the Approval queue.
4. Approval queue: type a pharmacist name, approve. Now it can be paid.
5. Orders and payments: pay, then download the invoice. Stock goes down automatically.
6. Dashboard and Audit log: show low stock, expiring items, and who approved what.

## Payments

`DEMO_MODE = True` simulates the provider's confirmation so the whole flow can be shown.
Real JazzCash / Easypaisa needs a merchant account, the provider's sandbox credentials, and a server
endpoint to receive and verify the provider's callback. The steps are written in the docstring of
`start_online_payment` in app.py. Keep credentials in environment variables, never in the code.

## Not included (good "future work" slide)

Login with roles, AI request classification, refill-reminder ML model, email/SMS sending, Docker.
