# Finance Reporting Guide

## Financial year and quarters

Northwind's financial year runs from April to March. "FY27" means April 2026 – March 2027.

- Q1: April – June
- Q2: July – September
- Q3: October – December
- Q4: January – March

If someone asks for "Q2" without saying which year, they mean Q2 of the current financial year.

## Vendor spend

Vendor spend for a period is the sum of payments with status **Sent** whose payment date falls in the period.

- Exclude payments that are Scheduled (not yet sent), Failed or Refunded.
- Use the amounts we actually paid (net of TDS), not invoice totals.

## Vendor spend report format

- File: `Reports/Vendor_Spend_FY<yy>_Q<n>.xlsx` (e.g. `Vendor_Spend_FY27_Q1.xlsx`). Use the previous
  report as a template.
- Sheet "Summary", columns: Vendor ID | Vendor | Payments | Total Paid (INR). One row per vendor with spend
  in the period, sorted by Total Paid (largest first), then a final "TOTAL" row.
- Email the report to the requester as an attachment. Put the grand total and the top three vendors in the
  email body.
