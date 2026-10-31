"""Synthetic demo facility. Degradation episodes raise temperature/load before failures, and missed PMs raise
failure hazard, so the predictive model has a realistic (but artificial) signal to learn."""
import random
from datetime import datetime, time, timedelta

from .models import (Asset, MaintSchedule, SensorReading, SlaPolicy, User, Vendor, WorkOrder, db)
from .security import audit, ensure_roles, hash_password, role_by_name
from .services.scheduling import generate_instances, occurrences

CATEGORIES = {
    "UPS": ("electrical", 26, 55, ["Inspect battery strings", "Check load and alarms", "Thermal scan connections"]),
    "CRAC": ("hvac", 22, 65, ["Replace/inspect filters", "Check refrigerant pressures", "Clean condensate drain"]),
    "PDU": ("electrical", 27, 50, ["Verify breaker loads", "Inspect connections", "Check monitoring feed"]),
    "Generator": ("mechanical", 30, 20, ["Run load test 30 min", "Check fuel and coolant", "Inspect battery"]),
    "Fire Suppression": ("fire", 21, 5, ["Check agent pressure", "Inspect detectors", "Test panel"]),
    "Network": ("network", 28, 45, ["Check redundant links", "Review error counters", "Inspect cooling fans"]),
}
CAUSES = ["component failure", "overheating", "power fluctuation", "wear", "firmware fault"]


