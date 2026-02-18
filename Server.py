# Servidor Python usando Flask para um Sistema de Loja de Costura.
# Expõe uma API para gerenciar clientes, serviços e pedidos.

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import pyodbc
from datetime import date, datetime
from functools import wraps

app = Flask(__name__)
CORS(app)

# --- Parâmetros de Conexão com o SQL Server ---
server = 'srv-ad'
database = 'loja'
username = 'sa'
password = 'Landesk1!'
cnxn_str = f'DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};DATABASE={database};UID={username};PWD={password}'

def get_db_connection():
    try:
        conn = pyodbc.connect(cnxn_str)
        return conn
    except pyodbc.Error as ex:
        print(f"Erro de conexão com o banco de dados: {ex}")
        return None

def handle_errors(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except pyodbc.Error as db_err:
            print(f"ERRO DE BANCO DE DADOS na rota {request.path}: {db_err}")
            return jsonify({"error": f"Erro de banco de dados: {db_err}"}), 500
        except Exception as e:
            print(f"ERRO INESPERADO na rota {request.path}: {e}")
            return jsonify({"error": f"Ocorreu um erro inesperado: {e}"}), 500
    return decorated_function

# --- Rotas para Servir as Páginas HTML ---
@app.route('/')
@handle_errors
def serve_main_page():
    return render_template('atendimento.html')

@app.route('/gerenciamento')
@handle_errors
def serve_management_page():
    return render_template('gerenciamento.html')

@app.route('/costureiras')
@handle_errors
def serve_seamstress_page():
    return render_template('costureiras.html')

# --- Rotas para Clientes ---
@app.route('/clients/<phone>', methods=['GET'])
@handle_errors
def get_client_by_phone(phone):
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    cursor.execute("SELECT id_client, name, nif FROM clients WHERE phone=?", phone)
    client = cursor.fetchone()
    conn.close()
    if client:
        return jsonify({"id": client.id_client, "name": client.name, "nif": client.nif}), 200
    else:
        return jsonify({"name": None}), 404

@app.route('/clients', methods=['POST'])
@handle_errors
def add_client():
    data = request.json
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    cursor.execute("INSERT INTO clients (name, phone, nif) OUTPUT INSERTED.id_client VALUES (?, ?, ?)", data['name'], data['phone'], data.get('nif'))
    new_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({"message": "Cliente cadastrado com sucesso", "id": new_id}), 201

@app.route('/clients/<int:client_id>/nif', methods=['PUT'])
@handle_errors
def update_client_nif(client_id):
    data = request.json
    nif = data.get('nif')
    if not nif:
        return jsonify({"error": "O NIF é obrigatório."}), 400
    
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    
    cursor = conn.cursor()
    cursor.execute("UPDATE clients SET nif = ? WHERE id_client = ?", nif, client_id)
    conn.commit()
    conn.close()
    
    if cursor.rowcount == 0:
        return jsonify({"error": "Cliente não encontrado."}), 404
        
    return jsonify({"message": "NIF do cliente atualizado com sucesso."}), 200

# --- Rotas para Serviços (CRUD) ---
@app.route('/services', methods=['GET'])
@handle_errors
def get_services():
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    cursor.execute("SELECT id_service, name, price, category FROM services ORDER BY category, name")
    services_list = cursor.fetchall()
    conn.close()
    services_by_category = {}
    for id_service, name, price, category in services_list:
        if category not in services_by_category:
            services_by_category[category] = []
        services_by_category[category].append({"id": id_service, "nome": name, "preco": float(price)})
    return jsonify(services_by_category), 200

@app.route('/services', methods=['POST'])
@handle_errors
def add_service():
    data = request.json
    name = data.get('name')
    category = data.get('category')
    price = data.get('price')
    if not all([name, category, price]): return jsonify({"error": "Nome, categoria e preço são obrigatórios."}), 400
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    cursor = conn.cursor()
    cursor.execute("INSERT INTO services (name, category, price) OUTPUT INSERTED.id_service VALUES (?, ?, ?)", name, category, float(price))
    new_service_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({"message": "Serviço adicionado com sucesso", "id": new_service_id}), 201

@app.route('/services/<int:service_id>', methods=['PUT'])
@handle_errors
def update_service(service_id):
    data = request.json
    name = data.get('name')
    category = data.get('category')
    price = data.get('price')
    if not all([name, category, price]): return jsonify({"error": "Nome, categoria e preço são obrigatórios."}), 400
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    cursor = conn.cursor()
    cursor.execute("UPDATE services SET name = ?, category = ?, price = ? WHERE id_service = ?", name, category, float(price), service_id)
    conn.commit()
    conn.close()
    if cursor.rowcount == 0: return jsonify({"error": "Serviço não encontrado."}), 404
    return jsonify({"message": "Serviço atualizado com sucesso"}), 200

@app.route('/services/<int:service_id>', methods=['DELETE'])
@handle_errors
def delete_service(service_id):
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    cursor = conn.cursor()
    cursor.execute("DELETE FROM services WHERE id_service = ?", service_id)
    conn.commit()
    conn.close()
    if cursor.rowcount == 0: return jsonify({"error": "Serviço não encontrado."}), 404
    return jsonify({"message": "Serviço apagado com sucesso"}), 200

# --- Rotas para Costureiras (CRUD Completo) ---
@app.route('/seamstresses', methods=['GET'])
@handle_errors
def get_seamstresses():
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    cursor.execute("SELECT id_seamstress, name FROM seamstresses ORDER BY name")
    seamstresses = [{"id": row.id_seamstress, "name": row.name} for row in cursor.fetchall()]
    conn.close()
    return jsonify(seamstresses), 200

@app.route('/seamstresses', methods=['POST'])
@handle_errors
def add_seamstress():
    data = request.json
    name = data.get('name')
    if not name: return jsonify({"error": "O nome é obrigatório."}), 400
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    cursor = conn.cursor()
    cursor.execute("INSERT INTO seamstresses (name) OUTPUT INSERTED.id_seamstress VALUES (?)", name)
    new_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({"message": "Costureira adicionada com sucesso", "id": new_id}), 201

@app.route('/seamstresses/<int:seamstress_id>', methods=['PUT'])
@handle_errors
def update_seamstress(seamstress_id):
    data = request.json
    name = data.get('name')
    if not name: return jsonify({"error": "O nome é obrigatório."}), 400
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    cursor = conn.cursor()
    cursor.execute("UPDATE seamstresses SET name = ? WHERE id_seamstress = ?", name, seamstress_id)
    conn.commit()
    conn.close()
    if cursor.rowcount == 0: return jsonify({"error": "Costureira não encontrada."}), 404
    return jsonify({"message": "Costureira atualizada com sucesso"}), 200

@app.route('/seamstresses/<int:seamstress_id>', methods=['DELETE'])
@handle_errors
def delete_seamstress(seamstress_id):
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    cursor = conn.cursor()
    cursor.execute("DELETE FROM seamstresses WHERE id_seamstress = ?", seamstress_id)
    conn.commit()
    conn.close()
    if cursor.rowcount == 0: return jsonify({"error": "Costureira não encontrada."}), 404
    return jsonify({"message": "Costureira apagada com sucesso"}), 200

# --- Rotas para Pedidos ---
@app.route('/pedidos', methods=['POST'])
@handle_errors
def add_pedido():
    data = request.json
    delivery_date = data.get('deliveryDate')
    if not delivery_date: return jsonify({"error": "A data de entrega prevista é obrigatória."}), 400
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO pedidos (id_client, data_prevista, observacoes, desconto, preco_total, status, data_entrada) OUTPUT INSERTED.id_pedido VALUES (?, ?, ?, ?, ?, 'Pendente', GETDATE())",
        data['clientId'], delivery_date, data.get('comments', ''), data.get('discount', 0), data['totalPrice']
    )
    pedido_id = cursor.fetchone()[0]
    for service in data['services']:
        cursor.execute(
            "INSERT INTO pedido_servicos (id_pedido, id_service, quantity, description, status) VALUES (?, ?, ?, ?, 'Pendente')",
            pedido_id, service['id'], service['quantity'], service.get('description', '')
        )
    conn.commit()
    conn.close()
    return jsonify({"message": "Pedido criado com sucesso", "pedido_id": pedido_id}), 201

