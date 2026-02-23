import os
import base64
import traceback
from datetime import date, datetime
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    Response,
)

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
def _get_database_url() -> str | None:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("PGDATABASE")
    )

DATABASE_URL = _get_database_url()

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

# -----------------------------------------------------------------------------
# Auth helpers (mantém seu comportamento atual)
# -----------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
@app.get("/")
@login_required
def atendimento():
    return render_template(
        "atendimento.html",
        username=session.get("username"),
        role=session.get("role", ""),
        store_name=os.environ.get("STORE_NAME", "Ponto&Linha"),
    )

# -----------------------------------------------------------------------------
# QZ Tray endpoints
# -----------------------------------------------------------------------------
@app.get("/qz/health")
def qz_health():
    cert_path = os.path.join(app.static_folder or "static", "qz", "certificate.pem")
    return jsonify(
        {
            "ok": True,
            "certificate_url": "/static/qz/certificate.pem",
            "has_certificate_file": os.path.exists(cert_path),
            "has_private_key_env": bool((os.environ.get("QZ_PRIVATE_KEY_PEM") or "").strip()),
        }
    )

@app.get("/qz/certificate")
def qz_certificate():
    """Serve o certificado como text/plain para evitar download/cache estranho."""
    cert_path = os.path.join(app.static_folder or "static", "qz", "certificate.pem")
    if not os.path.exists(cert_path):
        return jsonify({"error": "certificate.pem não encontrado em static/qz/"}), 404
    with open(cert_path, "rb") as f:
        pem = f.read()
    return Response(pem, mimetype="text/plain; charset=utf-8")

@app.post("/qz/sign")
def qz_sign():
    """Assina o payload do QZ e devolve assinatura base64 (texto puro)."""
    try:
        data = request.get_data()  # bytes

        private_key_pem = (os.environ.get("QZ_PRIVATE_KEY_PEM") or "").strip()
        if not private_key_pem:
            raise RuntimeError("QZ_PRIVATE_KEY_PEM vazio (configure no Railway > Variables).")

        try:
            from OpenSSL import crypto  # type: ignore
        except Exception as e:
            raise RuntimeError("pyOpenSSL não instalado (adicione 'pyOpenSSL' no requirements.txt).") from e

        algo = (os.environ.get("QZ_SIGNATURE_ALG") or "sha256").strip().lower()
        if algo not in ("sha256", "sha1"):
            algo = "sha256"

        pkey = crypto.load_privatekey(crypto.FILETYPE_PEM, private_key_pem.encode("utf-8"))
        signature = crypto.sign(pkey, data, algo)
        signature_b64 = base64.b64encode(signature).decode("utf-8").strip()

        # QZ espera texto puro
        return signature_b64, 200, {"Content-Type": "text/plain; charset=utf-8"}

    except Exception as e:
        print("[QZ/SIGN] ERRO:", repr(e))
        print(traceback.format_exc())
        return jsonify({"error": "qz_sign_failed", "detail": str(e)}), 500

# -----------------------------------------------------------------------------
# (Restante do seu app: login, clientes, serviços, pedidos, etc.)
# OBS: Este arquivo é uma base com os endpoints QZ corrigidos.
# -----------------------------------------------------------------------------
@app.get("/login")
def login():
    return "login placeholder", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
