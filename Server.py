import os
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não definida no Railway.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

# =========================
# HEALTH CHECK
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

# =========================
# CLIENTES
# =========================
@app.get("/clients")
def list_clients():
    conn = get_db_connection()
    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM clients ORDER BY name;")
            rows = cur.fetchall()
    conn.close()
    return jsonify(rows)

@app.post("/clients")
def create_client():
    data = request.json
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO clients (name, phone, nif)
                VALUES (%s, %s, %s)
                RETURNING id_client
            """, (data["name"], data["phone"], data.get("nif")))
            new_id = cur.fetchone()[0]
    conn.close()
    return {"id_client": new_id}

# =========================
# PRODUTOS
# =========================
@app.get("/products")
def list_products():
    conn = get_db_connection()
    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM products ORDER BY name;")
            rows = cur.fetchall()
    conn.close()
    return jsonify(rows)

@app.post("/products")
def create_product():
    data = request.json
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO products (name, description, sale_price, stock)
                VALUES (%s, %s, %s, %s)
                RETURNING id_product
            """, (
                data["name"],
                data.get("description"),
                data["sale_price"],
                data["stock"]
            ))
            new_id = cur.fetchone()[0]
    conn.close()
    return {"id_product": new_id}

# =========================
# REGISTRAR VENDA
# =========================
@app.post("/sales")
def create_sale():
    data = request.json
    conn = get_db_connection()

    with conn:
        with conn.cursor() as cur:
            # cria venda
            cur.execute("""
                INSERT INTO sales (id_client, sale_date, total_sale)
                VALUES (%s, %s, %s)
                RETURNING id_sale
            """, (
                data["id_client"],
                datetime.now(),
                data["total_sale"]
            ))
            id_sale = cur.fetchone()[0]

            # insere itens
            for item in data["items"]:
                cur.execute("""
                    INSERT INTO sale_items (id_sale, id_product, quantity, unit_price)
                    VALUES (%s, %s, %s, %s)
                """, (
                    id_sale,
                    item["id_product"],
                    item["quantity"],
                    item["unit_price"]
                ))

                # baixa estoque
                cur.execute("""
                    UPDATE products
                    SET stock = stock - %s
                    WHERE id_product = %s
                """, (
                    item["quantity"],
                    item["id_product"]
                ))

    conn.close()
    return {"id_sale": id_sale}

# =========================
# LISTAR VENDAS
# =========================
@app.get("/sales")
def list_sales():
    conn = get_db_connection()
    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.id_sale, s.sale_date, s.total_sale, c.name as client_name
                FROM sales s
                JOIN clients c ON s.id_client = c.id_client
                ORDER BY s.sale_date DESC;
            """)
            rows = cur.fetchall()
    conn.close()
    return jsonify(rows)

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
