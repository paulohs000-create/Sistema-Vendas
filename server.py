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
# PÁGINA (FRONT)
# -----------------------------------------------------------------------------
@app.get("/")
def atendimento_page():
    # Precisa existir: templates/atendimento.html
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


@app.get("/schema/tables")
def schema_tables():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name;
                    """
                )
                rows = cur.fetchall()
        return jsonify(rows)
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# CLIENTES
# -----------------------------------------------------------------------------
@app.get("/clients")
def list_clients():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id_client, name, phone, nif FROM clients ORDER BY name;")
                rows = cur.fetchall()
        return jsonify(rows)
    finally:
        conn.close()


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
    if not data.get("name") or not data.get("phone"):
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
                    (data["name"], data["phone"], data.get("nif")),
                )
                new_id = cur.fetchone()["id_client"]
        return jsonify({"id_client": new_id}), 201
    finally:
        conn.close()


@app.put("/clients/<int:client_id>/nif")
def update_client_nif(client_id: int):
    data = request.json or {}
    nif = data.get("nif")
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
# PRODUTOS
# -----------------------------------------------------------------------------
@app.get("/products")
def list_products():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id_product, name, description, sale_price, stock
                    FROM products
                    ORDER BY name;
                    """
                )
                rows = cur.fetchall()
        return jsonify(rows)
    finally:
        conn.close()


@app.post("/products")
def create_product():
    data = request.json or {}
    if not data.get("name") or data.get("sale_price") is None or data.get("stock") is None:
        return jsonify({"error": "Campos obrigatórios: name, sale_price, stock"}), 400

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO products (name, description, sale_price, stock)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id_product
                    """,
                    (data["name"], data.get("description"), data["sale_price"], data["stock"]),
                )
                new_id = cur.fetchone()["id_product"]
        return jsonify({"id_product": new_id}), 201
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# VENDAS
# -----------------------------------------------------------------------------
@app.post("/sales")
def create_sale():
    data = request.json or {}
    if data.get("id_client") is None or data.get("total_sale") is None or not isinstance(data.get("items"), list):
        return jsonify({"error": "Campos obrigatórios: id_client, total_sale, items(list)"}), 400

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sales (id_client, sale_date, total_sale)
                    VALUES (%s, %s, %s)
                    RETURNING id_sale
                    """,
                    (data["id_client"], datetime.now(), data["total_sale"]),
                )
                id_sale = cur.fetchone()["id_sale"]

                for item in data["items"]:
                    if item.get("id_product") is None or item.get("quantity") is None or item.get("unit_price") is None:
                        return jsonify({"error": "Cada item precisa: id_product, quantity, unit_price"}), 400

                    cur.execute(
                        """
                        INSERT INTO sale_items (id_sale, id_product, quantity, unit_price)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (id_sale, item["id_product"], item["quantity"], item["unit_price"]),
                    )

                    cur.execute(
                        """
                        UPDATE products
                        SET stock = stock - %s
                        WHERE id_product = %s
                        """,
                        (item["quantity"], item["id_product"]),
                    )

        return jsonify({"id_sale": id_sale}), 201
    finally:
        conn.close()


@app.get("/sales")
def list_sales():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.id_sale, s.sale_date, s.total_sale, c.name AS client_name
                    FROM sales s
                    JOIN clients c ON c.id_client = s.id_client
                    ORDER BY s.sale_date DESC;
                    """
                )
                rows = cur.fetchall()
        return jsonify(rows)
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
