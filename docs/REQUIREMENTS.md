# Requirements, scope and acceptance criteria

Source: `Intelligent_DCMSTS.pdf` (FR-01 to FR-15, NFRs in §6.3, controls in §7, UC-01 to UC-20, TC-01 to TC-10).

## MUST (implemented in Phase 1)

| Req | Acceptance criterion | Verified by |
|---|---|---|
| FR-01 / UC-01 | Monthly/weekly/daily/yearly recurring schedule generates future work orders with correct due dates; month-end clamps; end < start and empty checklist are rejected with nothing saved; overlapping schedule needs explicit confirmation | `test_tc01_*`, `test_month_end_dates_clamp`, `test_schedule_*`, `test_generation_is_idempotent` |
| FR-02 | Ad-hoc corrective work order / fault report with optional incident downtime; technicians can report faults and managers are notified | `test_pages`, `test_downtime_report_*` |
| FR-03 / UC-02 / UC-11 | Candidates are filtered by skill, ranked by priority-weighted workload, same-day conflicts flagged; overlapping high-priority or off-list assignment requires an override reason; vendors need a valid contract | `test_tc02_*`, `test_vendor_requires_valid_contract`, `test_assignment_outside_suggestions_needs_reason` |
| FR-04 / UC-03 | Status lifecycle assigned → in progress → blocked/completed; completion blocked until all checklist items are done and, for configured categories, evidence is uploaded; blocking requires a reason and notifies managers; parts log locked after 24 h for non-admins | `test_tc03_*`, `test_blocking_*`, `test_parts_log_locked_*`, `test_evidence_content_type_checked` |
| FR-05 | Time-stamped history per asset (work orders, activity log, parts, evidence, readings, predictions); exportable | asset page, `asset_history_report` |
| FR-06 / UC-04 | Reminder before due date (once); overdue marked and managers notified; escalation through chain tiers at the configured interval; final vendor tier drafts a vendor work order; failed deliveries retry with backoff and are logged | `test_tc04_*`, `test_reminder_sent_once_before_due`, `test_failed_delivery_retries_then_logs` |
| FR-07 / UC-07 | Roles Administrator, Manager, Technician, Vendor are enforced server-side on every route; technicians see only their own work orders, vendors only their organisation's; denials are audited | `test_tc07_*`, `test_technician_*`, `test_vendor_*`, `test_pages` |
| FR-08 / UC-05 | Compliance, SLA, downtime and asset-history reports with PDF and Excel export; an empty range shows a message instead of a report | `test_tc05_*`, `test_sla_report_*`, `test_empty_range_message_and_exports` |
| FR-09 | Audit log is append-only (DB triggers) and SHA-256 hash-chained; covers create/update/delete/status/login/denied/permission events with before/after values, IP and user agent | `test_audit_*` |
| FR-10 / UC-14 | SLA thresholds and escalation chains per priority and optionally per category; zero or negative values rejected; changes apply only to new work orders | `test_sla_change_does_not_affect_in_flight_orders` |
| FR-11 / UC-15 | Telemetry by manual entry or CSV; type and range validation; invalid rows quarantined; batch halted above the configured ratio; batches versioned and reversible | `test_telemetry_*` |
| FR-12 / UC-06 / UC-17 | Risk score 0–1, top contributing factors in plain language, recommended window, low-confidence flag, alert above threshold, review queue with approve/adjust/dismiss (reasons required), bulk dismiss, dismissed items retained, no automatic work orders | `test_tc08_*`, `test_review_*`, `test_adjust_*`, `test_queue_*`, `test_rescoring_supersedes_pending` |
| UC-16 | Training with time-based holdout, precision/recall/AUC, explicit failure on insufficient positives, manual promotion, rollback to a retired version | `test_training_*`, `test_features_do_not_leak_future` |
| FR-13 / UC-13 | Technician skills and certifications with expiry; an expired certification removes the matching skill from suggestions; per-technician performance (completed, on time, average resolution) | `test_expired_certification_removes_skill_match`, People screens |
| FR-14 / UC-18 | Downtime per incident; MTTR and MTBF per asset; planned downtime reported separately; ongoing incidents excluded from MTTR and flagged | `test_downtime_report_mttr_mtbf_planned_and_ongoing` |
| FR-15 / UC-12 | Asset registration, edit and lifecycle status; duplicate tag rejected; all-or-nothing CSV import; assets with history cannot be hard-deleted; retirement deactivates schedules | `test_duplicate_asset_tag_rejected`, `test_asset_csv_import_*`, `test_retiring_*`, `test_asset_with_history_*` |
| UC-09 | Month calendar filterable by asset, technician, vendor and status; technician and vendor views scoped automatically; empty state | `test_pages` |
| UC-10 | Manager reschedule/reassign with SLA-window warning and reschedule limit (note required); technician changes are requests pending manager approval | `test_reschedule_limit_requires_note`, `test_technician_reschedule_is_a_request` |
| UC-19 | Create users, change role and status (suspend/deactivate), reset password and MFA; privilege change ends sessions; the last active admin cannot be removed | `test_role_change_invalidates_sessions`, `test_last_admin_cannot_be_removed` |
| UC-19a | Managers (and administrators) can add **Technician** accounts from People → Technicians; the role is fixed server-side, so other roles remain administrator-only; each creation is audited | `test_manager_adds_technician_from_people_page`, `test_add_technician_validation_and_limits` |
| UC-20 / TC-10 | Backup with checksums, automatic verification, retention, restore; corruption detected | `test_tc10_*`, `test_corrupted_backup_*` |
| §7 Secure authentication | Unique accounts; TOTP MFA mandatory for Administrator and Manager; scrypt salted hashes; minimum 12 characters with letters and digits; lockout after 5 failures for 15 min with admin alert; per-IP rate limit | `test_lockout_*`, `test_manager_requires_mfa_*`, `test_mfa_wrong_code_rejected`, `test_password_policy` |
| §7 Sessions | Idle timeout (30 min default); invalidated on logout, password change and role change | `test_idle_timeout`, `test_role_change_*` |
| §7 Web attacks | CSRF tokens on every POST, strict CSP with no inline script, X-Frame-Options, nosniff, HSTS (secure mode), Jinja autoescape, upload type and magic-byte checks, 10 MB limit, spreadsheet formula neutralisation, safe redirects | `test_csrf_enforced`, `test_security_headers`, `test_tc09_*`, `test_xlsx_*`, `test_open_redirect_blocked` |

