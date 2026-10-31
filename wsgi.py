"""WSGI entry point for production (gunicorn wsgi:app) behind a reverse proxy.

ProxyFix trusts one proxy hop (nginx on the same host) for X-Forwarded-For/-Proto/-Host, so the audit log
and the login rate limiter see the real client IP and Flask knows the request arrived over HTTPS.
"""
from werkzeug.middleware.proxy_fix import ProxyFix

from dcmsts import create_app

app = create_app()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
