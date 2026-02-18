# Servidor Python usando Flask para um Sistema de Loja de Costura.
# Migrado de SQL Server (pyodbc) para PostgreSQL (psycopg2) com DATABASE_URL (Railway).

import os
from datetime import date, datetime
from functools import wraps

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS


app = Flask(__name__)
CORS(app)

# -----------------------------------------------------------------------------
# DB (PostgreSQL / Railway)
# -----------------------------------------------------------------------------
# Railway normalmente injeta DATABASE_URL no ambiente.
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # fallback opcional para dev local (se quiser)
    # Ex: export DATABASE_URL="postgresql://user:pass@localhost:5432/loja"
    print("AVISO: DATABASE_URL não encontrado no ambiente.")


def get_db_connection():
    """
    Abre uma nova conexão usando DATABASE_URL.
    Em Railway, normalmente precisa sslmode=require.
    """
    try:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL não definido. Configure no Railway (Variables).")
        conn = psycopg2.connect(DATABASE_URL, sslmode=os.getenv("PGSSLMODE", "require"))
        return conn
    except Exception as ex:
        print(f"Erro de conexão com o banco de dados: {ex}")
        return None


def handle_errors(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except psycopg2.Error as db_err:
            # Erros específicos do banco
            print(f"ERRO DE BANCO DE DADOS na rota {request.path}: {db_err}")
            return jsonify({"error": f"Erro de banco de dados: {str(db_err)}"}), 500
        except Exception as e:
            print(f"ERRO INESPERADO na rota {request.path}: {e}")
            return jsonify({"error": f"Ocorreu um erro inesperado: {str(e)}"}), 500
    return decorated_function


# -----------------------------------------------------------------------------
# Rotas para Servir as Páginas HTML
# IMPORTANTE: no Linux (Railway) o nome do arquivo é case-sensitive.
# Seu repo tem "Gerenciamento.html" (G maiúsculo) :contentReference[oaicite:1]{index=1}
# então render_template precisa bater com o nome real.
# -----------------------------------------------------------------------------
@app.route("/")
@handle_errors
def serve_main_page():
    return render_template("atendimento.html")


@app.route("/gerenciamento")
@handle_errors
def serve_management_page():
    # Se você preferir, renomeie o arquivo para "gerenciamento.html"
    # e altere aqui também. Por enquanto, batendo no nome real do arquivo.
    return render_template("Gerenciamento.html")


@app.route("/costureiras")
@handle_errors
def serve_seamstress_page():
    return render_template("costureiras.html")


# -----------------------------------------------------------------------------
# Clientes
# -----------------------------------------------------------------------------
@app.route("/clients/<phone>", methods=["GET"])
@handle_errors
def get_client_by_phone(phone):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Erro de conexão"}), 500

    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT id_client, name, nif FROM clients WHERE phone = %s",
                (phone,),
            )
            client = cursor.fetchone()

    conn.close()

    if client:
        return jsonify({"id": client["id_client"], "name": client["name"], "nif": client["nif"]}), 200
    return jsonify({"name": None}), 404


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
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO clients (name, phone, nif) VALUES (%s, %s, %s) RETURNING id_client",
                (name, phone, nif),
            )
            new_id = cursor.fetchone()[0]

    conn.close()
    return jsonify({"message": "Cliente cadastrado com sucesso", "id": new_id}), 201


@app.route("/clients/<int:client_id>/nif", methods=["PUT"])
@handle_errors
def update_client_nif(client_id):
    data = request.json or {}
    nif = data.get("nif")
    if not nif:
        return jsonify({"error": "O NIF é obrigat