@app.route('/pedidos/pendentes', methods=['GET'])
@handle_errors
def get_pending_services_by_date():
    selected_date_str = request.args.get('date')
    search_term = request.args.get('search')
    
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    
    query = """
        SELECT 
            ps.id_pedido_servico, ps.id_pedido, ps.quantity, ps.description, ps.status,
            s.name as service_name, c.name as client_name, p.data_prevista,
            ss.name as costureira_conclusao, ps.data_conclusao
        FROM pedido_servicos ps
        JOIN pedidos p ON ps.id_pedido = p.id_pedido
        JOIN services s ON ps.id_service = s.id_service
        JOIN clients c ON p.id_client = c.id_client
        LEFT JOIN seamstresses ss ON ps.id_seamstress_conclusao = ss.id_seamstress
    """
    params = []
    where_clauses = []
    
    if search_term:
        where_clauses.append("(c.name LIKE ? OR CAST(p.id_pedido AS VARCHAR(20)) LIKE ?)")
        params.extend([f"%{search_term}%", f"%{search_term}%"])
    else:
        where_clauses.append("ps.status = 'Pendente'")
    
    if selected_date_str and not search_term:
        where_clauses.append("p.data_prevista = ?")
        params.append(selected_date_str)
        
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
        
    query += " ORDER BY p.data_prevista, p.id_pedido"
    
    cursor.execute(query, *params)
    
    services = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]
    conn.close()
    
    for service in services:
        if service.get('data_prevista'): service['data_prevista'] = service['data_prevista'].strftime('%d/%m/%Y') if service['data_prevista'] else None
        if service.get('data_conclusao'): service['data_conclusao'] = service['data_conclusao'].strftime('%d/%m/%Y %H:%M') if service['data_conclusao'] else None

    if search_term:
        return jsonify(services)

    today = datetime.now().date()
    
    categorized_services = {"hoje": [], "atrasados": [], "proximos": []}
    
    for s in services:
        try:
            if s.get('data_prevista'):
                service_date = datetime.strptime(s['data_prevista'], '%d/%m/%Y').date()
                if service_date == today:
                    categorized_services["hoje"].append(s)
                elif service_date < today:
                    categorized_services["atrasados"].append(s)
                else:
                    categorized_services["proximos"].append(s)
        except (ValueError, TypeError):
             print(f"Aviso: Data inválida ou nula para o serviço do pedido {s.get('id_pedido')}. Ignorando na categorização.")

    return jsonify(categorized_services)

