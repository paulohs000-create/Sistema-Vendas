import os
from datetime import date

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não definida. Configure em Railway > Variables.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def fmt_date(d):
    if not d:
        return None
    try:
        return d.strftime("%d/%m/%Y")
    except Exception:
        return str(d)


def fmt_datetime(dt):
    if not dt:
        return None
    try:
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(dt)


# -----------------------------------------------------------------------------
# PÁGINAS
# -----------------------------------------------------------------------------
@app.get("/")
def atendimento_page():
    return render_template("atendimento.html")


@app.get("/gerenciamento")
def gerenciamento_page():
    return render_template("Gerenciamento.html")


@app.get("/costureiras")
def costureiras_page():
    return render_template("costureiras.html")


# -----------------------------------------------------------------------------
# HEALTH / DEBUG
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/db-test")
def db_test():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT now() AS server_time;")
                row = cur.fetchone()
        return jsonify(row)
    finally:
        conn.close()


@app.get("/routes")
def list_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint != "static":
            routes.append({"path": str(rule), "methods": sorted(list(rule.methods))})
    routes.sort(key=lambda x: x["path"])
    return jsonify(routes)


# -----------------------------------------------------------------------------
# CLIENTS
# -----------------------------------------------------------------------------
@app.get("/clients/<phone>")
def get_client_by_phone(phone: str):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id_client, name, phone, nif FROM clients WHERE phone = %s",
                    (phone,),
                )
                row = cur.fetchone()
        if not row:
            return jsonify({"name": None}), 404
        return jsonify(row)
    finally:
        conn.close()


@app.post("/clients")
def create_client():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    nif = (data.get("nif") or "").strip() or None

    if not name or not phone:
        return json_error("Campos obrigatórios: name, phone", 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO clients (name, phone, nif)
                    VALUES (%s, %s, %s)
                    RETURNING id_client
                    """,
                    (name, phone, nif),
                )
                new_id = cur.fetchone()["id_client"]
        return jsonify({"id_client": new_id}), 201
    finally:
        conn.close()


@app.put("/clients/<int:client_id>/nif")
def update_client_nif(client_id: int):
    data = request.json or {}
    nif = (data.get("nif") or "").strip()
    if not nif:
        return json_error("Campo obrigatório: nif", 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE clients SET nif = %s WHERE id_client = %s",
                    (nif, client_id),
                )
                if cur.rowcount == 0:
                    return json_error("Cliente não encontrado", 404)
        return jsonify({"message": "NIF atualizado com sucesso"}), 200
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# SERVICES
# - GET: retorna agrupado por categoria (para atendimento e gerenciamento)
# - POST/PUT/DELETE: usado no gerenciamento
# -----------------------------------------------------------------------------
@app.get("/services")
def get_services_grouped():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id_service, name, description, price, category
                    FROM services
                    ORDER BY category, name
                    """
                )
                rows = cur.fetchall()

        grouped = {}
        for r in rows:
            cat = r["category"] or "Geral"
            grouped.setdefault(cat, [])
            grouped[cat].append(
                {
                    "id": r["id_service"],
                    "nome": r["name"],
                    "descricao": r["description"],
                    "preco": float(r["price"]),
                    "categoria": cat,
                }
            )

        return jsonify(grouped)
    finally:
        conn.close()


@app.post("/services")
def create_service():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip() or "Geral"
    price = data.get("price")

    if not name:
        return json_error("Campo obrigatório: name", 400)
    if price in (None, ""):
        return json_error("Campo obrigatório: price", 400)

    try:
        price_val = float(price)
    except Exception:
        return json_error("price inválido", 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO services (name, category, price)
                    VALUES (%s, %s, %s)
                    RETURNING id_service
                    """,
                    (name, category, price_val),
                )
                new_id = cur.fetchone()["id_service"]
        return jsonify({"id_service": new_id}), 201
    finally:
        conn.close()


@app.put("/services/<int:service_id>")
def update_service(service_id: int):
    data = request.json or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip() or "Geral"
    price = data.get("price")

    if not name:
        return json_error("Campo obrigatório: name", 400)
    if price in (None, ""):
        return json_error("Campo obrigatório: price", 400)

    try:
        price_val = float(price)
    except Exception:
        return json_error("price inválido", 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE services
                    SET name = %s, category = %s, price = %s
                    WHERE id_service = %s
                    """,
                    (name, category, price_val, service_id),
                )
                if cur.rowcount == 0:
                    return json_error("Serviço não encontrado", 404)
        return jsonify({"message": "Serviço atualizado"}), 200
    finally:
        conn.close()


