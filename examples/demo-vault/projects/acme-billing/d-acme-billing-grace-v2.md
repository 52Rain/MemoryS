---
id: d-acme-billing-grace-v2
kind: decision
scope: project/acme-billing
status: active
basis: owner_confirmed
summary: Enterprise invoice grace period extended to 14 days
use_when:
- grace period
- invoice
- suspension
- enterprise
effective_from: '2026-06-15'
supersedes:
- d-acme-billing-grace-v1
subject_key: acme-billing.invoice-grace-period
source_refs:
- event:ev-demo-03
---

Starting 2026-06-15:
- Enterprise annual invoices receive a 14-day grace period before API suspension.
- Self-serve plans remain on a 7-day grace period.
