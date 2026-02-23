import os
from datetime import date, datetime, timedelta
from functools import wraps
import base64
import traceback

import psycopg
from psycopg.rows import dict_row
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    session,
    url_for,
)

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# -----------------------------------------------------------------------------
# QZ + Document numbering (OT/FR) - backend controlled
# -----------------------------------------------------------------------------
_SCHEMA_READY = False

def _ensure_schema() -> None:
    """Ensure minimal schema objects exist (idempotent)."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    db_url = _get_database_url()
    if not db_url:
        # Without DB we can't create sequences; keep app running.
        _SCHEMA_READY = True
        return
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                # sequence table for document numbering
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS doc_sequences (
                        year INT NOT NULL,
                        doc_type TEXT NOT NULL,
                        next_seq INT NOT NULL,
                        PRIMARY KEY (year, doc_type)
                    )"""
                )
                # columns on pedidos to store doc identity
                cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS doc_number TEXT")
                cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS doc_type TEXT")
                cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS doc_seq INT")
            conn.commit()
    except Exception:
        # Don't crash app on schema issues; we will surface errors at /pedidos creation.
        pass
    _SCHEMA_READY = True


def _next_doc_number(conn, include_nif: bool) -> tuple[str, str, int]:
    """Return (doc_number, doc_type, seq) using a DB transaction & row lock."""
    year = datetime.utcnow().year
    doc_type = "FR" if include_nif else "OT"
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO doc_sequences(year, doc_type, next_seq)
                 VALUES (%s, %s, 1)
                 ON CONFLICT (year, doc_type) DO NOTHING""",
            (year, doc_type),
        )
        cur.execute(
            """SELECT next_seq FROM doc_sequences
                 WHERE year=%s AND doc_type=%s
                 FOR UPDATE""",
            (year, doc_type),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("Falha ao obter sequencia de documento.")
        seq = int(row[0])
        cur.execute(
            """UPDATE doc_sequences
                 SET next_seq = next_seq + 1
                 WHERE year=%s AND doc_type=%s""",
            (year, doc_type),
        )
    doc_number = f"{doc_type} {year}/{seq:02d}"
    return doc_number, doc_type, seq


@app.before_request
def _schema_bootstrap():
    # one-time schema check
    _ensure_schema()


def _qz_private_key_pem() -> str:
    return (os.environ.get("QZ_PRIVATE_KEY_PEM") or "").strip()


@app.get("/qz/health")
def qz_health():
    cert_path = os.path.join(app.static_folder or "static", "qz", "certificate.pem")
    return jsonify(
        {
            "ok": True,
            "certificate_url": "/static/qz/certificate.pem",
            "has_certificate_file": os.path.exists(cert_path),
            "has_private_key_env": bool(_qz_private_key_pem()),
        }
    )


@app.post("/qz/sign")
def qz_sign():
    """Sign raw data for QZ Tray using RSA + SHA256.

    QZ Tray expects the response body to be a Base64 string (no JSON).
    """
    try:
        data: bytes = request.get_data(cache=False) or b""
        if not data:
            return "empty", 400

        private_key_pem = _qz_private_key_pem()
        if not private_key_pem:
            raise RuntimeError("QZ_PRIVATE_KEY_PEM vazio (defina no Railway).")

        # Lazy import to keep startup light.
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode("ascii")

    except Exception as e:
        print("[QZ/SIGN] ERRO:", repr(e))
        print(traceback.format_exc())
        return "error", 500



# -------------------------------------------------------------------------
# Schema safety (migrações leves em runtime)
# -------------------------------------------------------------------------
def ensure_schema():
    """Aplica ALTER TABLE leves e idempotentes.
    Observação: para projetos maiores, prefira uma migração (Alembic).
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Flag para relatórios: pedido com contribuinte (NIF) selecionado
                cur.execute(
                    """
                    ALTER TABLE IF EXISTS pedidos
                    ADD COLUMN IF NOT EXISTS include_nif boolean NOT NULL DEFAULT false
                    """
                )
            conn.commit()
    except Exception as e:
        # Não derruba o app por falha de migração leve; loga apenas.
        print(f"[SCHEMA] ensure_schema falhou: {e}")



# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
def _get_database_url() -> str | None:
    # Railway normalmente injeta DATABASE_URL, mas vamos aceitar variações
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("database_url")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("postgres_url")
        or os.environ.get("DATABASE_URL_INTERNAL")
        or os.environ.get("DATABASE_URL_PUBLIC")
        or os.environ.get("DATABASE_URL_PRIVATE")
        or os.environ.get("database_url_private")
    )


def get_db_connection():
    db_url = _get_database_url()
    if not db_url:
        raise RuntimeError("DATABASE_URL não está definida no ambiente.")
    # dict_row => cursor retorna dicts
    return psycopg.connect(db_url, row_factory=dict_row)


def handle_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except psycopg.Error as e:
            print(f"[DB ERROR] {request.method} {request.path}: {e}")
            return jsonify({"error": "Erro de banco de dados."}), 500
        except Exception as e:
            print(f"[ERROR] {request.method} {request.path}: {e}")
            return jsonify({"error": str(e)}), 500

    return wrapper


ensure_schema()


# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
def _env_user_pass(prefix: str) -> tuple[str | None, str | None]:
    """
    Lê PREFIX_USER / PREFIX_PASS
    Também aceita "PREFIX_USER" no formato "user:pass"
    """
    u = os.environ.get(f"{prefix}_USER")
    p = os.environ.get(f"{prefix}_PASS")

    # fallback: se o cara colocou tudo em uma env só "user:pass"
    if u and (":" in u) and (p is None):
        parts = u.split(":", 1)
        u = parts[0].strip()
        p = parts[1].strip()
    return u, p


def get_users_config():
    admin_u, admin_p = _env_user_pass("ADMIN")
    caixa_u, caixa_p = _env_user_pass("CAIXA")
    cost_u, cost_p = _env_user_pass("COSTUREIRA")

    users = {}
    if admin_u and admin_p:
        users[admin_u] = {"password": admin_p, "role": "admin"}
    if caixa_u and caixa_p:
        users[caixa_u] = {"password": caixa_p, "role": "caixa"}
    if cost_u and cost_p:
        users[cost_u] = {"password": cost_p, "role": "costureira"}
    return users


def login_required(roles: list[str] | None = None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            if roles:
                if session.get("role") not in roles:
                    return jsonify({"error": "Acesso negado."}), 403
            return f(*args, **kwargs)

        return wrapper

    return decorator


# -----------------------------------------------------------------------------
# Simple login page (sem template extra)
# -----------------------------------------------------------------------------
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="pt-PT">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sistema de Vendas - Login</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen flex items-center justify-center bg-[#f7f3f1] p-4">
  <div class="bg-white w-full max-w-md rounded-2xl shadow-2xl p-8">
    <h1 class="text-3xl font-extrabold text-center text-purple-700">Sistema de Vendas</h1>
    <p class="text-center text-gray-500 mt-1">Acesso restrito</p>

    {% if error %}
      <div class="mt-6 bg-red-50 border border-red-200 text-red-700 p-3 rounded-lg text-sm">
        {{ error }}
      </div>
    {% endif %}

    <form class="mt-6 space-y-4" method="POST" action="/login">
      <input type="hidden" name="next" value="{{ next_url or '' }}" />
      <div>
        <label class="block text-sm font-medium text-gray-700">Usuário</label>
        <input name="username" value="{{ username or '' }}" class="mt-1 w-full px-4 py-2 rounded-lg border border-gray-300 focus:outline-none focus:ring-2 focus:ring-purple-500" />
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-700">Senha</label>
        <input name="password" type="password" class="mt-1 w-full px-4 py-2 rounded-lg border border-gray-300 focus:outline-none focus:ring-2 focus:ring-purple-500" />
      </div>
      <button class="w-full bg-purple-600 hover:bg-purple-700 text-white font-semibold py-3 rounded-lg">
        Entrar
      </button>
    </form>

    <div class="mt-4 text-center">
      <a href="/logout?next=/login" class="text-sm text-gray-500 hover:text-gray-700 underline">Limpar sessão</a>
    </div>
  </div>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Admin dashboard (inline + fetch stats)
# -----------------------------------------------------------------------------
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="pt-PT">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Painel Admin</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-[#0b1220] text-white">
  <div class="flex min-h-screen">
    <aside class="w-72 bg-[#0a1020] border-r border-white/10 p-4">
      <div class="flex items-center gap-3 mb-6">
        <div class="w-10 h-10 rounded-xl bg-white/10 flex items-center justify-center font-bold">SV</div>
        <div>
          <div class="font-bold leading-5">Sistema-Vendas</div>
          <div class="text-xs text-white/60">Painel Admin</div>
        </div>
      </div>

      <nav class="space-y-2">
        <a href="/admin" class="block px-3 py-2 rounded-xl bg-white/10 hover:bg-white/15">
          🏠 <span class="ml-2">Dashboard</span>
          <div class="text-xs text-white/60 ml-6">Resumo do sistema</div>
        </a>

        <a href="/" class="block px-3 py-2 rounded-xl hover:bg-white/10">
          🧾 <span class="ml-2">Atendimento (Caixa)</span>
          <div class="text-xs text-white/60 ml-6">Criar pedidos</div>
        </a>

        <a href="/gerenciamento" class="block px-3 py-2 rounded-xl hover:bg-white/10">
          🧩 <span class="ml-2">Gerenciamento</span>
          <div class="text-xs text-white/60 ml-6">Serviços e costureiras</div>
        </a>

        <a href="/costureiras" class="block px-3 py-2 rounded-xl hover:bg-white/10">
          🧵 <span class="ml-2">Painel Costureiras</span>
          <div class="text-xs text-white/60 ml-6">Pendências e conclusão</div>
        </a>

        <div class="mt-6 pt-4 border-t border-white/10 text-white/50 text-sm">
          <div>Usuários (em breve)</div>
          <div class="text-xs">Criar/editar/remover</div>
          <div class="mt-3">Relatórios (em breve)</div>
          <div class="text-xs">Vendas e produtividade</div>
        </div>
      </nav>

      <div class="mt-6">
        <a href="/logout?next=/login" class="block text-center px-4 py-2 rounded-xl bg-red-500/20 hover:bg-red-500/30 border border-red-500/30">
          Sair
        </a>
      </div>
    </aside>

    <main class="flex-1 p-8">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-3xl font-bold">Dashboard</h1>
          <p class="text-white/60 mt-1">Aqui vamos centralizar tudo do admin.</p>
        </div>
        <div class="text-white/70 text-sm">
          Logado como: <span class="font-semibold text-white">{{ user }}</span>
        </div>
      </div>

      <!-- KPIs -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mt-8">
        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/60 text-sm">Pendentes hoje</div>
          <div id="kpi-pendentes-hoje" class="text-3xl font-extrabold mt-1">—</div>
        </div>
        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/60 text-sm">Concluídos hoje</div>
          <div id="kpi-concluidos-hoje" class="text-3xl font-extrabold mt-1">—</div>
        </div>
        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/60 text-sm">Atrasados</div>
          <div id="kpi-atrasados" class="text-3xl font-extrabold mt-1">—</div>
        </div>

        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/60 text-sm">Pedidos criados hoje</div>
          <div id="kpi-pedidos-hoje" class="text-3xl font-extrabold mt-1">—</div>
        </div>
        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/60 text-sm">Faturamento hoje</div>
          <div id="kpi-fat-hoje" class="text-3xl font-extrabold mt-1">—</div>
        </div>
        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/60 text-sm">Faturamento mês</div>
          <div id="kpi-fat-mes" class="text-3xl font-extrabold mt-1">—</div>
        </div>
      </div>

      <!-- Quick links -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mt-6">
        <a href="/gerenciamento" class="p-5 rounded-2xl bg-white/5 border border-white/10 hover:bg-white/10 transition">
          <div class="font-bold text-lg">Gerenciamento</div>
          <div class="text-white/60 text-sm mt-1">Cadastrar/editar serviços e costureiras</div>
        </a>

        <a href="/" class="p-5 rounded-2xl bg-white/5 border border-white/10 hover:bg-white/10 transition">
          <div class="font-bold text-lg">Atendimento</div>
          <div class="text-white/60 text-sm mt-1">Criar pedidos e imprimir talão</div>
        </a>

        <a href="/costureiras" class="p-5 rounded-2xl bg-white/5 border border-white/10 hover:bg-white/10 transition">
          <div class="font-bold text-lg">Painel Costureiras</div>
          <div class="text-white/60 text-sm mt-1">Acompanhar pendências e conclusões</div>
        </a>
      </div>

      <!-- Lists -->
      <div class="grid grid-cols-1 xl:grid-cols-3 gap-4 mt-6">
        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="font-bold text-lg">Atrasados</div>
          <div class="text-white/60 text-sm mt-1">Top 10</div>
          <div id="list-atrasados" class="mt-4 space-y-3 text-sm text-white/80">Carregando...</div>
        </div>

        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="font-bold text-lg">Próximos 7 dias</div>
          <div class="text-white/60 text-sm mt-1">Top 10</div>
          <div id="list-proximos" class="mt-4 space-y-3 text-sm text-white/80">Carregando...</div>
        </div>

        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="font-bold text-lg">Últimos pedidos</div>
          <div class="text-white/60 text-sm mt-1">Top 10</div>
          <div id="list-ultimos" class="mt-4 space-y-3 text-sm text-white/80">Carregando...</div>
        </div>
      </div>

      <div class="mt-6 p-6 rounded-2xl bg-white/5 border border-white/10">
        <div class="font-bold text-lg">Próximos passos</div>
        <ul class="list-disc ml-6 mt-2 text-white/70 text-sm space-y-1">
          <li>Gestão de usuários (admin cria caixa/costureira, reseta senha, desativa)</li>
          <li>Relatórios e exportação</li>
        </ul>
      </div>
    </main>
  </div>

<script>
  function eur(v){
    if (v === null || v === undefined) return "—";
    try {
      return new Intl.NumberFormat('pt-PT', { style: 'currency', currency: 'EUR' }).format(Number(v));
    } catch(e) {
      return "€ " + v;
    }
  }

  function itemLinhaPedido(p){
    const dt = p.data_prevista ? String(p.data_prevista) : "—";
    const total = (p.preco_total !== null && p.preco_total !== undefined) ? eur(p.preco_total) : "—";
    const pend = (p.pendentes !== null && p.pendentes !== undefined) ? `${p.pendentes} pend.` : "";
    return `
      <div class="p-3 rounded-xl bg-white/5 border border-white/10">
        <div class="flex items-center justify-between">
          <div class="font-semibold">#${p.id_pedido} — ${p.client_name || ""}</div>
          <div class="text-white/60">${dt}</div>
        </div>
        <div class="flex items-center justify-between mt-1 text-white/70">
          <div>${pend}</div>
          <div class="font-semibold">${total}</div>
        </div>
      </div>
    `;
  }

  async function loadAdminStats(){
    try{
      const r = await fetch("/admin/stats", { credentials: "same-origin" });
      if(!r.ok){
        throw new Error("Falha ao carregar stats: HTTP " + r.status);
      }
      const data = await r.json();

      document.getElementById("kpi-pendentes-hoje").textContent = data.kpis.pending_today ?? "—";
      document.getElementById("kpi-concluidos-hoje").textContent = data.kpis.completed_today ?? "—";
      document.getElementById("kpi-atrasados").textContent = data.kpis.overdue ?? "—";
      document.getElementById("kpi-pedidos-hoje").textContent = data.kpis.orders_today ?? "—";
      document.getElementById("kpi-fat-hoje").textContent = eur(data.kpis.revenue_today);
      document.getElementById("kpi-fat-mes").textContent = eur(data.kpis.revenue_month);

      const atrasados = data.lists.overdue || [];
      const proximos = data.lists.next7 || [];
      const ultimos = data.lists.latest || [];

      const elA = document.getElementById("list-atrasados");
      elA.innerHTML = atrasados.length ? atrasados.map(itemLinhaPedido).join("") : '<div class="text-white/60">Nenhum.</div>';

      const elP = document.getElementById("list-proximos");
      elP.innerHTML = proximos.length ? proximos.map(itemLinhaPedido).join("") : '<div class="text-white/60">Nenhum.</div>';

      const elU = document.getElementById("list-ultimos");
      elU.innerHTML = ultimos.length ? ultimos.map(itemLinhaPedido).join("") : '<div class="text-white/60">Nenhum.</div>';

    }catch(e){
      console.error(e);
      document.getElementById("list-atrasados").innerHTML = '<div class="text-red-300">Erro ao carregar.</div>';
      document.getElementById("list-proximos").innerHTML = '<div class="text-red-300">Erro ao carregar.</div>';
      document.getElementById("list-ultimos").innerHTML = '<div class="text-red-300">Erro ao carregar.</div>';
    }
  }

  loadAdminStats();
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


@app.get("/db-test")
@handle_errors
def db_test():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok;")
            row = cur.fetchone()
    return jsonify({"db": "ok", "result": row}), 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        next_url = request.args.get("next") or "/"
        return render_template_string(LOGIN_HTML, error=None, next_url=next_url, username="")

    users = get_users_config()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.form.get("next") or "/"

    if username in users and password == users[username]["password"]:
        session["user"] = username
        session["role"] = users[username]["role"]

        role = session["role"]
        if role == "admin":
            return redirect("/admin")
        if role == "costureira":
            return redirect("/costureiras")
        if role == "caixa":
            return redirect("/")
        return redirect(next_url)

    return render_template_string(
        LOGIN_HTML,
        error="Usuário ou senha inválidos",
        next_url=next_url,
        username=username,
    ), 401


@app.get("/logout")
def logout():
    session.clear()
    next_url = request.args.get("next") or "/login"
    return redirect(next_url)


@app.get("/admin")
@login_required(["admin"])
def admin_dashboard():
    return render_template_string(ADMIN_HTML, user=session.get("user"))


@app.get("/admin/stats")
@login_required(["admin"])
@handle_errors
def admin_stats():
    today = date.today()
    start_month = today.replace(day=1)
    next7 = today + timedelta(days=7)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # KPI: pendentes hoje (serviços)
            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos ps
                JOIN pedidos p ON p.id_pedido = ps.id_pedido
                WHERE p.data_prevista = %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                """,
                (today,),
            )
            pending_today = cur.fetchone()["c"]

            # KPI: concluídos hoje (serviços)
            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos
                WHERE status = 'Concluído'
                  AND data_conclusao::date = %s
                """,
                (today,),
            )
            completed_today = cur.fetchone()["c"]

            # KPI: atrasados (serviços pendentes com data prevista < hoje)
            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos ps
                JOIN pedidos p ON p.id_pedido = ps.id_pedido
                WHERE p.data_prevista < %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                """,
                (today,),
            )
            overdue = cur.fetchone()["c"]

            # KPI: pedidos criados hoje
            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedidos
                WHERE data_entrada::date = %s
                """,
                (today,),
            )
            orders_today = cur.fetchone()["c"]

            # KPI: faturamento hoje (se preco_total existir)
            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::float AS v
                FROM pedidos
                WHERE data_entrada::date = %s
                """,
                (today,),
            )
            revenue_today = float(cur.fetchone()["v"] or 0)

            # KPI: faturamento mês
            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::float AS v
                FROM pedidos
                WHERE data_entrada::date >= %s
                  AND data_entrada::date <= %s
                """,
                (start_month, today),
            )
            revenue_month = float(cur.fetchone()["v"] or 0)

            # LISTA: atrasados (top 10 por pedido)
            cur.execute(
                """
                SELECT
                  p.id_pedido,
                  c.name AS client_name,
                  p.data_prevista,
                  p.preco_total,
                  (
                    SELECT COUNT(*)::int
                    FROM pedido_servicos ps2
                    WHERE ps2.id_pedido = p.id_pedido
                      AND COALESCE(ps2.status,'') <> 'Concluído'
                  ) AS pendentes
                FROM pedidos p
                JOIN clients c ON c.id_client = p.id_client
                WHERE p.data_prevista < %s
                  AND COALESCE(p.status,'') <> 'Concluído'
                ORDER BY p.data_prevista ASC, p.id_pedido ASC
                LIMIT 10
                """,
                (today,),
            )
            list_overdue = cur.fetchall()

            # LISTA: próximos 7 dias (top 10)
            cur.execute(
                """
                SELECT
                  p.id_pedido,
                  c.name AS client_name,
                  p.data_prevista,
                  p.preco_total,
                  (
                    SELECT COUNT(*)::int
                    FROM pedido_servicos ps2
                    WHERE ps2.id_pedido = p.id_pedido
                      AND COALESCE(ps2.status,'') <> 'Concluído'
                  ) AS pendentes
                FROM pedidos p
                JOIN clients c ON c.id_client = p.id_client
                WHERE p.data_prevista >= %s
                  AND p.data_prevista <= %s
                  AND COALESCE(p.status,'') <> 'Concluído'
                ORDER BY p.data_prevista ASC, p.id_pedido ASC
                LIMIT 10
                """,
                (today, next7),
            )
            list_next7 = cur.fetchall()

            # LISTA: últimos pedidos (top 10)
            cur.execute(
                """
                SELECT
                  p.id_pedido,
                  c.name AS client_name,
                  p.data_prevista,
                  p.preco_total,
                  (
                    SELECT COUNT(*)::int
                    FROM pedido_servicos ps2
                    WHERE ps2.id_pedido = p.id_pedido
                      AND COALESCE(ps2.status,'') <> 'Concluído'
                  ) AS pendentes
                FROM pedidos p
                JOIN clients c ON c.id_client = p.id_client
                ORDER BY p.id_pedido DESC
                LIMIT 10
                """
            )
            list_latest = cur.fetchall()

    def _pedido_list_row(r: dict) -> dict:
        dp = r.get("data_prevista")
        dp_str = dp.strftime("%Y-%m-%d") if isinstance(dp, (date, datetime)) else None
        return {
            "id_pedido": r.get("id_pedido"),
            "client_name": r.get("client_name"),
            "data_prevista": dp_str,
            "preco_total": float(r.get("preco_total") or 0) if r.get("preco_total") is not None else None,
            "pendentes": r.get("pendentes"),
        }

    return jsonify(
        {
            "kpis": {
                "pending_today": pending_today,
                "completed_today": completed_today,
                "overdue": overdue,
                "orders_today": orders_today,
                "revenue_today": revenue_today,
                "revenue_month": revenue_month,
            },
            "lists": {
                "overdue": [_pedido_list_row(x) for x in list_overdue],
                "next7": [_pedido_list_row(x) for x in list_next7],
                "latest": [_pedido_list_row(x) for x in list_latest],
            },
        }
    ), 200


