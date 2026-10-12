"""A conventional RPA script for scenario T1, written the way bots are usually written: a fixed recording of
selectors and steps, with the 'reasoning' (TDS, due date, account choice) baked in by a human beforehand.

It exists for two reasons: it proves the world and the checkers agree on what "done" means, and it is the
baseline the agent is compared against — it works on a calm day and breaks under chaos.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
from openpyxl import load_workbook
from playwright.sync_api import Page

from world import config


def run_t1(page: Page, approval_ref: str = "APV-TEST") -> dict:
    secrets = json.loads(config.SECRETS_FILE.read_text())
    portal = secrets["portal"]
    page.set_default_timeout(5000)
    page.goto(f"{portal['url']}/login")
    page.get_by_label("User ID").fill(portal["username"])
    page.get_by_label("Password").fill(portal["password"])
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda url: "/login" not in url)

    page.goto(f"{portal['url']}/bills/new")
    page.get_by_role("combobox", name="Vendor", exact=True).select_option("V-1001")
    for label, value in [("Vendor invoice no.", "GLX/2026/0917"), ("Invoice date", "30-09-2026"),
                         ("PO number", "PO-2026-0141"), ("Taxable value", "150000"), ("GST amount", "27000"),
                         ("Invoice total", "177000"), ("TDS amount", "3000"), ("Net payable", "174000"),
                         ("Due date", "30-10-2026")]:
        page.get_by_role("textbox", name=label, exact=True).fill(value)
    page.get_by_role("button", name="Save bill").click()
    page.wait_for_url("**/bills/BILL-*")
    bill_id = page.locator("h1").inner_text().split()[1]

    page.get_by_role("link", name="Record payment").click()
    page.get_by_role("combobox", name="Payment method").select_option("NEFT")
    account = page.get_by_role("combobox", name="Pay to account")
    label = next(t for t in account.locator("option").all_inner_texts() if "XXXX7812" in t)
    account.select_option(label=label)
    page.get_by_role("textbox", name="Payment date").fill("30-10-2026")
    page.get_by_role("textbox", name="Approval ref").fill(approval_ref)
    page.get_by_role("button", name="Schedule payment").click()
    page.wait_for_url("**/payments/PAY-*")
    payment_id = page.locator("h1").inner_text().split()[1]

    mail = secrets["mail"]
    httpx.post(f"{mail['url']}/api/messages", headers={"X-API-Key": mail["api_key"]}, json={
        "to": ["billing@globex.example"], "subject": "Remittance advice – GLX/2026/0917",
        "body": f"Invoice GLX/2026/0917 dated 30-09-2026\nInvoice total: INR 1,77,000.00\nTDS deducted: INR 3,000.00\n"
                f"Net amount: INR 1,74,000.00\nPayment date: 30-10-2026\nPayment ID: {payment_id} (NEFT)\n\n"
                "Nova (AI AP Associate)\nAccounts Payable, Northwind Robotics"}).raise_for_status()

    path = config.DRIVE / "AP" / "AP_Tracker.xlsx"
    wb = load_workbook(path)
    wb["Invoices"].append([date.today().strftime("%d-%m-%Y"), "Globex Corporation India Pvt Ltd", "GLX/2026/0917",
                           "30-09-2026", "PO-2026-0141", 177000, 3000, 174000, bill_id, payment_id, "30-10-2026",
                           "Payment Scheduled", "RPA baseline"])
    wb.save(path)
    return {"bill_id": bill_id, "payment_id": payment_id}

