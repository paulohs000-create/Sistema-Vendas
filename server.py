import os
from datetime import date, datetime
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_cors import CORS

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
CORS(app)

# ---------------------------
# Database helpers (Postgres)
# ---------------------------

def get_database_url() -> str | None:
    # Railway typically exposes DATABASE_URL. Some setups use POSTGRES_URL/PG* vars.
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRESQL_URL")
        or os.environ.get("DATABASE_PRIVATE_URL")
    )

def get_db_connection():
    dsn = get_database_url()
    if not dsn:
        return None
    # psycopg3 supports postgres:// and postgresql:// DSNs
    return psycopg.connect(dsn, row_factory=dict_row)

# ---------------------------
# Error handling
# ---------------------------

def handle_errors(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except psycopg.Error as e:
            return jsonify({"error": "Erro no banco de dados", "detail": str(e)}), 500
        except Exception as e:
            return jsonify({"error": "Erro interno", "detail": str(e)}), 500
    return wrapper

# ---------------------------
# Auth helpers
# ---------------------------

def _parse_user_pass(user_value: str | None, pass_value: str | None):
    """
    Aceita:
      - user_value no formato 'user:pass' (uma variável só), OU
      - user_value='user' e pass_value='pass' (variáveis separadas)

    Retorna (user, pass) ou (None, None).
    """
    if user_value and ":" in user_value and (pass_value is None or pass_value == ""):
        u, p = user_value.split(":", 1)
        return u.strip(), p
    if user_value and pass_value:
        return user_value.strip(), pass_value
    if user_value and (pass_value is None):
        # user fornecido mas senha ausente
        return user_value.strip(), ""
    return None, None

def get_users_config():
    admin_u, admin_p = _parse_user_pass(os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASS"))
    caixa_u, caixa_p = _parse_user_pass(os.environ.get("CAIXA_USER"), os.environ.get("CAIXA_PASS"))
    cost_u, cost_p = _parse_user_pass(os.environ.get("COSTUREIRA_USER"), os.environ.get("COSTUREIRA_PASS"))

    return {
        "admin": {"user": admin_u, "pass": admin_p},
        "caixa": {"user": caixa_u, "pass": caixa_p},
        "costureira": {"user": cost_u, "pass": cost_p},
    }

def login_required(role: str | None = None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("role"):
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                return jsonify({"error": "Acesso negado"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# ---------------------------
# Page routes
# ---------------------------

@app.get("/")
def index():
    role = session.get("role")
    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    if role == "caixa":
        return redirect(url_for("atendimento_page"))
    if role == "costureira":
        return redirect(url_for("costureiras_page"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    users = get_users_config()

    for role, conf in users.items():
        if conf["user"] is None:
            continue
        if username == conf["user"] and password == (conf["pass"] or ""):
            session.clear()
            session["role"] = role
            session["username"] = username
            return redirect(url_for("index"))

    return render_template("login.html", error="Usuário ou senha inválidos"), 401

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/admin")
@login_required("admin")
def admin_dashboard():
    # precisa existir templates/admin.html no repo
    return render_template("admin.html", username=session.get("username"))

@app.get("/atendimento")
@login_required()
def atendimento_page():
    # caixa ou admin
    if session.get("role") not in ("caixa", "admin"):
        return redirect(url_for("login"))
    return render_template("atendimento.html")

@app.get("/gerenciamento")
@login_required()
def gerenciamento_page():
    # admin
    if session.get("role") not in ("admin",):
        return redirect(url_for("login"))
    return render_template("Gerenciamento.html")

@app.get("/costureiras")
@login_required()
def costureiras_page():
    # costureira ou admin
    if session.get("role") not in ("costureira", "admin"):
        return redirect(url_for("login"))
    return render_template("costureiras.html")

# ---------------------------
# Health & debug
# ---------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.get("/db-test")
@handle_errors
def db_test():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"ok": False, "error": "DATABASE_URL não configurada"}), 500
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS one")
            row = cur.fetchone()
    conn.close()
    return jsonify({"ok": True, "result": row})

# DEBUG TEMPORÁRIO (remova depois)
@app.get("/debug-env")
def debug_env():
    return {
        "ADMIN_USER": os.environ.get("ADMIN_USER"),
        "ADMIN_PASS": "***" if os.environ.get("ADMIN_PASS") else None,
        "CAIXA_USER": os.environ.get("CAIXA_USER"),
        "CAIXA_PASS": "***" if os.environ.get("CAIXA_PASS") else None,
        "COSTUREIRA_USER": os.environ.get("COSTUREIRA_USER"),
        "COSTUREIRA_PASS": "***" if os.environ.get("COSTUREIRA_PASS") else None,
    }

# ---------------------------
# API - Clients
# ---------------------------

@app.get("/clients/<phone>")
@handle_errors
def get_client_by_phone(phone):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id_client, name, phone, nif FROM clients WHERE phone = %s",
                (phone,),
            )
            row = cur.fetchone()

    conn.close()

    if not row:
        return jsonify({"error": "Cliente não encontrado"}), 404

    return jsonify(
        {
            "id": row["id_client"],
            "name": row["name"],
            "phone": row["phone"],
            "nif": row.get("nif"),
        }
    )

@app.post("/clients")
@handle_errors
def create_client():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    nif = (data.get("nif") or "").strip() or None

    if not name or not phone:
        return jsonify({"error": "Nome e telefone são obrigatórios"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clients (name, phone, nif) VALUES (%s, %s, %s) RETURNING id_client",
                (name, phone, nif),
            )
            new_id = cur.fetchone()["id_client"]

    conn.close()
    return jsonify({"id": new_id})

@app.put("/clients/<int:client_id>/nif")
@handle_errors
def update_client_nif(client_id):
    data = request.get_json(force=True, silent=True) or {}
    nif = (data.get("nif") or "").strip()
    if not nif:
        return jsonify({"error": "NIF é obrigatório"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE clients SET nif = %s WHERE id_client = %s", (nif, client_id))
            if cur.rowcount == 0:
                return jsonify({"error": "Cliente não encontrado"}), 404

    conn.close()
    return jsonify({"ok": True})

# ---------------------------
# API - Services
# ---------------------------

@app.get("/services")
@handle_errors
def list_services_grouped():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id_service, name, price, category FROM services ORDER BY category, name"
            )
            rows = cur.fetchall()

    conn.close()

    grouped = {}
    for r in rows:
        cat = r["category"]
        grouped.setdefault(cat, []).append(
            {"id": r["id_service"], "nome": r["name"], "preco": float(r["price"])}
        )

    return jsonify(grouped)

@app.post("/services")
@handle_errors
def create_service():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    price = data.get("price")

    if not name or not category or price is None:
        return jsonify({"error": "name, category e price são obrigatórios"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO services (name, category, price) VALUES (%s, %s, %s) RETURNING id_service",
                (name, category, price),
            )
            new_id = cur.fetchone()["id_service"]

    conn.close()
    return jsonify({"id": new_id})

@app.put("/services/<int:service_id>")
@handle_errors
def update_service(service_id):
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    price = data.get("price")

    if not name or not category or price is None:
        return jsonify({"error": "name, category e price são obrigatórios"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE services SET name=%s, category=%s, price=%s WHERE id_service=%s",
                (name, category, price, service_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Serviço não encontrado"}), 404

    conn.close()
    return jsonify({"ok": True})

@app.delete("/services/<int:service_id>")
@handle_errors
def delete_service(service_id):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM services WHERE id_service=%s", (service_id,))
            if cur.rowcount == 0:
                return jsonify({"error": "Serviço não encontrado"}), 404

    conn.close()
    return jsonify({"ok": True})

# ---------------------------
# API - Seamstresses (Costureiras)
# ---------------------------

@app.get("/seamstresses")
@handle_errors
def list_seamstresses():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id_seamstress, name FROM seamstresses ORDER BY name")
            rows = cur.fetchall()

    conn.close()
    return jsonify(rows)

@app.post("/seamstresses")
@handle_errors
def create_seamstress():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO seamstresses (name) VALUES (%s) RETURNING id_seamstress",
                (name,),
            )
            new_id = cur.fetchone()["id_seamstress"]

    conn.close()
    return jsonify({"id": new_id})

@app.put("/seamstresses/<int:seamstress_id>")
@handle_errors
def update_seamstress(seamstress_id):
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nome é obrigatório"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE seamstresses SET name=%s WHERE id_seamstress=%s",
                (name, seamstress_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Costureira não encontrada"}), 404

    conn.close()
    return jsonify({"ok": True})

@app.delete("/seamstresses/<int:seamstress_id>")
@handle_errors
def delete_seamstress(seamstress_id):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seamstresses WHERE id_seamstress=%s", (seamstress_id,))
            if cur.rowcount == 0:
                return jsonify({"error": "Costureira não encontrada"}), 404

    conn.close()
    return jsonify({"ok": True})

# ---------------------------
# API - Pedidos
# ---------------------------

@app.post("/pedidos")
@handle_errors
def create_pedido():
    data = request.get_json(force=True, silent=True) or {}

    client_id = data.get("clientId")
    delivery_date = data.get("deliveryDate")  # YYYY-MM-DD
    comments = data.get("comments")
    discount = data.get("discount") or 0
    total_price = data.get("totalPrice") or 0
    services = data.get("services") or []

    if not client_id or not delivery_date or not services:
        return jsonify({"error": "clientId, deliveryDate e services são obrigatórios"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    now = datetime.now()

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pedidos
                (id_client, data_entrada, data_prevista, observacoes, desconto, preco_total, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id_pedido
                """,
                (client_id, now, delivery_date, comments, discount, total_price, "Pendente"),
            )
            pedido_id = cur.fetchone()["id_pedido"]

            for s in services:
                service_id = s.get("id")
                qty = s.get("quantity") or 1
                desc = s.get("description")
                cur.execute(
                    """
                    INSERT INTO pedido_servicos
                    (id_pedido, id_service, quantity, description, status)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (pedido_id, service_id, qty, desc, "Pendente"),
                )

    conn.close()
    return jsonify({"pedido_id": pedido_id})

@app.get("/pedidos/stats")
@handle_errors
def pedidos_stats():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    today = date.today()

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(ps.id_pedido_servico) AS pending_today
                FROM pedido_servicos ps
                JOIN pedidos p ON ps.id_pedido = p.id_pedido
                WHERE ps.status = 'Pendente'
                  AND p.data_prevista = %s
                """,
                (today,),
            )
            pending_today = (cur.fetchone() or {}).get("pending_today") or 0

            cur.execute(
                """
                SELECT COUNT(id_pedido_servico) AS completed_today
                FROM pedido_servicos
                WHERE status = 'Concluído'
                  AND DATE(data_conclusao) = %s
                """,
                (today,),
            )
            completed_today = (cur.fetchone() or {}).get("completed_today") or 0

    conn.close()
    return jsonify({"pending_today": int(pending_today), "completed_today": int(completed_today)})

@app.get("/pedidos/pendentes")
@handle_errors
def pedidos_pendentes():
    selected_date = request.args.get("date")  # YYYY-MM-DD
    search_term = (request.args.get("search") or "").strip()

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    query = """
        SELECT
            ps.id_pedido_servico,
            ps.id_pedido,
            ps.quantity,
            ps.description,
            ps.status,
            s.name AS service_name,
            c.name AS client_name,
            TO_CHAR(p.data_prevista, 'YYYY-MM-DD') AS data_prevista
        FROM pedido_servicos ps
        JOIN pedidos p ON ps.id_pedido = p.id_pedido
        JOIN services s ON ps.id_service = s.id_service
        JOIN clients c ON p.id_client = c.id_client
        WHERE ps.status = 'Pendente'
    """
    params = []

    if selected_date:
        query += " AND p.data_prevista = %s"
        params.append(selected_date)

    if search_term:
        query += " AND (c.name ILIKE %s OR CAST(p.id_pedido AS TEXT) ILIKE %s)"
        like = f"%{search_term}%"
        params.extend([like, like])

    query += " ORDER BY p.data_prevista, ps.id_pedido_servico"

    with conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

    conn.close()
    return jsonify(rows)

@app.post("/pedidos/servico/<int:pedido_servico_id>/concluir")
@handle_errors
def concluir_pedido_servico(pedido_servico_id):
    data = request.get_json(force=True, silent=True) or {}
    seamstress_id = data.get("seamstressId")

    if not seamstress_id:
        return jsonify({"error": "seamstressId é obrigatório"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    now = datetime.now()

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pedido_servicos
                SET status = 'Concluído',
                    id_seamstress_conclusao = %s,
                    data_conclusao = %s
                WHERE id_pedido_servico = %s
                """,
                (seamstress_id, now, pedido_servico_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Serviço do pedido não encontrado"}), 404

    conn.close()
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