@app.get("/")
@login_required(["admin", "caixa"])
@handle_errors
def serve_main_page():
    return render_template("atendimento.html", user=session.get("user"), role=session.get("role"))


@app.get("/gerenciamento")
@login_required(["admin"])
@handle_errors
def serve_management_page():
    # atenção: seu arquivo no GitHub está "Gerenciamento.html" (G maiúsculo)
    return render_template("Gerenciamento.html")


@app.get("/costureiras")
@login_required(["admin", "costureira"])
@handle_errors
def serve_seamstress_page():
    return render_template("costureiras.html")


# -----------------------------------------------------------------------------
# API - Clients
# -----------------------------------------------------------------------------
@app.get("/clients/<phone>")
@login_required(["admin", "caixa"])
@handle_errors
def get_client_by_phone(phone):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id_client, name, nif FROM clients WHERE phone = %s",
                (phone,),
            )
            row = cur.fetchone()

    if row:
        return jsonify({"id": row["id_client"], "name": row["name"], "nif": row["nif"]}), 200
    return jsonify({"error": "Cliente não encontrado."}), 404


@app.get("/clients/search")
@login_required(["admin", "caixa"])
@handle_errors
def search_clients():
    """Busca por telefone (parcial) ou nome.
    Query param: ?q=...
    Retorna lista (máx 20).
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []}), 200

    q_digits = "".join([c for c in q if c.isdigit()])
    like_name = f"%{q}%"
    like_phone = f"%{q_digits}%" if q_digits else None

    sql = "SELECT id_client, name, phone, nif FROM clients WHERE "
    params = []

    if like_phone:
        sql += "(phone ILIKE %s) OR (name ILIKE %s) "
        params.extend([like_phone, like_name])
    else:
        sql += "name ILIKE %s "
        params.append(like_name)

    sql += "ORDER BY name ASC LIMIT 20"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []

    return jsonify(
        {
            "results": [
                {"id": r["id_client"], "name": r["name"], "phone": r["phone"], "nif": r["nif"]}
                for r in rows
            ]
        }
    ), 200



@app.post("/clients")
@login_required(["admin", "caixa"])
@handle_errors
def add_client():
    data = request.get_json(force=True) or {}
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


@app.put("/clients/<int:client_id>/nif")
@login_required(["admin", "caixa"])
@handle_errors
def update_client_nif(client_id):
    data = request.get_json(force=True) or {}
    nif = (data.get("nif") or "").strip()
    if not nif:
        return jsonify({"error": "O NIF é obrigatório."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clients SET nif = %s WHERE id_client = %s",
                (nif, client_id),
            )
            updated = cur.rowcount
        conn.commit()

    if updated == 0:
        return jsonify({"error": "Cliente não encontrado."}), 404
    return jsonify({"message": "NIF atualizado com sucesso."}), 200


@app.get("/clients/by-id/<int:client_id>")
@login_required(["admin", "caixa"])
@handle_errors
def get_client_by_id(client_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id_client, name, phone, nif FROM clients WHERE id_client = %s",
                (client_id,),
            )
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "Cliente não encontrado."}), 404

    return jsonify(
        {"id": row["id_client"], "name": row["name"], "phone": row["phone"], "nif": row["nif"]}
    ), 200


@app.put("/clients/by-id/<int:client_id>")
@login_required(["admin", "caixa"])
@handle_errors
def update_client_by_id(client_id):
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    nif = (data.get("nif") or "").strip() or None

    if not name or not phone:
        return jsonify({"error": "Nome e telefone são obrigatórios."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Impede duplicação de telefone em outro cliente
            cur.execute(
                "SELECT id_client FROM clients WHERE phone = %s AND id_client <> %s",
                (phone, client_id),
            )
            exists = cur.fetchone()
            if exists:
                return jsonify({"error": "Já existe outro cliente com este telefone."}), 409

            cur.execute(
                "UPDATE clients SET name = %s, phone = %s, nif = %s WHERE id_client = %s",
                (name, phone, nif, client_id),
            )
            updated = cur.rowcount
        conn.commit()

    if updated == 0:
        return jsonify({"error": "Cliente não encontrado."}), 404

    return jsonify({"message": "Cliente atualizado com sucesso."}), 200



# -----------------------------------------------------------------------------
# API - Services (CRUD)
# -----------------------------------------------------------------------------
@app.get("/services")
@login_required(["admin", "caixa"])
@handle_errors
def get_services():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id_service, name, price, category FROM services ORDER BY category, name"
            )
            rows = cur.fetchall()

    services_by_category: dict[str, list[dict]] = {}
    for r in rows:
        cat = r["category"]
        services_by_category.setdefault(cat, []).append(
            {"id": r["id_service"], "nome": r["name"], "preco": float(r["price"])}
        )
    return jsonify(services_by_category), 200


@app.post("/services")
@login_required(["admin"])
@handle_errors
def add_service():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    price = data.get("price")

    if not name or not category or price is None:
        return jsonify({"error": "Nome, categoria e preço são obrigatórios."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO services (name, category, price) VALUES (%s, %s, %s) RETURNING id_service",
                (name, category, float(price)),
            )
            new_id = cur.fetchone()["id_service"]
        conn.commit()

    return jsonify({"message": "Serviço adicionado com sucesso", "id": new_id}), 201


@app.put("/services/<int:service_id>")
@login_required(["admin"])
@handle_errors
def update_service(service_id):
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    category = (data.get("category") or "").strip()
    price = data.get("price")

    if not name or not category or price is None:
        return jsonify({"error": "Nome, categoria e preço são obrigatórios."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE services SET name=%s, category=%s, price=%s WHERE id_service=%s",
                (name, category, float(price), service_id),
            )
            updated = cur.rowcount
        conn.commit()

    if updated == 0:
        return jsonify({"error": "Serviço não encontrado."}), 404
    return jsonify({"message": "Serviço atualizado com sucesso"}), 200


@app.delete("/services/<int:service_id>")
@login_required(["admin"])
@handle_errors
def delete_service(service_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM services WHERE id_service=%s", (service_id,))
            deleted = cur.rowcount
        conn.commit()

    if deleted == 0:
        return jsonify({"error": "Serviço não encontrado."}), 404
    return jsonify({"message": "Serviço apagado com sucesso"}), 200


# -----------------------------------------------------------------------------
# API - Seamstresses (CRUD)
# -----------------------------------------------------------------------------
@app.get("/seamstresses")
@login_required(["admin", "costureira"])
@handle_errors
def get_seamstresses():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id_seamstress, name FROM seamstresses ORDER BY name")
            rows = cur.fetchall()
    return jsonify([{"id": r["id_seamstress"], "name": r["name"]} for r in rows]), 200


@app.post("/seamstresses")
@login_required(["admin"])
@handle_errors
def add_seamstress():
    data = request.get_json(force=True) or {}
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

    return jsonify({"message": "Costureira adicionada com sucesso", "id": new_id}), 201


@app.put("/seamstresses/<int:seamstress_id>")
@login_required(["admin"])
@handle_errors
def update_seamstress(seamstress_id):
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "O nome é obrigatório."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE seamstresses SET name=%s WHERE id_seamstress=%s",
                (name, seamstress_id),
            )
            updated = cur.rowcount
        conn.commit()

    if updated == 0:
        return jsonify({"error": "Costureira não encontrada."}), 404
    return jsonify({"message": "Costureira atualizada com sucesso"}), 200


@app.delete("/seamstresses/<int:seamstress_id>")
@login_required(["admin"])
@handle_errors
def delete_seamstress(seamstress_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seamstresses WHERE id_seamstress=%s", (seamstress_id,))
            deleted = cur.rowcount
        conn.commit()

    if deleted == 0:
        return jsonify({"error": "Costureira não encontrada."}), 404
    return jsonify({"message": "Costureira apagada com sucesso"}), 200


# -----------------------------------------------------------------------------
# API - Pedidos
# -----------------------------------------------------------------------------
@app.post("/pedidos")
@login_required(["admin", "caixa"])
@handle_errors
def criar_pedido():
    """
    Espera o payload do atendimento.html:
    {
      clientId, deliveryDate, comments, discount, totalPrice,
      services: [{id, quantity, description, preco, nome}]
    }
    """
    data = request.get_json(force=True) or {}

    client_id = data.get("clientId")
    delivery_date = data.get("deliveryDate")
    comments = data.get("comments")
    discount = float(data.get("discount") or 0)
    total_price = float(data.get("totalPrice") or 0)
    include_nif = bool(data.get("include_nif") or data.get("includeNif") or False)
    services = data.get("services") or []

    if not client_id or not delivery_date or not services:
        return jsonify({"error": "clientId, deliveryDate e services são obrigatórios."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pedidos (id_client, data_entrada, data_prevista, observacoes, desconto, preco_total, status, include_nif)
                VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s)
                RETURNING id_pedido
                """,
                (int(client_id), delivery_date, comments, discount, total_price, "Pendente", include_nif),
            )
            pedido_id = cur.fetchone()["id_pedido"]

            for s in services:
                service_id = s.get("id")
                qty = int(s.get("quantity") or 1)
                desc = s.get("description")
                cur.execute(
                    """
                    INSERT INTO pedido_servicos (id_pedido, id_service, quantity, description, status)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (pedido_id, int(service_id), qty, desc, "Pendente"),
                )

        conn.commit()

    return jsonify({"message": "Pedido criado com sucesso", "pedido_id": pedido_id}), 201


@app.get("/pedidos/stats")
@login_required(["admin", "costureira"])
@handle_errors
def pedidos_stats():
    today = date.today()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # pendentes com entrega hoje
            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos ps
                JOIN pedidos p ON p.id_pedido = ps.id_pedido
                WHERE p.data_prevista = %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                """,
                (today,),
            )
            pending_today = cur.fetchone()["c"]

            # concluídos hoje (pela data de conclusão)
            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos
                WHERE status = 'Concluído'
                  AND data_conclusao::date = %s
                """,
                (today,),
            )
            completed_today = cur.fetchone()["c"]

    return jsonify({"pending_today": pending_today, "completed_today": completed_today}), 200


@app.get("/pedidos/recentes")
@login_required(["admin", "caixa"])
@handle_errors
def pedidos_recentes():
    """Lista serviços/pedidos dos últimos N meses (default=3).
    Filtros:
      - ?q=...  (nome/telefone/nif/pedido_id)
      - ?only_nif=1  (somente pedidos com include_nif=true)
    """
    months = int(request.args.get("months") or 3)
    q = (request.args.get("q") or "").strip()
    only_nif = (request.args.get("only_nif") or "").strip() in ("1", "true", "True", "yes", "sim")

    start_date = (date.today() - timedelta(days=30 * months))

    sql = """
      SELECT
        p.id_pedido,
        p.data_entrada,
        p.data_prevista,
        p.preco_total,
        p.desconto,
        p.include_nif,
        p.observacoes,
        c.id_client,
        c.name AS client_name,
        c.phone AS client_phone,
        c.nif AS client_nif,
        ps.id_pedido_servico,
        ps.quantity,
        ps.description,
        ps.status,
        s.name AS service_name
      FROM pedidos p
      JOIN clients c ON c.id_client = p.id_client
      JOIN pedido_servicos ps ON ps.id_pedido = p.id_pedido
      JOIN services s ON s.id_service = ps.id_service
      WHERE p.data_entrada::date >= %s
    """
    params = [start_date]

    if only_nif:
        sql += " AND p.include_nif = true "

    if q:
        if q.isdigit():
            # pedido_id exato ou telefone parcial
            sql += " AND (p.id_pedido = %s OR c.phone ILIKE %s) "
            params.extend([int(q), f"%{q}%"])
        else:
            sql += " AND (c.name ILIKE %s OR c.phone ILIKE %s OR COALESCE(c.nif,'') ILIKE %s) "
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    sql += " ORDER BY p.data_entrada DESC, p.id_pedido DESC, ps.id_pedido_servico ASC "

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []

    # agrupar por pedido
    pedidos = {}
    for r in rows:
        pid = r["id_pedido"]
        if pid not in pedidos:
            de = r["data_entrada"]
            dp = r["data_prevista"]
            pedidos[pid] = {
                "id_pedido": pid,
                "data_entrada": de.strftime("%Y-%m-%d %H:%M") if isinstance(de, datetime) else str(de),
                "data_prevista": dp.strftime("%Y-%m-%d") if isinstance(dp, (datetime, date)) else str(dp),
                "preco_total": float(r["preco_total"] or 0),
                "desconto": float(r["desconto"] or 0),
                "include_nif": bool(r["include_nif"]),
                "observacoes": r.get("observacoes") or "",
                "client": {
                    "id": r["id_client"],
                    "name": r["client_name"],
                    "phone": r["client_phone"],
                    "nif": r["client_nif"],
                },
                "services": [],
            }
        pedidos[pid]["services"].append(
            {
                "id_pedido_servico": r["id_pedido_servico"],
                "service_name": r["service_name"],
                "quantity": int(r["quantity"] or 1),
                "description": r.get("description") or "",
                "status": r.get("status") or "",
            }
        )

    return jsonify({"start_date": start_date.strftime("%Y-%m-%d"), "results": list(pedidos.values())}), 200



def _service_row_to_payload(r: dict) -> dict:
    data_prevista = r.get("data_prevista")
    if isinstance(data_prevista, (datetime, date)):
        data_prevista_str = data_prevista.strftime("%Y-%m-%d")
    else:
        data_prevista_str = None

    data_conclusao = r.get("data_conclusao")
    if isinstance(data_conclusao, datetime):
        data_conclusao_str = data_conclusao.strftime("%Y-%m-%d %H:%M")
    else:
        data_conclusao_str = None

    return {
        "id_pedido_servico": r["id_pedido_servico"],
        "id_pedido": r["id_pedido"],
        "service_name": r["service_name"],
        "client_name": r["client_name"],
        "quantity": r["quantity"],
        "status": r.get("status"),
        "data_prevista": data_prevista_str,
        "costureira_conclusao": r.get("costureira_conclusao"),
        "data_conclusao": data_conclusao_str,
    }


@app.get("/pedidos/pendentes")
@login_required(["admin", "costureira"])
@handle_errors
def pedidos_pendentes():
    """
    Suporta:
      - sem params -> retorna {atrasados, hoje, proximos}
      - ?date=YYYY-MM-DD -> retorna lista simples para aquela data
      - ?search=... -> retorna lista simples por busca (cliente ou pedido)
    """
    q_date = request.args.get("date")
    q_search = (request.args.get("search") or "").strip()

    base_sql = """
      SELECT
        ps.id_pedido_servico,
        ps.id_pedido,
        ps.quantity,
        ps.status,
        ps.data_conclusao,
        p.data_prevista,
        c.name AS client_name,
        s.name AS service_name,
        ss.name AS costureira_conclusao
      FROM pedido_servicos ps
      JOIN pedidos p ON p.id_pedido = ps.id_pedido
      JOIN clients c ON c.id_client = p.id_client
      JOIN services s ON s.id_service = ps.id_service
      LEFT JOIN seamstresses ss ON ss.id_seamstress = ps.id_seamstress_conclusao
    """

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Busca
            if q_search:
                if q_search.isdigit():
                    cur.execute(
                        base_sql
                        + """
                        WHERE ps.id_pedido = %s
                        ORDER BY p.data_prevista ASC, ps.id_pedido_servico ASC
                        """,
                        (int(q_search),),
                    )
                else:
                    cur.execute(
                        base_sql
                        + """
                        WHERE c.name ILIKE %s
                        ORDER BY p.data_prevista ASC, ps.id_pedido_servico ASC
                        """,
                        (f"%{q_search}%",),
                    )
                rows = cur.fetchall()
                return jsonify([_service_row_to_payload(r) for r in rows]), 200

            # Filtro por data (retorna lista simples)
            if q_date:
                cur.execute(
                    base_sql
                    + """
                    WHERE p.data_prevista = %s
                    ORDER BY ps.id_pedido_servico ASC
                    """,
                    (q_date,),
                )
                rows = cur.fetchall()
                return jsonify([_service_row_to_payload(r) for r in rows]), 200

            # Sem filtros -> categorizado
            today = date.today()

            # atrasados
            cur.execute(
                base_sql
                + """
                WHERE p.data_prevista < %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                ORDER BY p.data_prevista ASC, ps.id_pedido_servico ASC
                """,
                (today,),
            )
            atrasados = [_service_row_to_payload(r) for r in cur.fetchall()]

            # hoje
            cur.execute(
                base_sql
                + """
                WHERE p.data_prevista = %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                ORDER BY ps.id_pedido_servico ASC
                """,
                (today,),
            )
            hoje = [_service_row_to_payload(r) for r in cur.fetchall()]

            # proximos
            cur.execute(
                base_sql
                + """
                WHERE p.data_prevista > %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                ORDER BY p.data_prevista ASC, ps.id_pedido_servico ASC
                """,
                (today,),
            )
            proximos = [_service_row_to_payload(r) for r in cur.fetchall()]

    return jsonify({"atrasados": atrasados, "hoje": hoje, "proximos": proximos}), 200


@app.route("/pedidos/servico/<int:pedido_servico_id>/concluir", methods=["PUT", "POST"])
@login_required(["admin", "costureira"])
@handle_errors
def concluir_servico(pedido_servico_id):
    """
    Chamado pelo costureiras.html:
      PUT/POST /pedidos/servico/<id>/concluir
      body: { id_seamstress: <id> }
    """
    data = request.get_json(force=True) or {}
    seamstress_id = data.get("id_seamstress")
    if not seamstress_id:
        return jsonify({"error": "id_seamstress é obrigatório."}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # atualiza o serviço
            cur.execute(
                """
                UPDATE pedido_servicos
                SET status='Concluído',
                    id_seamstress_conclusao=%s,
                    data_conclusao=NOW()
                WHERE id_pedido_servico=%s
                RETURNING id_pedido
                """,
                (int(seamstress_id), pedido_servico_id),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Serviço do pedido não encontrado."}), 404

            pedido_id = row["id_pedido"]

            # se todos os serviços do pedido concluídos, marca pedido como concluído
            cur.execute(
                """
                SELECT COUNT(*)::int AS pendentes
                FROM pedido_servicos
                WHERE id_pedido=%s
                  AND COALESCE(status,'') <> 'Concluído'
                """,
                (pedido_id,),
            )
            pendentes = cur.fetchone()["pendentes"]
            if pendentes == 0:
                cur.execute(
                    "UPDATE pedidos SET status='Concluído' WHERE id_pedido=%s",
                    (pedido_id,),
                )

        conn.commit()

    return jsonify({"message": "Serviço concluído com sucesso.", "pedido_id": pedido_id}), 200


# -----------------------------------------------------------------------------
# Debug (protegido)
# -----------------------------------------------------------------------------
@app.get("/debug-env")
@login_required(["admin"])
def debug_env():
    # Não expõe senha; só valida se está carregando as envs
    return jsonify(
        {
            "ADMIN_USER": os.environ.get("ADMIN_USER"),
            "CAIXA_USER": os.environ.get("CAIXA_USER"),
            "COSTUREIRA_USER": os.environ.get("COSTUREIRA_USER"),
            "HAS_ADMIN_PASS": bool(os.environ.get("ADMIN_PASS")),
            "HAS_CAIXA_PASS": bool(os.environ.get("CAIXA_PASS")),
            "HAS_COSTUREIRA_PASS": bool(os.environ.get("COSTUREIRA_PASS")),
        }
    ), 200


# -----------------------------------------------------------------------------
# Local run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7000"))
    app.run(host="0.0.0.0", port=port, debug=True)