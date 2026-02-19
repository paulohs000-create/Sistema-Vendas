import os
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    session,
    redirect,
    url_for,
)
from flask_cors import CORS

import psycopg
from psycopg.rows import dict_row


app = Flask(__name__, template_folder="templates")
CORS(app)

# IMPORTANTE: no Railway você deve ter SECRET_KEY e DATABASE_URL nas Variables
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# Perfis/usuários via variáveis (Railway Variables)
# Você pode setar algo simples assim:
# ADMIN_USER=admin:senha
# CAIXA_USER=caixa:senha
# COSTUREIRA_USER=costureira:senha
ADMIN_USER = os.environ.get("ADMIN_USER", "admin:admin")
CAIXA_USER = os.environ.get("CAIXA_USER", "caixa:caixa")
COSTUREIRA_USER = os.environ.get("COSTUREIRA_USER", "costureira:costureira")

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
if not DATABASE_URL:
    # Não dá crash aqui, mas suas rotas de DB vão retornar erro 500 se não tiver URL
    print("AVISO: DATABASE_URL não definido. Configure nas Variables do Railway.")


# -----------------------
# DB helpers
# -----------------------
def get_db_connection():
    """
    Cria conexão com Postgres usando DATABASE_URL do Railway.
    Usa dict_row para retornar dicionários nas consultas.
    """
    if not DATABASE_URL:
        return None
    # psycopg aceita URL no formato postgresql://...
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return conn


def handle_errors(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except psycopg.Error as db_err:
            print(f"ERRO DB na rota {request.path}: {db_err}")
            return jsonify({"error": f"Erro de banco de dados: {str(db_err)}"}), 500
        except Exception as e:
            print(f"ERRO INESPERADO na rota {request.path}: {e}")
            return jsonify({"error": f"Ocorreu um erro inesperado: {str(e)}"}), 500

    return decorated_function


# -----------------------
# Auth helpers
# -----------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return decorated


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            if session.get("role") not in roles:
                return jsonify({"error": "Acesso negado"}), 403
            return f(*args, **kwargs)

        return decorated

    return decorator


def _parse_user_cred(env_value: str):
    """
    Espera formato 'user:pass'. Se não tiver ':', usa a string inteira como user e senha vazia.
    """
    if ":" in env_value:
        u, p = env_value.split(":", 1)
        return u.strip(), p.strip()
    return env_value.strip(), ""


def _check_credentials(username: str, password: str):
    au, ap = _parse_user_cred(ADMIN_USER)
    cu, cp = _parse_user_cred(CAIXA_USER)
    su, sp = _parse_user_cred(COSTUREIRA_USER)

    if username == au and password == ap:
        return "admin"
    if username == cu and password == cp:
        return "caixa"
    if username == su and password == sp:
        return "costureira"
    return None


# -----------------------
# Pages
# -----------------------
@app.route("/login", methods=["GET", "POST"])
@handle_errors
def login():
    if request.method == "GET":
        return render_template("login.html")

    data = request.form or request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    role = _check_credentials(username, password)
    if not role:
        # Se for fetch JSON
        if request.is_json:
            return jsonify({"error": "Usuário ou senha inválidos"}), 401
        # Se for form normal
        return render_template("login.html", error="Usuário ou senha inválidos"), 401

    session["user"] = username
    session["role"] = role

    # Redirect por perfil
    if role == "admin":
        return redirect("/admin")
    if role == "caixa":
        return redirect("/")
    if role == "costureira":
        return redirect("/costureiras")

    return redirect("/")


@app.route("/logout")
@handle_errors
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@handle_errors
@login_required
def serve_main_page():
    # Caixa e Admin podem ver atendimento (se você quiser restringir só caixa, me diga)
    return render_template("atendimento.html")


@app.route("/gerenciamento")
@handle_errors
@role_required("admin")
def serve_management_page():
    return render_template("gerenciamento.html")


@app.route("/costureiras")
@handle_errors
@login_required
def serve_seamstress_page():
    # Se quiser restringir para costureira/admin, ajuste aqui:
    # @role_required("costureira","admin")
    return render_template("costureiras.html")


@app.route("/admin")
@handle_errors
@role_required("admin")
def serve_admin_page():
    return render_template("admin.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/db-test")
@handle_errors
def db_test():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Sem DATABASE_URL"}), 500
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 as ok;")
            row = cur.fetchone()
    conn.close()
    return jsonify(row), 200


# -----------------------
# Clientes
# -----------------------
@app.route("/clients/<phone>", methods=["GET"])
@handle_errors
def get_client_by_phone(phone):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id_client, name, nif FROM clients WHERE phone = %s",
                (phone,),
            )
            client = cur.fetchone()

    conn.close()
    if client:
        return jsonify({"id": client["id_client"], "name": client["name"], "nif": client["nif"]}), 200
    return jsonify({"error": "Cliente não encontrado"}), 404


