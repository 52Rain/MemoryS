---
id: d-acme-billing-grace-v1
kind: decision
scope: project/acme-billing
status: superseded
basis: owner_confirmed
summary: Invoice grace period is 7 days for all plans
use_when:
- grace period
- invoice
- suspension
effective_from: '2026-02-01'
effective_until: '2026-06-14'
superseded_by: d-acme-billing-grace-v2
subject_key: acme-billing.invoice-grace-period
source_refs:
- event:ev-demo-02
---

All unpaid annual invoices receive a 7-day grace period before API access is suspended.