def seed_demo(password, today):
    rng = random.Random(42)
    if Asset.query.first():
        return "Database already contains assets; demo seed skipped."
    ensure_roles()
    now = datetime.combine(today, time(9, 0))

    def user(name, email, role, skills="", vendor=None, certs=None):
        u = User(full_name=name, email=email, password_hash=hash_password(password), role_id=role_by_name(role).role_id,
                 skills=skills, vendor_id=vendor.vendor_id if vendor else None, certifications=certs or [])
        db.session.add(u)
        return u

    v1 = Vendor(company_name="CoolAir Services Ltd", contact_email="ops@coolair.example", service_type="HVAC",
                skills="hvac", contract_start=today - timedelta(days=200), contract_end=today + timedelta(days=165))
    v2 = Vendor(company_name="VoltCare Electrical", contact_email="desk@voltcare.example", service_type="electrical",
                skills="electrical", contract_start=today - timedelta(days=500), contract_end=today - timedelta(days=10))
    db.session.add_all([v1, v2])
    db.session.flush()
    admin = user("Ama Admin", "admin@dcmsts.local", "Administrator")
    user("Kofi Manager", "manager@dcmsts.local", "Manager")
    techs = [
        user("Esi Mensah", "esi@dcmsts.local", "Technician", "electrical, ups",
             certs=[{"name": "electrical", "expires": (today + timedelta(days=300)).isoformat()}]),
        user("Yaw Boateng", "yaw@dcmsts.local", "Technician", "hvac, mechanical"),
        user("Akosua Owusu", "akosua@dcmsts.local", "Technician", "network, fire",
             certs=[{"name": "fire", "expires": (today - timedelta(days=5)).isoformat()}]),
        user("Kwame Asante", "kwame@dcmsts.local", "Technician", "electrical, mechanical"),
    ]
    user("CoolAir Field Engineer", "vendor@coolair.example", "Vendor", vendor=v1)
    db.session.flush()

    for prio, (resp, res, esc) in {"critical": (1, 8, 2), "high": (4, 24, 4), "medium": (24, 72, 12),
                                   "low": (48, 168, 24)}.items():
        db.session.add(SlaPolicy(priority=prio, category=None, response_hours=resp, resolution_hours=res,
                                 escalation_hours=esc, escalation_chain=[admin.user_id]))

    start = now - timedelta(days=540)
    assets = []
    n = 0
    for cat, (skill, base_t, base_load, checklist) in CATEGORIES.items():
        for i in range(1, 5 if cat not in ("Fire Suppression", "Generator") else 3):
            n += 1
            install = today - timedelta(days=rng.randint(400, 3600))
            a = Asset(asset_tag=f"{cat[:3].upper()}-{i:03d}", name=f"{cat} unit {i}", category=cat,
                      location=f"Hall {'AB'[i % 2]} / Row {rng.randint(1, 12)}",
                      manufacturer=rng.choice(["Schneider", "Vertiv", "Eaton", "Stulz", "Cisco", "Caterpillar"]),
                      install_date=install, warranty_expiry=install + timedelta(days=rng.choice([1095, 1825])))
            db.session.add(a)
            assets.append((a, skill, base_t, base_load, checklist))
    db.session.flush()

    pm_count = fail_count = reading_count = 0
    degrading_now = {0, 5, 9}  # indexes of assets currently in a degradation episode
    for idx, (a, skill, base_t, base_load, checklist) in enumerate(assets):
        sched = MaintSchedule(asset_id=a.asset_id, title=f"Monthly PM – {a.category}", recurrence_rule="FREQ=MONTHLY;INTERVAL=1",
                              checklist_template=checklist, start_date=start.date(), next_due_date=start.date(),
                              priority="medium", required_skill=skill, estimated_minutes=60,
                              compliance_ref="Manufacturer maintenance manual")
        sched.asset = a
        db.session.add(sched)
        db.session.flush()
        missed_dates = []
        for d in occurrences(sched.recurrence_rule, start.date(), today - timedelta(days=1)):
            due = datetime.combine(d, time(17, 0))
            tech = rng.choice([t for t in techs if skill in t.skills] or techs)
            roll = rng.random()
            wo = WorkOrder(asset_id=a.asset_id, schedule_id=sched.schedule_id, type="preventive", priority="medium",
                           title=sched.title, due_date=due, created_at=due - timedelta(days=30), assigned_to=tech.user_id,
                           checklist=[{"item": c, "done": roll < 0.93} for c in checklist], required_skill=skill,
                           sla_resolution_hours=72)
            if roll < 0.82:
                wo.status, wo.completed_at = "completed", due - timedelta(hours=rng.randint(1, 72))
            elif roll < 0.93:
                wo.status, wo.completed_at = "completed", due + timedelta(days=rng.randint(1, 10))
            else:
                wo.status, wo.is_overdue = "assigned", True
                wo.escalation_level, wo.last_escalated_at = 1, due + timedelta(hours=1)  # already escalated historically
                missed_dates.append(due)
            if wo.completed_at and wo.completed_at > now:
                wo.completed_at = now - timedelta(hours=1)
            wo.time_spent_minutes = rng.randint(30, 120) if wo.completed_at else 0
            db.session.add(wo)
            pm_count += 1
        for d in occurrences(sched.recurrence_rule, start.date(), today + timedelta(days=62)):
            if d >= today:
                sched.next_due_date = d
                break

        # weekly telemetry + failures driven by degradation episodes
        t = start
        episode_left = 0
        runtime = rng.uniform(5000, 40000)
        while t < now:
            weeks_to_now = (now - t).days // 7
            if idx in degrading_now and weeks_to_now == 4:
                episode_left = 6
            elif episode_left == 0 and rng.random() < 0.025 and weeks_to_now > 6:
                episode_left = rng.randint(3, 6)
            stress = 1.0 if episode_left else 0.0
            temp = base_t + rng.gauss(0, 1.2) + stress * rng.uniform(5, 9)
            load = max(0, base_load + rng.gauss(0, 5) + stress * rng.uniform(10, 20))
            runtime += 168
            for metric, value in (("temperature", temp), ("load_pct", min(load, 150)), ("runtime_hours", runtime)):
                db.session.add(SensorReading(asset_id=a.asset_id, metric_name=metric, metric_value=round(value, 2),
                                             source="csv_import", recorded_at=t))
                reading_count += 1
            recent_missed = any(timedelta(0) <= t - m <= timedelta(days=60) for m in missed_dates)
            hazard = 0.004 + (0.16 if episode_left and episode_left <= 3 else 0.03 * stress) + (0.05 if recent_missed else 0)
            if weeks_to_now >= 1 and rng.random() < hazard:
                fail_at = t + timedelta(hours=rng.randint(1, 150))
                hours = rng.uniform(1.5, 30)
                end = fail_at + timedelta(hours=hours)
                if end < now:
                    db.session.add(WorkOrder(
                        asset_id=a.asset_id, type="corrective", priority=rng.choice(["high", "critical", "medium"]),
                        title=f"Fault: {rng.choice(CAUSES)}", status="completed", created_at=fail_at,
                        due_date=fail_at + timedelta(hours=24), completed_at=end, sla_resolution_hours=24,
                        assigned_to=rng.choice(techs).user_id, downtime_start=fail_at, downtime_end=end,
                        downtime_cause=rng.choice(CAUSES), root_cause="Replaced faulty component",
                        time_spent_minutes=int(hours * 30)))
                    fail_count += 1
                    episode_left = 0
            if episode_left:
                episode_left -= 1
            t += timedelta(days=7)

    db.session.flush()
    generated = 0
    for s in MaintSchedule.query.all():
        generated += len(generate_instances(s, today, horizon_days=45))
    # a little live state: one incident in progress, a few assignments
    crac = Asset.query.filter_by(asset_tag="CRA-001").one()
    db.session.add(WorkOrder(asset_id=crac.asset_id, type="corrective", priority="high", title="Fault: high return-air temperature",
                             status="unassigned", created_at=now - timedelta(hours=30), due_date=now - timedelta(hours=6),
                             sla_resolution_hours=24, sla_escalation_hours=4, sla_chain=[admin.user_id],
                             downtime_start=now - timedelta(hours=30), downtime_cause="overheating",
                             checklist=[{"item": "Diagnose compressor", "done": False}], required_skill="hvac"))
    upcoming = WorkOrder.query.filter(WorkOrder.status == "unassigned", WorkOrder.due_date > now).order_by(WorkOrder.due_date).limit(6).all()
    for wo in upcoming:
        wo.assigned_to = techs[0].user_id if wo.required_skill == "electrical" else techs[1].user_id
        wo.status = "assigned"
    audit("seed_demo", "SYSTEM", after={"assets": len(assets), "failures": fail_count}, user_id=None)
    db.session.commit()
    return (f"Seeded {len(assets)} assets, {pm_count} historical PM work orders, {fail_count} failures, "
            f"{reading_count} readings, {generated} upcoming tasks.\n"
            f"Logins (password '{password}'): admin@dcmsts.local, manager@dcmsts.local (MFA enrolment on first login), "
            "esi@dcmsts.local, yaw@dcmsts.local, akosua@dcmsts.local, kwame@dcmsts.local (technicians), "
            "vendor@coolair.example (vendor).")
