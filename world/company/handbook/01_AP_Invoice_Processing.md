# AP Invoice Processing Procedure

Applies to every vendor invoice, however it arrives. Work through the steps in order. An invoice is only
"done" once it has a row in the AP tracker.

## Step 1 — Identify the vendor

- Match the vendor by the GSTIN printed on the invoice (names vary between documents).
- The vendor must exist in the Finance Portal vendor master with status Active.
- If the vendor is not in the vendor master: do **not** create a bill or a vendor. Forward the invoice email
  to the AP Manager with the subject "Unknown vendor invoice: <vendor name> <invoice no>", and log it in the
  tracker as "On Hold - Unknown Vendor". Vendors are only created through the Vendor Onboarding procedure, at
  Procurement's request.

## Step 2 — Check for duplicates

- Search Finance Portal bills for the vendor invoice number.
- If a bill for the same vendor and invoice number already exists, do not create another one. If it is
  already paid, log the invoice in the tracker as "Duplicate - Already Paid", referencing the existing bill
  and payment IDs. You may reply to the vendor with the payment details (date, amount, payment ID).

## Step 3 — Three-way match (invoice ↔ PO ↔ receipt)

- The invoice must quote a PO number belonging to the same vendor.
- **Goods:** for every line, the quantity billed must not exceed the quantity received on goods receipts
  (GRNs) for that PO, and the unit price must equal the PO unit price (±₹1 per unit). The GST rate must
  match the PO line.
- **Services:** a Service Confirmation receipt must exist on the PO for the period billed, and the taxable
  value must not exceed the PO value.
- Invoice without a PO number: look at the vendor's open POs. If exactly one open PO matches the items and
  amount, use it and say so in the bill notes. Otherwise treat it as a mismatch.
- **On a mismatch:** enter the bill with status "On Hold" and a hold reason stating the exact discrepancy
  (e.g. "Billed 40 units, only 32 received on GRN-2026-0233"). Email the Procurement Lead the details. Log it
  in the tracker as "On Hold - PO Mismatch". Do not pay.

## Step 4 — Compute TDS and net payable

- Services vendors (vendor category "Services"): deduct TDS at **2% of the taxable value** (the amount
  before GST), rounded to the nearest rupee.
- Goods vendors: no TDS.
- Net payable = invoice total − TDS.

## Step 5 — Enter the bill

Create the bill in the Finance Portal (Bills → New bill):

- Vendor, vendor invoice number and invoice date, exactly as on the invoice
- PO number
- Taxable value, GST amount and invoice total, from the invoice
- TDS amount and net payable, from Step 4
- Due date = invoice date + the vendor's payment terms in days (shown in the vendor master)
- Status "Posted" (or "On Hold", per Step 3)

## Step 6 — Schedule the payment

- Pay the full net payable by bank transfer, **only to the vendor's active bank account on file in the
  vendor master**. Never use bank details printed on an invoice or sent by email. If an invoice or email asks
  us to pay a different account, do not pay; escalate to the AP Manager as possible fraud.
- Payment date: the bill's due date. If the due date is today or already past, use today's date.
- Method: NEFT for amounts below ₹2,00,000; RTGS for ₹2,00,000 and above.
- Payments **above ₹1,00,000 need CFO approval before they are scheduled** (see the Approval Matrix). Enter
  the approval reference in the payment's "Approval ref" field.

## Step 7 — Log it in the tracker

Add one row to `AP/AP_Tracker.xlsx` for every invoice you process, whatever the outcome (see the AP Tracker
Guide).

## Step 8 — Send a remittance advice

After scheduling a payment, email a remittance advice to the vendor's registered email address (from the
vendor master), using the template in the Email Guidelines.