@app.route("/clients", methods=["POST"])
@handle_errors
def add_client():
    data = request.json or {}
    name = data.get("name")
    phone = data.get("phone")
    nif = data.get("nif")

    if not name or not phone:
        return jsonify({"error": "Nome e telefone são obrigatórios."}), 400

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
    return jsonify({"message": "Cliente cadastrado com sucesso", "id": new_id}), 201


@app.route("/clients/<int:client_id>/nif", methods=["PUT"])
@handle_errors
def update_client_nif(client_id):
    data = request.json or {}
    nif = data.get("nif")
    if not nif:
        return jsonify({"error": "O NIF é obrigatório."}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clients SET nif = %s WHERE id_client = %s",
                (nif, client_id),
            )
            updated = cur.rowcount

    conn.close()
    if updated == 0:
        return jsonify({"error": "Cliente não encontrado."}), 404
    return jsonify({"message": "NIF do cliente atualizado com sucesso."}), 200


# -----------------------
# Serviços
# -----------------------
@app.route("/services", methods=["GET"])
@handle_errors
def get_services():
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

    services_by_category = {}
    for r in rows:
        cat = r["category"]
        services_by_category.setdefault(cat, [])
        services_by_category[cat].append(
            {"id": r["id_service"], "nome": r["name"], "preco": float(r["price"])}
        )
    return jsonify(services_by_category), 200


@app.route("/services", methods=["POST"])
@handle_errors
@role_required("admin")
def add_service():
    data = request.json or {}
    name = data.get("name")
    category = data.get("category")
    price = data.get("price")

    if not name or not category or price is None:
        return jsonify({"error": "Nome, categoria e preço são obrigatórios."}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO services (name, category, price) VALUES (%s, %s, %s) RETURNING id_service",
                (name, category, float(price)),
            )
            new_id = cur.fetchone()["id_service"]

    conn.close()
    return jsonify({"message": "Serviço adicionado com sucesso", "id": new_id}), 201


@app.route("/services/<int:service_id>", methods=["PUT"])
@handle_errors
@role_required("admin")
def update_service(service_id):
    data = request.json or {}
    name = data.get("name")
    category = data.get("category")
    price = data.get("price")

    if not name or not category or price is None:
        return jsonify({"error": "Nome, categoria e preço são obrigatórios."}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE services SET name = %s, category = %s, price = %s WHERE id_service = %s",
                (name, category, float(price), service_id),
            )
            updated = cur.rowcount

    conn.close()
    if updated == 0:
        return jsonify({"error": "Serviço não encontrado."}), 404
    return jsonify({"message": "Serviço atualizado com sucesso"}), 200


@app.route("/services/<int:service_id>", methods=["DELETE"])
@handle_errors
@role_required("admin")
def delete_service(service_id):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM services WHERE id_service = %s", (service_id,))
            deleted = cur.rowcount

    conn.close()
    if deleted == 0:
        return jsonify({"error": "Serviço não encontrado."}), 404
    return jsonify({"message": "Serviço apagado com sucesso"}), 200


# -----------------------
# Costureiras
# -----------------------
@app.route("/seamstresses", methods=["GET"])
@handle_errors
def get_seamstresses():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id_seamstress, name FROM seamstresses ORDER BY name")
            rows = cur.fetchall()

    conn.close()
    return jsonify([{"id": r["id_seamstress"], "name": r["name"]} for r in rows]), 200


@app.route("/seamstresses", methods=["POST"])
@handle_errors
@role_required("admin")
def add_seamstress():
    data = request.json or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "O nome é obrigatório."}), 400

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
    return jsonify({"message": "Costureira adicionada com sucesso", "id": new_id}), 201