@app.delete("/services/<int:service_id>")
def delete_service(service_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("DELETE FROM services WHERE id_service = %s", (service_id,))
                except Exception as e:
                    # provavel FK (pedido_servicos)
                    return json_error("Não foi possível apagar: serviço já usado em pedidos.", 409)

                if cur.rowcount == 0:
                    return json_error("Serviço não encontrado", 404)

        return jsonify({"message": "Serviço apagado"}), 200
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# PEDIDOS (criação pelo atendimento)
# -----------------------------------------------------------------------------
@app.post("/pedidos")
def create_pedido():
    data = request.json or {}

    client_id = data.get("clientId")
    delivery_date = data.get("deliveryDate")
    comments = data.get("comments") or ""
    discount = data.get("discount") if data.get("discount") is not None else 0
    total_price = data.get("totalPrice")
    services = data.get("services") or []

    if client_id is None:
        return json_error("clientId é obrigatório", 400)
    if not delivery_date:
        return json_error("deliveryDate é obrigatório", 400)
    if total_price is None:
        return json_error("totalPrice é obrigatório", 400)
    if not isinstance(services, list) or len(services) == 0:
        return json_error("services deve ser uma lista com pelo menos 1 item", 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pedidos (id_client, data_entrada, data_prevista, observacoes, desconto, preco_total, status)
                    VALUES (%s, NOW(), %s, %s, %s, %s, 'Pendente')
                    RETURNING id_pedido
                    """,
                    (client_id, delivery_date, comments, discount, total_price),
                )
                pedido_id = cur.fetchone()["id_pedido"]

                for s in services:
                    service_id = s.get("id")
                    quantity = s.get("quantity")
                    description = s.get("description") or ""

                    if service_id is None or quantity is None:
                        return json_error("Cada serviço precisa: id, quantity", 400)

                    cur.execute(
                        """
                        INSERT INTO pedido_servicos (id_pedido, id_service, quantity, description, status)
                        VALUES (%s, %s, %s, %s, 'Pendente')
                        """,
                        (pedido_id, service_id, quantity, description),
                    )

        return jsonify({"pedido_id": pedido_id}), 201
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# SEAMSTRESSES (Costureiras) - CRUD para Gerenciamento e Painel
# -----------------------------------------------------------------------------
@app.get("/seamstresses")
def list_seamstresses():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id_seamstress, name FROM seamstresses ORDER BY name;")
                rows = cur.fetchall()
        return jsonify([{"id": r["id_seamstress"], "name": r["name"]} for r in rows])
    finally:
        conn.close()


@app.post("/seamstresses")
def create_seamstress():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return json_error("Campo obrigatório: name", 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO seamstresses (name) VALUES (%s) RETURNING id_seamstress",
                    (name,),
                )
                new_id = cur.fetchone()["id_seamstress"]
        return jsonify({"id_seamstress": new_id}), 201
    finally:
        conn.close()


@app.put("/seamstresses/<int:seamstress_id>")
def update_seamstress(seamstress_id: int):
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return json_error("Campo obrigatório: name", 400)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE seamstresses SET name = %s WHERE id_seamstress = %s",
                    (name, seamstress_id),
                )
                if cur.rowcount == 0:
                    return json_error("Costureira não encontrada", 404)
        return jsonify({"message": "Costureira atualizada"}), 200
    finally:
        conn.close()


@app.delete("/seamstresses/<int:seamstress_id>")
def delete_seamstress(seamstress_id: int):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("DELETE FROM seamstresses WHERE id_seamstress = %s", (seamstress_id,))
                except Exception:
                    return json_error("Não foi possível apagar: costureira já usada em serviços/pedidos.", 409)

                if cur.rowcount == 0:
                    return json_error("Costureira não encontrada", 404)

        return jsonify({"message": "Costureira apagada"}), 200
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# API DO PAINEL DAS COSTUREIRAS
# -----------------------------------------------------------------------------
@app.get("/pedidos/stats")
def pedidos_stats():
    conn = get_db_connection()
    today = date.today()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS pending_today
                    FROM pedido_servicos ps
                    JOIN pedidos p ON p.id_pedido = ps.id_pedido
                    WHERE p.data_prevista = %s
                      AND COALESCE(ps.status, '') <> 'Concluído'
                    """,
                    (today,),
                )
                pending_today = cur.fetchone()["pending_today"]

                cur.execute(
                    """
                    SELECT COUNT(*) AS completed_today
                    FROM pedido_servicos ps
                    WHERE ps.status = 'Concluído'
                      AND ps.data_conclusao::date = %s
                    """,
                    (today,),
                )
                completed_today = cur.fetchone()["completed_today"]

        return jsonify({"pending_today": pending_today, "completed_today": completed_today})
    finally:
        conn.close()


def _map_service_row(r):
    return {
        "id_pedido_servico": r["id_pedido_servico"],
        "id_pedido": r["id_pedido"],
        "service_name": r["service_name"],
        "client_name": r["client_name"],
        "quantity": r["quantity"],
        "data_prevista": fmt_date(r["data_prevista"]),
        "status": r["status"],
        "costureira_conclusao": r["costureira_conclusao"],
        "data_conclusao": fmt_datetime(r["data_conclusao"]),
    }


@app.get("/pedidos/pendentes")
def pedidos_pendentes():
    selected_date = (request.args.get("date") or "").strip()
    search = (request.args.get("search") or "").strip()

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                base_sql = """
                    SELECT
                        ps.id_pedido_servico,
                        ps.id_pedido,
                        ps.quantity,
                        ps.status,
                        ps.data_conclusao,
                        s.name AS service_name,
                        c.name AS client_name,
                        p.data_prevista,
                        ss.name AS costureira_conclusao
                    FROM pedido_servicos ps
                    JOIN pedidos p ON p.id_pedido = ps.id_pedido
                    JOIN clients c ON c.id_client = p.id_client
                    JOIN services s ON s.id_service = ps.id_service
                    LEFT JOIN seamstresses ss ON ss.id_seamstress = ps.id_seamstress_conclusao
                """

                if search:
                    if search.isdigit():
                        cur.execute(
                            base_sql + """
                            WHERE (ps.id_pedido::text = %s OR c.name ILIKE %s)
                            ORDER BY p.data_prevista ASC, ps.id_pedido DESC
                            LIMIT 200
                            """,
                            (search, f"%{search}%"),
                        )
                    else:
                        cur.execute(
                            base_sql + """
                            WHERE c.name ILIKE %s
                            ORDER BY p.data_prevista ASC, ps.id_pedido DESC
                            LIMIT 200
                            """,
                            (f"%{search}%",),
                        )
                    rows = cur.fetchall()
                    return jsonify([_map_service_row(r) for r in rows])

                if selected_date:
                    cur.execute(
                        base_sql + """
                        WHERE p.data_prevista = %s
                        ORDER BY p.data_prevista ASC, ps.id_pedido DESC
                        LIMIT 500
                        """,
                        (selected_date,),
                    )
                    rows = cur.fetchall()
                    return jsonify([_map_service_row(r) for r in rows])

                today = date.today()

                cur.execute(
                    base_sql + """
                    WHERE p.data_prevista < %s
                      AND COALESCE(ps.status, '') <> 'Concluído'
                    ORDER BY p.data_prevista ASC, ps.id_pedido DESC
                    LIMIT 500
                    """,
                    (today,),
                )
                atrasados = [_map_service_row(r) for r in cur.fetchall()]

                cur.execute(
                    base_sql + """
                    WHERE p.data_prevista = %s
                      AND COALESCE(ps.status, '') <> 'Concluído'
                    ORDER BY ps.id_pedido DESC
                    LIMIT 500
                    """,
                    (today,),
                )
                hoje = [_map_service_row(r) for r in cur.fetchall()]

                cur.execute(
                    base_sql + """
                    WHERE p.data_prevista > %s
                      AND COALESCE(ps.status, '') <> 'Concluído'
                    ORDER BY p.data_prevista ASC, ps.id_pedido DESC
                    LIMIT 500
                    """,
                    (today,),
                )
                proximos = [_map_service_row(r) for r in cur.fetchall()]

                return jsonify({"atrasados": atrasados, "hoje": hoje, "proximos": proximos})
    finally:
        conn.close()


@app.put("/pedidos/servico/<int:pedido_servico_id>/concluir")
def concluir_pedido_servico(pedido_servico_id: int):
    data = request.json or {}
    id_seamstress = data.get("id_seamstress")

    if not id_seamstress:
        return json_error("id_seamstress é obrigatório", 400)

    conn = get_db_connection()
    try:
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
                    (id_seamstress, pedido_servico_id),
                )
                if cur.rowcount == 0:
                    return json_error("Serviço do pedido não encontrado", 404)
        return jsonify({"message": "Serviço concluído com sucesso"}), 200
    finally:
        conn.close()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
