import os
from datetime import date, datetime
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import psycopg
from psycopg.rows import dict_row


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")

    # ====== CONFIG ======
    app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME_SECRET_KEY")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL não configurada nas variáveis de ambiente.")

    # ====== USERS / ROLES ======
    def _env(name: str, default: str) -> str:
        return os.environ.get(name, default)

    USERS = {
        _env("ADMIN_USER", "admin"): {
            "password_hash": generate_password_hash(_env("ADMIN_PASS", "admin123")),
            "role": "admin",
        },
        _env("CAIXA_USER", "caixa"): {
            "password_hash": generate_password_hash(_env("CAIXA_PASS", "caixa123")),
            "role": "caixa",
        },
        _env("COSTUREIRA_USER", "costureira"): {
            "password_hash": generate_password_hash(_env("COSTUREIRA_PASS", "costureira123")),
            "role": "costureira",
        },
    }

    # ====== DB HELPERS ======
    def get_db_connection():
        return psycopg.connect(database_url, row_factory=dict_row)

    def is_api_request() -> bool:
        return (
            request.path.startswith("/clients")
            or request.path.startswith("/services")
            or request.path.startswith("/seamstresses")
            or request.path.startswith("/pedidos")
            or request.path.startswith("/db-test")
            or request.path.startswith("/health")
            or request.path.startswith("/users")
        )

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                if is_api_request():
                    return jsonify({"error": "Não autenticado"}), 401
                return redirect(url_for("login", next=request.path))
            return fn(*args, **kwargs)

        return wrapper

    def roles_required(*roles):
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                if not session.get("user"):
                    if is_api_request():
                        return jsonify({"error": "Não autenticado"}), 401
                    return redirect(url_for("login", next=request.path))

                user_role = session.get("role")
                if user_role not in roles:
                    if is_api_request():
                        return jsonify({"error": "Sem permissão"}), 403
                    return render_template("unauthorized.html"), 403
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    def handle_errors(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except psycopg.Error as db_err:
                print(f"[DB ERROR] {request.path}: {db_err}")
                return jsonify({"error": f"Erro de banco de dados: {db_err}"}), 500
            except Exception as e:
                print(f"[ERROR] {request.path}: {e}")
                return jsonify({"error": f"Ocorreu um erro inesperado: {e}"}), 500

        return wrapper

    # ====== AUTH ROUTES ======
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if session.get("user"):
                return redirect_by_role()
            return render_template("login.html", next=request.args.get("next", "/"))

        data_user = request.form.get("username", "").strip()
        data_pass = request.form.get("password", "").strip()
        next_url = request.form.get("next", "/")

        user = USERS.get(data_user)
        if not user or not check_password_hash(user["password_hash"], data_pass):
            return render_template("login.html", next=next_url, error="Usuário ou senha inválidos.")

        session["user"] = data_user
        session["role"] = user["role"]

        return redirect_by_role(preferred_next=next_url)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def redirect_by_role(preferred_next: str | None = None):
        role = session.get("role")

        # admin -> manda pro /admin (menu)
        if role == "admin":
            # se tentou abrir algo específico, e for rota interna, pode respeitar
            # mas por padrão vai pro menu
            if preferred_next and preferred_next.startswith("/") and preferred_next != "/":
                return redirect(preferred_next)
            return redirect(url_for("admin_home"))

        # caixa -> sempre "/"
        if role == "caixa":
            return redirect(url_for("serve_main_page"))

        # costureira -> sempre "/costureiras"
        if role == "costureira":
            return redirect(url_for("serve_seamstress_page"))

        return redirect(url_for("serve_main_page"))

    # ====== PAGES ======
    @app.route("/")
    @login_required
    @roles_required("admin", "caixa")
    def serve_main_page():
        return render_template("atendimento.html")

    @app.route("/admin")
    @login_required
    @roles_required("admin")
    def admin_home():
        # menu do admin
        return render_template("admin.html")

    @app.route("/gerenciamento")
    @login_required
    @roles_required("admin")
    def serve_management_page():
        return render_template("Gerenciamento.html")

    @app.route("/costureiras")
    @login_required
    @roles_required("admin", "costureira")
    def serve_seamstress_page():
        return render_template("costureiras.html")

    @app.route("/unauthorized")
    def unauthorized_page():
        return render_template("unauthorized.html"), 403

    # ====== HEALTH / DB TEST ======
    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.route("/db-test")
    def db_test():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 as ok;")
                    row = cur.fetchone()
            return jsonify({"db": "ok", "result": row}), 200
        except Exception as e:
            return jsonify({"db": "fail", "error": str(e)}), 500

    # ====== CLIENTS ======
    @app.route("/clients/<phone>", methods=["GET"])
    @login_required
    @roles_required("admin", "caixa")
    @handle_errors
    def get_client_by_phone(phone):
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id_client, name, nif FROM clients WHERE phone = %s",
                    (phone,),
                )
                client = cur.fetchone()

        if client:
            return jsonify({"id": client["id_client"], "name": client["name"], "nif": client.get("nif")}), 200
        return jsonify({"error": "Cliente não encontrado"}), 404

    @app.route("/clients", methods=["POST"])
    @login_required
    @roles_required("admin", "caixa")
    @handle_errors
    def add_client():
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        phone = (data.get("phone") or "").strip()
        nif = (data.get("nif") or "").strip() or None

        if not name or not phone:
            return jsonify({"error": "Nome e telefone são obrigatórios."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clients (name, phone, nif) VALUES (%s, %s, %s) RETURNING id_client",
                    (name, phone, nif),
                )
                new_id = cur.fetchone()["id_client"]
            conn.commit()

        return jsonify({"message": "Cliente cadastrado com sucesso", "id": new_id}), 201

    @app.route("/clients/<int:client_id>/nif", methods=["PUT"])
    @login_required
    @roles_required("admin", "caixa")
    @handle_errors
    def update_client_nif(client_id: int):
        data = request.get_json(force=True)
        nif = (data.get("nif") or "").strip()
        if not nif:
            return jsonify({"error": "O NIF é obrigatório."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE clients SET nif=%s WHERE id_client=%s", (nif, client_id))
                if cur.rowcount == 0:
                    return jsonify({"error": "Cliente não encontrado."}), 404
            conn.commit()

        return jsonify({"message": "NIF atualizado com sucesso"}), 200

    # ====== SERVICES ======
    @app.route("/services", methods=["GET"])
    @login_required
    @roles_required("admin", "caixa", "costureira")
    @handle_errors
    def get_services_grouped():
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id_service, name, price, category FROM services ORDER BY category, name")
                rows = cur.fetchall()

        grouped: dict[str, list[dict]] = {}
        for r in rows:
            cat = r["category"]
            grouped.setdefault(cat, []).append(
                {"id": r["id_service"], "nome": r["name"], "preco": float(r["price"])}
            )
        return jsonify(grouped), 200

    @app.route("/services", methods=["POST"])
    @login_required
    @roles_required("admin")
    @handle_errors
    def create_service():
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        category = (data.get("category") or "").strip()
        price = data.get("price")

        if not name or not category or price is None:
            return jsonify({"error": "name, category e price são obrigatórios."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO services (name, category, price) VALUES (%s, %s, %s) RETURNING id_service",
                    (name, category, price),
                )
                new_id = cur.fetchone()["id_service"]
            conn.commit()

        return jsonify({"message": "Serviço criado com sucesso", "id": new_id}), 201

    @app.route("/services/<int:service_id>", methods=["PUT"])
    @login_required
    @roles_required("admin")
    @handle_errors
    def update_service(service_id: int):
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        category = (data.get("category") or "").strip()
        price = data.get("price")

        if not name or not category or price is None:
            return jsonify({"error": "name, category e price são obrigatórios."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE services SET name=%s, category=%s, price=%s WHERE id_service=%s",
                    (name, category, price, service_id),
                )
                if cur.rowcount == 0:
                    return jsonify({"error": "Serviço não encontrado."}), 404
            conn.commit()

        return jsonify({"message": "Serviço atualizado com sucesso"}), 200

    @app.route("/services/<int:service_id>", methods=["DELETE"])
    @login_required
    @roles_required("admin")
    @handle_errors
    def delete_service(service_id: int):
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM services WHERE id_service=%s", (service_id,))
                if cur.rowcount == 0:
                    return jsonify({"error": "Serviço não encontrado."}), 404
            conn.commit()
        return jsonify({"message": "Serviço apagado com sucesso"}), 200

    # ====== SEAMSTRESSES ======
    @app.route("/seamstresses", methods=["GET"])
    @login_required
    @roles_required("admin", "costureira")
    @handle_errors
    def list_seamstresses():
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id_seamstress, name FROM seamstresses ORDER BY name")
                rows = cur.fetchall()
        return jsonify([{"id": r["id_seamstress"], "name": r["name"]} for r in rows]), 200

    @app.route("/seamstresses", methods=["POST"])
    @login_required
    @roles_required("admin")
    @handle_errors
    def add_seamstress():
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "O nome é obrigatório."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO seamstresses (name) VALUES (%s) RETURNING id_seamstress",
                    (name,),
                )
                new_id = cur.fetchone()["id_seamstress"]
            conn.commit()
        return jsonify({"message": "Costureira cadastrada com sucesso", "id": new_id}), 201

    @app.route("/seamstresses/<int:seamstress_id>", methods=["PUT"])
    @login_required
    @roles_required("admin")
    @handle_errors
    def update_seamstress(seamstress_id: int):
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "O nome é obrigatório."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE seamstresses SET name=%s WHERE id_seamstress=%s", (name, seamstress_id))
                if cur.rowcount == 0:
                    return jsonify({"error": "Costureira não encontrada."}), 404
            conn.commit()

        return jsonify({"message": "Costureira atualizada com sucesso"}), 200

    @app.route("/seamstresses/<int:seamstress_id>", methods=["DELETE"])
    @login_required
    @roles_required("admin")
    @handle_errors
    def delete_seamstress(seamstress_id: int):
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM seamstresses WHERE id_seamstress=%s", (seamstress_id,))
                if cur.rowcount == 0:
                    return jsonify({"error": "Costureira não encontrada."}), 404
            conn.commit()
        return jsonify({"message": "Costureira apagada com sucesso"}), 200

    # ====== PEDIDOS ======
    @app.route("/pedidos", methods=["POST"])
    @login_required
    @roles_required("admin", "caixa")
    @handle_errors
    def add_pedido():
        data = request.get_json(force=True)
        delivery_date = data.get("deliveryDate")
        if not delivery_date:
            return jsonify({"error": "A data de entrega prevista é obrigatória."}), 400

        client_id = data.get("clientId")
        total_price = data.get("totalPrice")
        discount = data.get("discount", 0)
        comments = data.get("comments", "")

        services = data.get("services", [])
        if not services:
            return jsonify({"error": "O pedido deve ter ao menos 1 serviço."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pedidos (id_client, data_prevista, observacoes, desconto, preco_total, status, data_entrada)
                    VALUES (%s, %s, %s, %s, %s, 'Pendente', NOW())
                    RETURNING id_pedido
                    """,
                    (client_id, delivery_date, comments, discount, total_price),
                )
                pedido_id = cur.fetchone()["id_pedido"]

                for s in services:
                    cur.execute(
                        """
                        INSERT INTO pedido_servicos (id_pedido, id_service, quantity, description, status)
                        VALUES (%s, %s, %s, %s, 'Pendente')
                        """,
                        (
                            pedido_id,
                            s.get("id"),
                            s.get("quantity", 1),
                            s.get("description", "") or "",
                        ),
                    )

            conn.commit()

        return jsonify({"message": "Pedido criado com sucesso", "pedido_id": pedido_id}), 201

    @app.route("/pedidos/pendentes", methods=["GET"])
    @login_required
    @roles_required("admin", "costureira")
    @handle_errors
    def get_pending_services_by_date():
        selected_date_str = request.args.get("date")
        search_term = (request.args.get("search") or "").strip()

        base_query = """
            SELECT
                ps.id_pedido_servico, ps.id_pedido, ps.quantity, ps.description, ps.status,
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

        where_clauses = []
        params = []

        if search_term:
            where_clauses.append("(c.name ILIKE %s OR CAST(p.id_pedido AS TEXT) ILIKE %s)")
            params.extend([f"%{search_term}%", f"%{search_term}%"])
        else:
            where_clauses.append("ps.status = 'Pendente'")

        if selected_date_str and not search_term:
            where_clauses.append("p.data_prevista = %s")
            params.append(selected_date_str)

        query = base_query
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += " ORDER BY p.data_prevista NULLS LAST, p.id_pedido"

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()

        services = []
        for r in rows:
            data_prevista = r["data_prevista"].strftime("%d/%m/%Y") if r.get("data_prevista") else None
            data_conclusao = r["data_conclusao"].strftime("%d/%m/%Y %H:%M") if r.get("data_conclusao") else None
            services.append(
                {
                    "id_pedido_servico": r["id_pedido_servico"],
                    "id_pedido": r["id_pedido"],
                    "quantity": r["quantity"],
                    "description": r.get("description") or "",
                    "status": r["status"],
                    "service_name": r["service_name"],
                    "client_name": r["client_name"],
                    "data_prevista": data_prevista,
                    "costureira_conclusao": r.get("costureira_conclusao"),
                    "data_conclusao": data_conclusao,
                }
            )

        if search_term or selected_date_str:
            return jsonify(services), 200

        today = datetime.now().date()
        categorized = {"hoje": [], "atrasados": [], "proximos": []}

        for s in services:
            if not s.get("data_prevista"):
                categorized["proximos"].append(s)
                continue
            try:
                d = datetime.strptime(s["data_prevista"], "%d/%m/%Y").date()
                if d == today:
                    categorized["hoje"].append(s)
                elif d < today:
                    categorized["atrasados"].append(s)
                else:
                    categorized["proximos"].append(s)
            except Exception:
                categorized["proximos"].append(s)

        return jsonify(categorized), 200

    @app.route("/pedidos/stats", methods=["GET"])
    @login_required
    @roles_required("admin", "costureira")
    @handle_errors
    def get_daily_stats():
        today_str = date.today().strftime("%Y-%m-%d")

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(ps.id_pedido_servico) AS cnt
                    FROM pedido_servicos ps
                    JOIN pedidos p ON ps.id_pedido = p.id_pedido
                    WHERE ps.status = 'Pendente' AND p.data_prevista = %s
                    """,
                    (today_str,),
                )
                pending_today = (cur.fetchone() or {}).get("cnt", 0) or 0

                cur.execute(
                    """
                    SELECT COUNT(id_pedido_servico) AS cnt
                    FROM pedido_servicos
                    WHERE status = 'Concluído' AND CAST(data_conclusao AS DATE) = %s
                    """,
                    (today_str,),
                )
                completed_today = (cur.fetchone() or {}).get("cnt", 0) or 0

        return jsonify({"pending_today": int(pending_today), "completed_today": int(completed_today)}), 200

    @app.route("/pedidos/servico/<int:pedido_servico_id>/concluir", methods=["PUT"])
    @login_required
    @roles_required("admin", "costureira")
    @handle_errors
    def complete_service_item(pedido_servico_id: int):
        data = request.get_json(force=True)
        seamstress_id = data.get("id_seamstress")
        if not seamstress_id:
            return jsonify({"error": "id_seamstress é obrigatório."}), 400

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pedido_servicos
                    SET status='Concluído',
                        id_seamstress_conclusao=%s,
                        data_conclusao=NOW()
                    WHERE id_pedido_servico=%s
                    """,
                    (seamstress_id, pedido_servico_id),
                )
                if cur.rowcount == 0:
                    return jsonify({"error": "Serviço do pedido não encontrado."}), 404
            conn.commit()

        return jsonify({"message": "Serviço concluído com sucesso"}), 200

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7000"))
    app.run(host="0.0.0.0", port=port, debug=True)