@app.route("/seamstresses/<int:seamstress_id>", methods=["PUT"])
@handle_errors
@role_required("admin")
def update_seamstress(seamstress_id):
    data = request.json or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "O nome é obrigatório."}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE seamstresses SET name = %s WHERE id_seamstress = %s",
                (name, seamstress_id),
            )
            updated = cur.rowcount

    conn.close()
    if updated == 0:
        return jsonify({"error": "Costureira não encontrada."}), 404
    return jsonify({"message": "Costureira atualizada com sucesso"}), 200


@app.route("/seamstresses/<int:seamstress_id>", methods=["DELETE"])
@handle_errors
@role_required("admin")
def delete_seamstress(seamstress_id):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seamstresses WHERE id_seamstress = %s", (seamstress_id,))
            deleted = cur.rowcount

    conn.close()
    if deleted == 0:
        return jsonify({"error": "Costureira não encontrada."}), 404
    return jsonify({"message": "Costureira apagada com sucesso"}), 200


# -----------------------
# Pedidos
# -----------------------
@app.route("/pedidos", methods=["POST"])
@handle_errors
def add_pedido():
    data = request.json or {}
    delivery_date = data.get("deliveryDate")
    if not delivery_date:
        return jsonify({"error": "A data de entrega prevista é obrigatória."}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pedidos (id_client, data_prevista, observacoes, desconto, preco_total, status, data_entrada)
                VALUES (%s, %s, %s, %s, %s, 'Pendente', NOW())
                RETURNING id_pedido
                """,
                (
                    data.get("clientId"),
                    delivery_date,
                    data.get("comments", ""),
                    data.get("discount", 0),
                    data.get("totalPrice", 0),
                ),
            )
            pedido_id = cur.fetchone()["id_pedido"]

            for service in data.get("services", []):
                cur.execute(
                    """
                    INSERT INTO pedido_servicos (id_pedido, id_service, quantity, description, status)
                    VALUES (%s, %s, %s, %s, 'Pendente')
                    """,
                    (
                        pedido_id,
                        service.get("id"),
                        service.get("quantity", 1),
                        service.get("description", ""),
                    ),
                )

    conn.close()
    return jsonify({"message": "Pedido criado com sucesso", "pedido_id": pedido_id}), 201


@app.route("/pedidos/pendentes", methods=["GET"])
@handle_errors
def get_pending_services_by_date():
    selected_date_str = request.args.get("date")
    search_term = request.args.get("search")

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    base_query = """
        SELECT
            ps.id_pedido_servico,
            ps.id_pedido,
            ps.quantity,
            ps.description,
            ps.status,
            s.name as service_name,
            c.name as client_name,
            p.data_prevista,
            ss.name as costureira_conclusao,
            ps.data_conclusao
        FROM pedido_servicos ps
        JOIN pedidos p ON ps.id_pedido = p.id_pedido
        JOIN services s ON ps.id_service = s.id_service
        JOIN clients c ON p.id_client = c.id_client
        LEFT JOIN seamstresses ss ON ps.id_seamstress_conclusao = ss.id_seamstress
    """

    params = []
    where_clauses = []

    # Busca por nome do cliente ou id do pedido
    if search_term:
        where_clauses.append("(c.name ILIKE %s OR CAST(p.id_pedido AS TEXT) ILIKE %s)")
        params.extend([f"%{search_term}%", f"%{search_term}%"])
    else:
        where_clauses.append("ps.status = 'Pendente'")

    if selected_date_str and not search_term:
        where_clauses.append("p.data_prevista = %s")
        params.append(selected_date_str)

    if where_clauses:
        base_query += " WHERE " + " AND ".join(where_clauses)

    base_query += " ORDER BY p.data_prevista, p.id_pedido"

    with conn:
        with conn.cursor() as cur:
            cur.execute(base_query, tuple(params))
            rows = cur.fetchall()

    conn.close()

    # Normaliza datas para string PT
    services = []
    for r in rows:
        item = dict(r)
        if item.get("data_prevista"):
            try:
                item["data_prevista"] = item["data_prevista"].strftime("%d/%m/%Y")
            except Exception:
                pass
        if item.get("data_conclusao"):
            try:
                item["data_conclusao"] = item["data_conclusao"].strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass
        services.append(item)

    if search_term:
        return jsonify(services)

    today = datetime.now().date()
    categorized = {"hoje": [], "atrasados": [], "proximos": []}

    for s in services:
        try:
            if s.get("data_prevista"):
                d = datetime.strptime(s["data_prevista"], "%d/%m/%Y").date()
                if d == today:
                    categorized["hoje"].append(s)
                elif d < today:
                    categorized["atrasados"].append(s)
                else:
                    categorized["proximos"].append(s)
        except Exception:
            # ignora erro de parsing
            pass

    return jsonify(categorized)


@app.route("/pedidos/stats", methods=["GET"])
@handle_errors
def get_daily_stats():
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
                WHERE ps.status = 'Pendente' AND p.data_prevista = %s
                """,
                (today,),
            )
            pending_today = cur.fetchone()["pending_today"] or 0

            cur.execute(
                """
                SELECT COUNT(id_pedido_servico) AS completed_today
                FROM pedido_servicos
                WHERE status = 'Concluído' AND DATE(data_conclusao) = %s
                """,
                (today,),
            )
            completed_today = cur.fetchone()["completed_today"] or 0

    conn.close()
    return jsonify({"pending_today": pending_today, "completed_today": completed_today})


