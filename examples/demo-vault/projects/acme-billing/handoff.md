# Handoff — Acme Billing

- **Goal**: Reconcile Q2 overdue invoice suspension jobs with the updated grace period rule.
- **Completed**: Verified dunning webhook retries for self-serve accounts.
- **Next Step**: Update enterprise grace-period timer to 14 days and backfill invoices dated before 2026-06-15 using the historical 7-day rule (`as_of=2026-05-01`).
