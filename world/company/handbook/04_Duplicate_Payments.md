# Duplicate Payments — Detection and Recovery

## What counts as a duplicate payment

A duplicate is two or more payments made against the same vendor invoice number for the same vendor. It
usually happens when one invoice was entered as two separate bills.

The same amount alone does **not** prove a duplicate. Vendors often bill identical amounts for repeat orders,
so always compare vendor invoice numbers.

## Recovery steps

1. Confirm it in the Finance Portal: find the bills with the same vendor invoice number, and the payments
   made against each one.
2. Email the vendor's registered address asking for a refund of the duplicate amount to Northwind's bank
   account (see the Company Overview). Quote the invoice number, both payment IDs, their UTRs and their
   dates. This needs AP Manager approval before it is sent.
3. Mark the later bill (the second entry) with status "Duplicate", with the reason
   "Duplicate of <original bill ID> – refund requested on <DD-MM-YYYY>".
4. Add a tracker row with status "Duplicate - Refund Requested".
5. Email the AP Manager a short summary.

Do not offset the amount against future invoices unless the AP Manager explicitly approves it.
