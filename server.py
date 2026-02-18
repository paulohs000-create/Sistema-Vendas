import os
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não definida. Configure em Railway > Variables.")
    return psycopg2.connect(DATABASE_URL, sslmode=os.getenv("PGSSLMODE", "require"))

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.get("/db-test")
def db_test():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name;
                """)
                rows = cur.fetchall()
        return jsonify(rows)
    finally:
        conn.close()

# -------------------------
# Exemplo: listar produtos
# -------------------------
@app.get("/products")
def list_products():
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM products ORDER BY name;")
                rows = cur.fetchall()
        return jsonify(rows)
    finally:
        conn.close()

# -------------------------
# Exemplo: registrar venda
# -------------------------
@app.post("/sales")
def create_sale():
    data = request.json or {}
    if "id_client" not in data or "total_sale" not in data or "items" not in data:
        return jsonify({"error": "Campos obrigatórios: id_client, total_sale, items"}), 400

    conn = get_db_connection()
    try:
        with conn:
