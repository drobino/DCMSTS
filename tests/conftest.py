from datetime import date, timedelta

import pyotp
import pytest

from dcmsts import create_app
from dcmsts.models import Asset, SlaPolicy, User, Vendor, db
from dcmsts.security import ensure_roles, hash_password, role_by_name
from dcmsts.models import install_db_guards

PASSWORD = "CorrectHorse123"


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "SECRET_KEY": "test", "TESTING": True, "WTF_CSRF_ENABLED": False, "SESSION_COOKIE_SECURE": False,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}", "UPLOAD_DIR": str(tmp_path / "uploads"),
        "BACKUP_DIR": str(tmp_path / "backups"), "TESTING_DISABLE_RATE_LIMIT": True,
    })
    with app.app_context():
        db.create_all()
        install_db_guards()
        ensure_roles()
        yield app
        db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(role, email=None, skills="", vendor=None, certs=None, mfa=None):
    u = User(full_name=f"{role} {email or ''}".strip(), email=email or f"{role.lower()}@t.local",
             password_hash=hash_password(PASSWORD), role_id=role_by_name(role).role_id, skills=skills,
             vendor_id=vendor.vendor_id if vendor else None, certifications=certs or [])
    if mfa or role in ("Administrator", "Manager"):
        u.mfa_secret, u.mfa_enabled = pyotp.random_base32(), True
    db.session.add(u)
    db.session.commit()
    return u


def login(client, user, password=PASSWORD):
    resp = client.post("/login", data={"email": user.email, "password": password})
    if user.mfa_enabled:
        resp = client.post("/mfa", data={"code": pyotp.TOTP(user.mfa_secret).now()})
    return resp


def make_asset(tag="UPS-1", category="UPS", **kw):
    a = Asset(asset_tag=tag, name=f"{category} {tag}", category=category, install_date=kw.pop("install_date", date(2020, 1, 1)), **kw)
    db.session.add(a)
    db.session.commit()
    return a


def make_vendor(valid=True, skills="hvac"):
    today = date.today()
    v = Vendor(company_name="Vend Co", contact_email="v@x.example", skills=skills,
               contract_start=today - timedelta(days=30),
               contract_end=today + timedelta(days=30) if valid else today - timedelta(days=1))
    db.session.add(v)
    db.session.commit()
    return v


def default_sla(chain=None, final_vendor=None):
    for prio, res in {"low": 168, "medium": 72, "high": 24, "critical": 8}.items():
        db.session.add(SlaPolicy(priority=prio, response_hours=1, resolution_hours=res, escalation_hours=2,
                                 escalation_chain=chain or [], final_vendor_id=final_vendor))
    db.session.commit()
