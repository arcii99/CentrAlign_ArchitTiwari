"""Is a local model agent-ready on this machine? Measures speed (prefill / decode tokens per second) and whether
the model makes a real AP decision correctly through tool calls: 2% TDS on the *taxable* value, net payable,
due date from the vendor's payment terms — none of which the request mentions.

    uv run python -m scripts.model_probe qwen3.5:4b --trials 3 [--think]
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta

import httpx
from pypdf import PdfReader

from world import config

OLLAMA = "http://127.0.0.1:11434"

TOOLS = [
    {"type": "function", "function": {
        "name": "calc", "description": "Evaluate an arithmetic expression exactly, e.g. '150000 * 0.02'. "
                                       "Use this for every calculation.",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "add_days", "description": "Add a number of days to a date written DD-MM-YYYY.",
        "parameters": {"type": "object", "properties": {"date": {"type": "string"}, "days": {"type": "integer"}},
                       "required": ["date", "days"]}}},
    {"type": "function", "function": {
        "name": "create_bill", "description": "Enter the bill in the Finance Portal. Amounts are plain numbers; "
                                              "dates are DD-MM-YYYY.",
        "parameters": {"type": "object", "properties": {
            k: {"type": "number" if k in ("taxable_value", "gst_amount", "total", "tds_amount", "net_payable")
                else "string"} for k in ("vendor_id", "vendor_invoice_no", "invoice_date", "po_id", "taxable_value",
                                         "gst_amount", "total", "tds_amount", "net_payable", "due_date")},
            "required": ["vendor_id", "vendor_invoice_no", "invoice_date", "po_id", "taxable_value", "gst_amount",
                         "total", "tds_amount", "net_payable", "due_date"]}}},
]

EXPECTED = {"vendor_id": "V-1001", "vendor_invoice_no": "GLX/2026/0917", "invoice_date": "30-09-2026",
            "po_id": "PO-2026-0141", "taxable_value": 150000, "gst_amount": 27000, "total": 177000,
            "tds_amount": 3000, "net_payable": 174000, "due_date": "30-10-2026"}


def _context() -> str:
    handbook = (config.DRIVE / "Handbook" / "01_AP_Invoice_Processing.md").read_text()
    steps = handbook[handbook.index("## Step 3"): handbook.index("## Step 6")]
    pdf = next(config.WORLD_DIR.glob("mail_files/*/GLX-2026-0917.pdf"))
    invoice = PdfReader(pdf).pages[0].extract_text()
    vendor = ("Vendor V-1001 Globex Corporation India Pvt Ltd | GSTIN 29AACCG4567K1Z2 | Category: Services | "
              "Payment terms: 30 days | Status: Active")
    po = ("PO-2026-0141 (V-1001, Open): line 1 'ERP integration consulting – September 2026', qty 1 lot @ 150000.00, "
          "GST 18%. Receipts: SC-2026-0121 dated 30-09-2026, Service confirmation – September 2026.")
    return (f"## Handbook excerpt\n{steps}\n## Invoice (text extracted from GLX-2026-0917.pdf)\n{invoice}\n\n"
            f"## Finance Portal: vendor master\n{vendor}\n\n## Finance Portal: purchase order\n{po}\n")


def _run_tool(name: str, args: dict) -> str:
    if name == "calc":
        expr = str(args.get("expression", ""))
        if not set(expr) <= set("0123456789.+-*/() "):
            return "error: only numbers and + - * / ( ) are allowed"
        return str(round(eval(expr), 2))  # noqa: S307 — character-whitelisted above
    if name == "add_days":
        d, m, y = map(int, str(args["date"]).split("-"))
        return (date(y, m, d) + timedelta(days=int(args["days"]))).strftime("%d-%m-%Y")
    return "ok"


def trial(model: str, think: bool) -> dict:
    messages = [
        {"role": "system", "content": "You are Nova, the AI accounts-payable associate at Northwind Robotics. Today is "
                                      "05-10-2026. Follow the company handbook exactly. Use the calc and add_days tools "
                                      "for all arithmetic and date math; never compute in your head."},
        {"role": "user", "content": "Request from Priya (CFO): \"Please pay the Globex invoice that came in this week.\"\n\n"
                                    f"{_context()}\nThe three-way match has passed. Your next step is to enter the bill: "
                                    "work out every field per the handbook, then call create_bill."},
    ]
    stats = {"calls": 0, "prompt_tokens": 0, "prompt_s": 0.0, "gen_tokens": 0, "gen_s": 0.0}
    started = time.time()
    for _ in range(10):
        r = httpx.post(f"{OLLAMA}/api/chat", timeout=900, json={
            "model": model, "messages": messages, "tools": TOOLS, "stream": False, "think": think,
            "options": {"num_ctx": 16384, "temperature": 0.2}})
        r.raise_for_status()
        out = r.json()
        stats["calls"] += 1
        stats["prompt_tokens"] += out.get("prompt_eval_count", 0)
        stats["prompt_s"] += out.get("prompt_eval_duration", 0) / 1e9
        stats["gen_tokens"] += out.get("eval_count", 0)
        stats["gen_s"] += out.get("eval_duration", 0) / 1e9
        msg = out["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return {**stats, "wall_s": time.time() - started, "result": "no create_bill call",
                    "said": (msg.get("content") or "")[:300]}
        for call in calls:
            name, args = call["function"]["name"], call["function"]["arguments"]
            if name == "create_bill":
                wrong = {k: args.get(k) for k, v in EXPECTED.items()
                         if (str(args.get(k)).strip() != str(v) if isinstance(v, str)
                             else not isinstance(args.get(k), (int, float)) or abs(args[k] - v) > 1)}
                return {**stats, "wall_s": time.time() - started, "result": "correct" if not wrong else "wrong",
                        "wrong_fields": wrong}
            messages.append({"role": "tool", "tool_name": name, "content": _run_tool(name, args)})
    return {**stats, "wall_s": time.time() - started, "result": "ran out of turns"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--think", action="store_true")
    args = ap.parse_args()
    results = []
    for i in range(args.trials):
        res = trial(args.model, args.think)
        results.append(res)
        pp = res["prompt_tokens"] / res["prompt_s"] if res["prompt_s"] else 0
        tg = res["gen_tokens"] / res["gen_s"] if res["gen_s"] else 0
        print(f"trial {i + 1}: {res['result']:<20} {res['calls']} calls, {res['wall_s']:.0f}s wall | prefill "
              f"{pp:.0f} tok/s ({res['prompt_tokens']} tok) | decode {tg:.1f} tok/s ({res['gen_tokens']} tok)"
              + (f" | wrong: {res['wrong_fields']}" if res.get("wrong_fields") else "")
              + (f" | said: {res['said']!r}" if res.get("said") else ""))
    ok = sum(r["result"] == "correct" for r in results)
    print(json.dumps({"model": args.model, "think": args.think, "correct": f"{ok}/{len(results)}",
                      "mean_wall_s": round(sum(r["wall_s"] for r in results) / len(results))}))


if __name__ == "__main__":
    main()
