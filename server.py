import os
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não definida. Configure em Railway > Variables.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# -----------------------------------------------------------------------------
# FRONT
# -----------------------------------------------------------------------------
@app.get("/")
def atendimento_page():
    return render_template("atendimento.html")


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
        return jsonify({"error": "Campos obrigatórios: name, phone"}), 400

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
        return jsonify({"error": "Campo obrigatório: nif"}), 400

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE clients SET nif = %s WHERE id_client = %s",
                    (nif, client_id),
                )
                if cur.rowcount == 0:
                    return jsonify({"error": "Cliente não encontrado"}), 404
        return jsonify({"message": "NIF atualizado com sucesso"}), 200
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# SERVICES (para o atendimento.html)
# Retorna no formato: { "Categoria": [ {id, nome, preco}, ... ] }
# -----------------------------------------------------------------------------
@app.get("/services")
def get_services_grouped():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id_service, name, price, category
                    FROM services
                    ORDER BY category, name
                    """
                )
                rows = cur.fetchall()

        grouped = {}
        for r in rows:
            cat = r["category"]
            grouped.setdefault(cat, [])
            grouped[cat].append(
                {
                    "id": r["id_service"],
                    "nome": r["name"],
                    "preco": float(r["price"]),
                }
            )

        return jsonify(grouped)
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# PEDIDOS (para o atendimento.html)
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
        return jsonify({"error": "clientId é obrigatório"}), 400
    if not delivery_date:
        return jsonify({"error": "deliveryDate é obrigatório"}), 400
    if total_price is None:
        return jsonify({"error": "totalPrice é obrigatório"}), 400
    if not isinstance(services, list) or len(services) == 0:
        return jsonify({"error": "services deve ser uma lista com pelo menos 1 item"}), 400

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # cria pedido
                cur.execute(
                    """
                    INSERT INTO pedidos (id_client, data_entrada, data_prevista, observacoes, desconto, preco_total, status)
                    VALUES (%s, NOW(), %s, %s, %s, %s, 'Pendente')
                    RETURNING id_pedido
                    """,
                    (client_id, delivery_date, comments, discount, total_price),
                )
                pedido_id = cur.fetchone()["id_pedido"]

                # itens do pedido
                for s in services:
                    service_id = s.get("id")
                    quantity = s.get("quantity")
                    description = s.get("description") or ""

                    if service_id is None or quantity is None:
                        return jsonify({"error": "Cada serviço precisa: id, quantity"}), 400

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
# MAIN
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