@app.route("/pedidos/servico/<int:pedido_servico_id>/concluir", methods=["PUT"])
@handle_errors
def complete_service_item(pedido_servico_id):
    data = request.json or {}
    seamstress_id = data.get("id_seamstress")
    if not seamstress_id:
        return jsonify({"error": "ID da costureira é obrigatório."}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pedido_servicos
                SET status = 'Concluído',
                    id_seamstress_conclusao = %s,
                    data_conclusao = NOW()
                WHERE id_pedido_servico = %s
                """,
                (seamstress_id, pedido_servico_id),
            )
            updated = cur.rowcount

    conn.close()
    if updated == 0:
        return jsonify({"error": "Item de serviço não encontrado."}), 404
    return jsonify({"message": "Serviço marcado como concluído."}), 200


# -----------------------
# ADMIN Dashboard Stats (NOVO)
# -----------------------
@app.route("/admin/stats", methods=["GET"])
@handle_errors
@role_required("admin")
def admin_stats():
    """
    Retorna indicadores para o dashboard admin.
    Usa a tabela pedidos (status, data_prevista, data_entrada, preco_total).
    """
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    hoje = date.today()
    primeiro_dia_mes = hoje.replace(day=1)

    with conn:
        with conn.cursor() as cur:
            # Pendentes hoje (por data_prevista = hoje)
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM pedidos
                WHERE status = 'Pendente' AND data_prevista = %s
                """,
                (hoje,),
            )
            pendentes_hoje = cur.fetchone()["total"] or 0

            # Concluídos hoje (pela data_conclusao nos itens)
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM pedido_servicos
                WHERE status = 'Concluído' AND DATE(data_conclusao) = %s
                """,
                (hoje,),
            )
            concluidos_hoje = cur.fetchone()["total"] or 0

            # Atrasados: pendentes com data_prevista < hoje
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM pedidos
                WHERE status = 'Pendente' AND data_prevista < %s
                """,
                (hoje,),
            )
            atrasados = cur.fetchone()["total"] or 0

            # Faturamento hoje: soma preco_total do dia (data_entrada)
            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total), 0) AS total
                FROM pedidos
                WHERE DATE(data_entrada) = %s
                """,
                (hoje,),
            )
            faturamento_hoje = float(cur.fetchone()["total"] or 0)

            # Faturamento mês: soma preco_total desde primeiro dia do mês
            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total), 0) AS total
                FROM pedidos
                WHERE data_entrada >= %s
                """,
                (primeiro_dia_mes,),
            )
            faturamento_mes = float(cur.fetchone()["total"] or 0)

    conn.close()
    return jsonify(
        {
            "pendentes_hoje": int(pendentes_hoje),
            "concluidos_hoje": int(concluidos_hoje),
            "atrasados": int(atrasados),
            "faturamento_hoje": faturamento_hoje,
            "faturamento_mes": faturamento_mes,
        }
    ), 200


if __name__ == "__main__":
    # Local dev
    port = int(os.environ.get("PORT", "7000"))
    app.run(host="0.0.0.0", port=port, debug=True)