@app.route('/pedidos/stats', methods=['GET'])
@handle_errors
def get_daily_stats():
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    today_str = date.today().strftime('%Y-%m-%d')
    
    cursor.execute("SELECT COUNT(ps.id_pedido_servico) FROM pedido_servicos ps JOIN pedidos p ON ps.id_pedido = p.id_pedido WHERE ps.status = 'Pendente' AND p.data_prevista = ?", today_str)
    pending_today = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(id_pedido_servico) FROM pedido_servicos WHERE status = 'Concluído' AND CAST(data_conclusao AS DATE) = ?", today_str)
    completed_today = cursor.fetchone()[0] or 0
    conn.close()
    return jsonify({"pending_today": pending_today, "completed_today": completed_today})

@app.route('/pedidos/servico/<int:pedido_servico_id>/concluir', methods=['PUT'])
@handle_errors
def complete_service_item(pedido_servico_id):
    data = request.json
    seamstress_id = data.get('id_seamstress')
    if not seamstress_id: return jsonify({"error": "ID da costureira é obrigatório."}), 400
    conn = get_db_connection()
    if conn is None: return jsonify({"error": "Erro de conexão"}), 500
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE pedido_servicos SET status = 'Concluído', id_seamstress_conclusao = ?, data_conclusao = GETDATE() WHERE id_pedido_servico = ?",
        seamstress_id, pedido_servico_id
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0: return jsonify({"error": "Item de serviço não encontrado."}), 404
    return jsonify({"message": "Serviço marcado como concluído."}), 200

if __name__ == '__main__':
    # A alteração principal é adicionar host='0.0.0.0'
    # Isto permite que o servidor seja acessível por outras máquinas na mesma rede.
    app.run(host='0.0.0.0', port=7000, debug=True)