## SHOULD (partially done or next)

- **TC-06 offline capture** is implemented as a browser-side queue (`static/app.js`): status, checklist and log forms
  submitted while offline are stored on the device and replayed with their CSRF token when connectivity returns.
  Photo uploads still need connectivity. There is no automated browser test.
- Scheduled report emails (UC-05 4a) are not built. Reports are generated on demand.
- Rule-template library (UC-08) is not built. Recurrence and checklist are defined per schedule.
- Device-bound mobile tokens and concurrent-session limits for admins (§7) are not built. Web sessions are used.

## WON'T YET (explicit Phase 1 exclusions or deferred)

- Native iOS/Android app. The responsive web UI is the field interface.
- SMS/push channels. Email plus the in-app Inbox only; the `channel` column is ready for more.
- BMS/SCADA integration, ERP procurement, unattended retraining (spec §3.2).
- Multi-site facility scoping in RBAC. Location is a free-text asset attribute.
- Calendar sync (Outlook/Google).
- The 99.999% availability and 500-concurrent-user NFRs are deployment properties (redundant hosts, managed
  PostgreSQL, load balancer) and have not been load-tested.

## Assumptions made

- Spec work-order status `overdue` is modelled as a separate `is_overdue` flag so the lifecycle state is not lost.
- Predictions add a `superseded` decision: re-scoring retires older pending predictions so the queue shows one
  current recommendation per asset (history is kept).
- Without SMTP configured, notifications are logged and visible in each user's Inbox.
- The predictive label is "a corrective work order is created within 30 days", matching the §1.5 success criterion.
