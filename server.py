import os
import base64
import csv
from decimal import Decimal, ROUND_HALF_UP
import io
from datetime import date, datetime, timedelta
from functools import wraps

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
    Response,
)

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")


# -----------------------------------------------------------------------------
# QZ Tray (assinatura + health)
# -----------------------------------------------------------------------------
def _qz_private_key_pem() -> str | None:
    return os.environ.get("QZ_PRIVATE_KEY_PEM") or os.environ.get("QZ_PRIVATE_KEY")


def _qz_sign_payload(payload: str) -> str:
    """Assina o payload vindo do QZ e retorna BASE64 (texto puro)."""
    pem = _qz_private_key_pem()
    if not pem:
        raise RuntimeError("QZ_PRIVATE_KEY_PEM não configurada.")

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    alg = (os.environ.get("QZ_SIGNATURE_ALG") or "sha256").lower().strip()
    digest = hashes.SHA256() if alg in ("sha256", "sha-256") else hashes.SHA1()

    private_key = serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=None,
    )

    sig = private_key.sign(
        payload.encode("utf-8"),
        padding.PKCS1v15(),
        digest,
    )
    return base64.b64encode(sig).decode("utf-8").strip()


@app.get("/qz/health")
def qz_health():
    cert_path = os.path.join(app.root_path, "static", "qz", "certificate.pem")
    return jsonify(
        {
            "ok": True,
            "certificate_url": "/static/qz/certificate.pem",
            "has_certificate_file": os.path.exists(cert_path),
            "has_private_key_env": bool(_qz_private_key_pem()),
            "signature_alg": (os.environ.get("QZ_SIGNATURE_ALG") or "sha256").lower().strip(),
        }
    )


@app.post("/qz/sign")
def qz_sign():
    payload = request.get_data(as_text=True) or ""
    try:
        return (_qz_sign_payload(payload), 200, {"Content-Type": "text/plain; charset=utf-8"})
    except Exception as e:
        return (f"ERROR: {e}", 500, {"Content-Type": "text/plain; charset=utf-8"})


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
def _get_database_url() -> str | None:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("database_url")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("postgres_url")
        or os.environ.get("DATABASE_URL_INTERNAL")
        or os.environ.get("DATABASE_URL_PUBLIC")
        or os.environ.get("DATABASE_URL_PRIVATE")
        or os.environ.get("DATABASE_URL_PRIVATE")
        or os.environ.get("database_url_private")
    )


def get_db_connection():
    db_url = _get_database_url()
    if not db_url:
        raise RuntimeError("DATABASE_URL não está definida no ambiente.")
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


# -------------------------------------------------------------------------
# Schema safety (migrações leves em runtime)
# -------------------------------------------------------------------------
def ensure_schema():
    """ALTER TABLE idempotente (sem quebrar deploy)."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # já existia: include_nif
                cur.execute(
                    """
                    ALTER TABLE IF EXISTS pedidos
                    ADD COLUMN IF NOT EXISTS include_nif boolean NOT NULL DEFAULT false
                    """
                )

                # novo: pagamento por cartão
                cur.execute(
                    """
                    ALTER TABLE IF EXISTS pedidos
                    ADD COLUMN IF NOT EXISTS paid_by_card boolean NOT NULL DEFAULT false
                    """
                )

                # (opcional) idx simples para filtros de caixa
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_pedidos_data_entrada_date
                    ON pedidos ((data_entrada::date))
                    """
                )
            conn.commit()
    except Exception as e:
        print(f"[SCHEMA] ensure_schema falhou: {e}")


ensure_schema()

# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
def _env_user_pass(prefix: str) -> tuple[str | None, str | None]:
    u = os.environ.get(f"{prefix}_USER")
    p = os.environ.get(f"{prefix}_PASS")
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
            if roles and session.get("role") not in roles:
                return jsonify({"error": "Acesso negado."}), 403
            return f(*args, **kwargs)

        return wrapper

    return decorator


# -----------------------------------------------------------------------------
# Simple login page (inline)
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
# Admin dashboard (inline)
# -----------------------------------------------------------------------------
ADMIN_HTML = """<!DOCTYPE html>
<html lang="pt-PT">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Dashboard</title>
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

        <a href="/controle-caixa" class="block px-3 py-2 rounded-xl hover:bg-white/10">
          💳 <span class="ml-2">Controle de Caixa</span>
          <div class="text-xs text-white/60 ml-6">Cartão, NIF e exportação</div>
        </a>

        <a href="/exportacoes-caixa" class="block px-3 py-2 rounded-xl hover:bg-white/10">
          📤 <span class="ml-2">Exportações Caixa</span>
          <div class="text-xs text-white/60 ml-6">CSV mensal detalhado</div>
        </a>
      </nav>

      <div class="mt-6">
        <a href="/logout?next=/login" class="block text-center px-4 py-2 rounded-xl bg-red-500/20 hover:bg-red-500/30 border border-red-500/30">
          Sair
        </a>
      </div>
    </aside>

    <main class="flex-1 p-8">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h1 class="text-4xl font-bold">Dashboard</h1>
          <p class="text-white/60 mt-1">Resumo de vendas (OT + FR) e faturação com IVA (Somente FR).</p>
        </div>
        <div class="text-white/70 text-sm">
          Logado como: <span class="font-semibold text-white">{{ user }}</span>
        </div>
      </div>

      <!-- Filtro por data -->
      <div class="mt-6 p-5 rounded-2xl bg-white/5 border border-white/10">
        <div class="flex flex-col md:flex-row md:items-center gap-3">
          <div class="flex-1">
            <div class="text-sm text-white/70 mb-1">Buscar por data</div>
            <input id="day" type="date" value="{{ day }}" class="w-full md:w-72 px-3 py-2 rounded-xl bg-white/10 border border-white/10 text-white"/>
          </div>
          <div class="pt-5 md:pt-0">
            <button id="btnApplyDay" class="px-4 py-2 rounded-xl bg-purple-600 hover:bg-purple-700 font-semibold">
              Buscar
            </button>
          </div>
          <div class="text-xs text-white/50 md:ml-auto">
            Dica: selecione um dia para ver “Vendas Hoje” daquele dia.
          </div>
        </div>
      </div>

      <!-- Cards -->
      <div class="mt-6 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-sm text-white/60">Vendas Hoje</div>
          <div id="salesDayAll" class="text-3xl font-bold mt-2">—</div>
          <div class="text-xs text-white/50 mt-1">OT + FR</div>
        </div>

        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-sm text-white/60">Vendas Hoje (com IVA)</div>
          <div id="salesDayFR" class="text-3xl font-bold mt-2 text-emerald-300">—</div>
          <div class="text-xs text-white/50 mt-1">Somente FR</div>
        </div>

        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-sm text-white/60">Vendas do Mês</div>
          <div id="salesMonthAll" class="text-3xl font-bold mt-2">—</div>
          <div class="text-xs text-white/50 mt-1">OT + FR</div>
        </div>

        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-sm text-white/60">Vendas do Mês (com IVA)</div>
          <div id="salesMonthFR" class="text-3xl font-bold mt-2 text-emerald-300">—</div>
          <div class="text-xs text-white/50 mt-1">Somente FR</div>
        </div>
      </div>

      <!-- Comparação mês atual vs mês anterior -->
      <div class="mt-6 grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="flex items-center justify-between">
            <div>
              <div class="text-lg font-bold">Comparação do Mês (OT + FR)</div>
              <div class="text-sm text-white/60">Mês atual vs mês anterior</div>
            </div>
            <div id="pctAll" class="text-lg font-bold">—</div>
          </div>

          <div class="mt-4">
            <div class="text-xs text-white/50">Mês anterior</div>
            <div class="h-3 rounded-full bg-white/10 overflow-hidden">
              <div id="barPrevAll" class="h-full bg-white/30 w-0"></div>
            </div>
            <div id="prevAll" class="text-sm mt-1 text-white/70">—</div>
          </div>

          <div class="mt-4">
            <div class="text-xs text-white/50">Mês atual</div>
            <div class="h-3 rounded-full bg-white/10 overflow-hidden">
              <div id="barCurAll" class="h-full bg-purple-500/70 w-0"></div>
            </div>
            <div id="curAll" class="text-sm mt-1 text-white/70">—</div>
          </div>
        </div>

        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="flex items-center justify-between">
            <div>
              <div class="text-lg font-bold">Comparação do Mês (Somente FR)</div>
              <div class="text-sm text-white/60">Mês atual vs mês anterior</div>
            </div>
            <div id="pctFR" class="text-lg font-bold">—</div>
          </div>

          <div class="mt-4">
            <div class="text-xs text-white/50">Mês anterior</div>
            <div class="h-3 rounded-full bg-white/10 overflow-hidden">
              <div id="barPrevFR" class="h-full bg-white/30 w-0"></div>
            </div>
            <div id="prevFR" class="text-sm mt-1 text-white/70">—</div>
          </div>

          <div class="mt-4">
            <div class="text-xs text-white/50">Mês atual</div>
            <div class="h-3 rounded-full bg-white/10 overflow-hidden">
              <div id="barCurFR" class="h-full bg-emerald-500/70 w-0"></div>
            </div>
            <div id="curFR" class="text-sm mt-1 text-white/70">—</div>
          </div>
        </div>
      </div>

      <div class="mt-6 text-xs text-white/40">
        Sugestões futuras: gráfico diário do mês (linha), split Cartão x Dinheiro, e top 5 serviços.
      </div>
    </main>
  </div>

<script>
  function eur(v){
    try{
      const n = Number(v || 0);
      return n.toLocaleString('pt-PT', { style:'currency', currency:'EUR' });
    }catch(e){ return '0,00 €'; }
  }

  function setBar(el, cur, max){
    const pct = max <= 0 ? 0 : Math.round((cur / max) * 100);
    el.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  async function loadStats(){
    const day = document.getElementById('day')?.value || '';
    const resp = await fetch(`/admin/stats?day=${encodeURIComponent(day)}`);
    const data = await resp.json();

    document.getElementById('salesDayAll').textContent = eur(data.sales_day_all);
    document.getElementById('salesDayFR').textContent  = eur(data.sales_day_fr);
    document.getElementById('salesMonthAll').textContent = eur(data.sales_month_all);
    document.getElementById('salesMonthFR').textContent  = eur(data.sales_month_fr);

    document.getElementById('prevAll').textContent = `Mês anterior: ${eur(data.sales_prev_month_all)}`;
    document.getElementById('curAll').textContent  = `Mês atual: ${eur(data.sales_month_all)}`;

    document.getElementById('prevFR').textContent = `Mês anterior: ${eur(data.sales_prev_month_fr)}`;
    document.getElementById('curFR').textContent  = `Mês atual: ${eur(data.sales_month_fr)}`;

    const pctAll = Number(data.pct_month_all || 0);
    const pctFR  = Number(data.pct_month_fr || 0);

    const pctAllEl = document.getElementById('pctAll');
    const pctFREl  = document.getElementById('pctFR');

    pctAllEl.textContent = (pctAll >= 0 ? '▲ ' : '▼ ') + Math.abs(pctAll).toFixed(1) + '%';
    pctAllEl.className = 'text-lg font-bold ' + (pctAll >= 0 ? 'text-emerald-300' : 'text-red-300');

    pctFREl.textContent = (pctFR >= 0 ? '▲ ' : '▼ ') + Math.abs(pctFR).toFixed(1) + '%';
    pctFREl.className = 'text-lg font-bold ' + (pctFR >= 0 ? 'text-emerald-300' : 'text-red-300');

    const maxAll = Math.max(Number(data.sales_prev_month_all||0), Number(data.sales_month_all||0));
    const maxFR  = Math.max(Number(data.sales_prev_month_fr||0), Number(data.sales_month_fr||0));

    setBar(document.getElementById('barPrevAll'), Number(data.sales_prev_month_all||0), maxAll);
    setBar(document.getElementById('barCurAll'),  Number(data.sales_month_all||0), maxAll);

    setBar(document.getElementById('barPrevFR'), Number(data.sales_prev_month_fr||0), maxFR);
    setBar(document.getElementById('barCurFR'),  Number(data.sales_month_fr||0), maxFR);
  }

  document.getElementById('btnApplyDay')?.addEventListener('click', () => {
    const day = document.getElementById('day')?.value || '';
    window.location.href = `/admin?day=${encodeURIComponent(day)}`;
  });

  loadStats();
</script>
</body>
</html>"""

# -----------------------------------------------------------------------------
# Controle de Caixa (Admin)
# -----------------------------------------------------------------------------
CONTROLE_CAIXA_HTML = """
<!DOCTYPE html>
<html lang="pt-PT">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Controle de Caixa</title>
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
        <a href="/admin" class="block px-3 py-2 rounded-xl hover:bg-white/10">
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

        <a href="/controle-caixa" class="block px-3 py-2 rounded-xl bg-white/10 hover:bg-white/15">
          💳 <span class="ml-2">Controle de Caixa</span>
          <div class="text-xs text-white/60 ml-6">Cartão, NIF e exportação</div>
        </a>

        <a href="/exportacoes-caixa" class="block px-3 py-2 rounded-xl hover:bg-white/10">
          📤 <span class="ml-2">Exportações Caixa</span>
          <div class="text-xs text-white/60 ml-6">CSV mensal detalhado</div>
        </a>

      </nav>

      <div class="mt-6">
        <a href="/logout?next=/login" class="block text-center px-4 py-2 rounded-xl bg-red-500/20 hover:bg-red-500/30 border border-red-500/30">
          Sair
        </a>
      </div>
    </aside>

    <main class="flex-1 p-8">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h1 class="text-3xl font-bold">Controle de Caixa</h1>
          <p class="text-white/60 mt-1">Marque se foi pago por cartão e acompanhe totais por dia/mês.</p>
        </div>
        <div class="text-white/70 text-sm">
          Logado como: <span class="font-semibold text-white">{{ user }}</span>
        </div>
      </div>

      <!-- Filtros -->
      <div class="mt-6 grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/70 text-sm font-semibold mb-2">Filtrar por dia</div>
          <div class="flex items-center gap-2">
            <input id="day-filter" type="date" value="{{ day }}" class="px-3 py-2 rounded-xl bg-white/10 border border-white/10 text-white w-full"/>
            <button id="btn-day" class="px-4 py-2 rounded-xl bg-purple-600 hover:bg-purple-700 text-white font-semibold">Aplicar</button>
          </div>
        </div>

        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/70 text-sm font-semibold mb-2">Exportar CSV mensal</div>
          <div class="flex items-center gap-2">
            <input id="month-filter" type="month" value="{{ month }}" class="px-3 py-2 rounded-xl bg-white/10 border border-white/10 text-white w-full"/>
            <button id="btn-month" class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-semibold">Baixar</button>
          </div>
          <div class="text-xs text-white/50 mt-2">Baixa todos os pedidos do mês em CSV.</div>
        </div>

        <div class="p-5 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-white/70 text-sm font-semibold mb-2">Totais do dia ({{ day }})</div>
          <div class="grid grid-cols-2 gap-3">
            <div class="p-4 rounded-xl bg-white/5 border border-white/10">
              <div class="text-xs text-white/60">Total Cartão</div>
              <div id="tot-card" class="text-xl font-extrabold">—</div>
            </div>
            <div class="p-4 rounded-xl bg-white/5 border border-white/10">
              <div class="text-xs text-white/60">Total Dinheiro</div>
              <div id="tot-cash" class="text-xl font-extrabold">—</div>
            </div>
            <div class="p-4 rounded-xl bg-white/5 border border-white/10">
              <div class="text-xs text-white/60">Total Com NIF</div>
              <div id="tot-nif" class="text-xl font-extrabold">—</div>
            </div>
            <div class="p-4 rounded-xl bg-white/5 border border-white/10">
              <div class="text-xs text-white/60">Total Sem NIF</div>
              <div id="tot-no-nif" class="text-xl font-extrabold">—</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Tabela -->
      <div class="mt-6 p-6 rounded-2xl bg-white/5 border border-white/10">
        <div class="flex items-center justify-between">
          <div>
            <div class="text-lg font-bold">Pedidos do dia</div>
            <div class="text-sm text-white/60">Marque “Cartão” e/ou apague uma nota (pedido) se necessário.</div>
          </div>
          <div id="save-hint" class="text-sm text-white/60"></div>
        </div>

        <div class="mt-4 overflow-auto">
          <table class="min-w-full text-sm">
            <thead class="text-white/70">
              <tr class="border-b border-white/10">
                <th class="text-left py-2 pr-4">Pedido</th>
                <th class="text-left py-2 pr-4">Hora</th>
                <th class="text-left py-2 pr-4">Cliente</th>
                <th class="text-left py-2 pr-4">NIF</th>
                <th class="text-left py-2 pr-4">Total</th>
                <th class="text-left py-2 pr-4">Cartão</th>
                <th class="text-left py-2 pr-4">Ações</th>
              </tr>
            </thead>
            <tbody id="rows" class="text-white/90"></tbody>
          </table>
        </div>
      </div>
    </main>
  </div>

<script>
  const DAY = "{{ day }}";
  function eur(v){
    try { return new Intl.NumberFormat('pt-PT', { style: 'currency', currency: 'EUR' }).format(Number(v||0)); }
    catch(e){ return "€ " + (v||0); }
  }

  function setTotals(t){
    document.getElementById("tot-card").textContent = eur(t.total_card);
    document.getElementById("tot-cash").textContent = eur(t.total_cash);
    document.getElementById("tot-nif").textContent = eur(t.total_with_nif);
    document.getElementById("tot-no-nif").textContent = eur(t.total_without_nif);
  }

  function rowHtml(p){
    const nifBadge = p.include_nif ? '<span class="px-2 py-0.5 rounded-full text-xs bg-emerald-500/15 border border-emerald-500/20 text-emerald-200">Com NIF</span>'
                                  : '<span class="px-2 py-0.5 rounded-full text-xs bg-white/10 border border-white/10 text-white/70">Sem NIF</span>';
    return `
      <tr class="border-b border-white/5 hover:bg-white/5" data-pedido="${p.id_pedido}">
        <td class="py-3 pr-4 font-semibold">#${p.id_pedido}</td>
        <td class="py-3 pr-4 text-white/70">${p.time || ""}</td>
        <td class="py-3 pr-4">${p.client_name || ""}</td>
        <td class="py-3 pr-4">${nifBadge}</td>
        <td class="py-3 pr-4 font-semibold">${eur(p.preco_total)}</td>
        <td class="py-3 pr-4">
          <label class="inline-flex items-center gap-2 select-none">
            <input class="pay-card" type="checkbox" ${p.paid_by_card ? "checked" : ""}/>
            <span class="text-white/70">Cartão</span>
          </label>
        </td>
        <td class="py-3 pr-4">
          <button class="btn-delete px-3 py-1.5 rounded-xl bg-red-500/20 hover:bg-red-500/30 border border-red-500/30 text-red-200 font-semibold">
            Apagar
          </button>
        </td>
      </tr>
    `;
  }

  async function loadDay(){
    const r = await fetch(`/controle-caixa/data?day=${encodeURIComponent(DAY)}`, { credentials: "same-origin" });
    const data = await r.json();
    const tbody = document.getElementById("rows");
    tbody.innerHTML = (data.rows || []).map(rowHtml).join("");
    setTotals(data.totals || {});
  }

  async function savePaidByCard(id_pedido, value){
    const hint = document.getElementById("save-hint");
    hint.textContent = "A guardar...";
    try{
      const r = await fetch(`/pedidos/${id_pedido}/payment`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paid_by_card: !!value })
      });
      const data = await r.json().catch(()=> ({}));
      if(!r.ok) throw new Error(data.error || "Falha ao salvar");
      hint.textContent = "Guardado ✓";
      await refreshTotals();
      setTimeout(()=> hint.textContent = "", 900);
    }catch(e){
      console.error(e);
      hint.textContent = "Erro ao guardar";
      alert(e.message || "Erro ao guardar");
    }
  }

  async function deletePedido(id_pedido){
    if(!confirm(`Tem certeza que deseja apagar o pedido #${id_pedido}?\\n\\nIsso remove também os serviços do pedido.`)) return;
    const hint = document.getElementById("save-hint");
    hint.textContent = "A apagar...";
    try{
      const r = await fetch(`/pedidos/${id_pedido}`, { method: "DELETE" });
      const data = await r.json().catch(()=> ({}));
      if(!r.ok) throw new Error(data.error || "Falha ao apagar");
      document.querySelector(`tr[data-pedido="${id_pedido}"]`)?.remove();
      hint.textContent = "Apagado ✓";
      await refreshTotals();
      setTimeout(()=> hint.textContent = "", 900);
    }catch(e){
      console.error(e);
      hint.textContent = "Erro ao apagar";
      alert(e.message || "Erro ao apagar");
    }
  }

  async function refreshTotals(){
    const r = await fetch(`/controle-caixa/totals?day=${encodeURIComponent(DAY)}`, { credentials: "same-origin" });
    const data = await r.json().catch(()=>({}));
    setTotals(data || {});
  }

  document.addEventListener("change", (e) => {
    const t = e.target;
    if(t.classList.contains("pay-card")){
      const tr = t.closest("tr");
      const id = tr?.getAttribute("data-pedido");
      if(!id) return;
      savePaidByCard(id, t.checked);
    }
  });

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-delete");
    if(btn){
      const tr = btn.closest("tr");
      const id = tr?.getAttribute("data-pedido");
      if(id) deletePedido(id);
    }
  });

  document.getElementById("btn-day")?.addEventListener("click", () => {
    const v = document.getElementById("day-filter")?.value;
    if(!v) return;
    window.location.href = `/controle-caixa?day=${encodeURIComponent(v)}`;
  });

  document.getElementById("btn-month")?.addEventListener("click", () => {
    const v = document.getElementById("month-filter")?.value; // YYYY-MM
    if(!v) return;
    window.location.href = `/controle-caixa/export?month=${encodeURIComponent(v)}`;
  });

  loadDay();
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Exportações de Caixa (Admin)
# -----------------------------------------------------------------------------
EXPORTACOES_CAIXA_HTML = """
<!DOCTYPE html>
<html lang="pt-PT">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Exportações Caixa</title>
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
        <a href="/admin" class="block px-3 py-2 rounded-xl hover:bg-white/10">
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

        <a href="/controle-caixa" class="block px-3 py-2 rounded-xl hover:bg-white/10">
          💳 <span class="ml-2">Controle de Caixa</span>
          <div class="text-xs text-white/60 ml-6">Cartão, NIF e exportação</div>
        </a>

        <a href="/exportacoes-caixa" class="block px-3 py-2 rounded-xl bg-white/10 hover:bg-white/15">
          📤 <span class="ml-2">Exportações Caixa</span>
          <div class="text-xs text-white/60 ml-6">CSV mensal detalhado</div>
        </a>
      </nav>

      <div class="mt-6">
        <a href="/logout?next=/login" class="block text-center px-4 py-2 rounded-xl bg-red-500/20 hover:bg-red-500/30 border border-red-500/30">
          Sair
        </a>
      </div>
    </aside>

    <main class="flex-1 p-8">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h1 class="text-3xl font-bold">Exportações Caixa</h1>
          <p class="text-white/60 mt-1">Exportar CSV por mês (resumo ou detalhado por serviços).</p>
        </div>
        <div class="text-white/70 text-sm">
          Logado como: <span class="font-semibold text-white">{{ user }}</span>
        </div>
      </div>

      <div class="mt-6 grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-lg font-bold">Escolher mês</div>
          <div class="text-sm text-white/60 mt-1">Selecione o mês e exporte o CSV.</div>

          <div class="mt-4 flex items-center gap-2">
            <input id="month" type="month" value="{{ month }}" class="px-3 py-2 rounded-xl bg-white/10 border border-white/10 text-white w-full"/>
          </div>

          <div class="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
            <button id="btn-resumo" class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-semibold">
              Baixar CSV (Resumo)
            </button>
            <button id="btn-detalhado" class="px-4 py-2 rounded-xl bg-purple-600 hover:bg-purple-700 text-white font-semibold">
              Baixar CSV (Detalhado)
            </button>
          </div>

          <div class="mt-4 text-xs text-white/50 space-y-1">
            <div><b>Resumo</b>: 1 linha por pedido (como já existe no Controle de Caixa).</div>
            <div><b>Detalhado</b>: 1 linha por pedido com coluna “Serviços” (lista/descrição), + Dia, Nome, NIF, Total.</div>
          </div>
        </div>

        <div class="p-6 rounded-2xl bg-white/5 border border-white/10">
          <div class="text-lg font-bold">Campos do CSV detalhado</div>
          <div class="mt-3 text-sm text-white/70">
            <ul class="list-disc ml-6 space-y-1">
              <li><b>dia</b> (data do pedido)</li>
              <li><b>nome</b> (cliente)</li>
              <li><b>nif</b> (apenas se “Com NIF” e cliente tiver NIF)</li>
              <li><b>servicos</b> (descrição dos serviços do pedido)</li>
              <li><b>valor_total</b> (total do pedido)</li>
            </ul>
          </div>
        </div>
      </div>
    </main>
  </div>

<script>
  document.getElementById("btn-resumo")?.addEventListener("click", () => {
    const m = document.getElementById("month")?.value;
    if(!m) return;
    window.location.href = `/controle-caixa/export?month=${encodeURIComponent(m)}`;
  });

  document.getElementById("btn-detalhado")?.addEventListener("click", () => {
    const m = document.getElementById("month")?.value;
    if(!m) return;
    window.location.href = `/exportacoes-caixa/detalhado?month=${encodeURIComponent(m)}`;
  });
</script>
</body>
</html>
"""

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

    return (
        render_template_string(
            LOGIN_HTML,
            error="Usuário ou senha inválidos",
            next_url=next_url,
            username=username,
        ),
        401,
    )


@app.get("/logout")
def logout():
    session.clear()
    next_url = request.args.get("next") or "/login"
    return redirect(next_url)


@app.get("/admin")
@login_required(["admin"])
def admin_dashboard():
    day_str = (request.args.get("day") or "").strip()
    if day_str:
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except Exception:
            day = date.today()
    else:
        day = date.today()

    return render_template_string(ADMIN_HTML, user=session.get("user"), day=day.strftime("%Y-%m-%d"))


@app.get("/admin/stats")
@login_required(["admin"])
@handle_errors
def admin_stats():
    # Permite filtrar por dia (YYYY-MM-DD)
    day_str = (request.args.get("day") or "").strip()
    if day_str:
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except Exception:
            day = date.today()
    else:
        day = date.today()

    start_month = day.replace(day=1)

    # Próximo mês (para fechar o intervalo [start_month, next_month))
    if start_month.month == 12:
        next_month = date(start_month.year + 1, 1, 1)
    else:
        next_month = date(start_month.year, start_month.month + 1, 1)

    # Mês anterior
    if start_month.month == 1:
        prev_month_start = date(start_month.year - 1, 12, 1)
    else:
        prev_month_start = date(start_month.year, start_month.month - 1, 1)
    prev_month_end = start_month  # exclusivo

    next7 = day + timedelta(days=7)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Indicadores operacionais (mantidos para outras telas/uso futuro)
            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos ps
                JOIN pedidos p ON p.id_pedido = ps.id_pedido
                WHERE p.data_prevista = %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                """,
                (day,),
            )
            pending_today = cur.fetchone()["c"]

            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos
                WHERE status = 'Concluído'
                  AND data_conclusao::date = %s
                """,
                (day,),
            )
            completed_today = cur.fetchone()["c"]

            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedido_servicos ps
                JOIN pedidos p ON p.id_pedido = ps.id_pedido
                WHERE p.data_prevista < %s
                  AND COALESCE(ps.status,'') <> 'Concluído'
                """,
                (day,),
            )
            overdue = cur.fetchone()["c"]

            cur.execute(
                """
                SELECT COUNT(*)::int AS c
                FROM pedidos
                WHERE data_entrada::date = %s
                """,
                (day,),
            )
            orders_today = cur.fetchone()["c"]

            # Vendas (OT+FR) e "Somente FR" (include_nif=true)
            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::numeric AS v
                FROM pedidos
                WHERE data_entrada::date = %s
                """,
                (day,),
            )
            sales_day_all = float(cur.fetchone()["v"] or 0)

            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::numeric AS v
                FROM pedidos
                WHERE data_entrada::date = %s
                  AND include_nif = true
                """,
                (day,),
            )
            sales_day_fr = float(cur.fetchone()["v"] or 0)

            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::numeric AS v
                FROM pedidos
                WHERE data_entrada::date >= %s
                  AND data_entrada::date < %s
                """,
                (start_month, next_month),
            )
            sales_month_all = float(cur.fetchone()["v"] or 0)

            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::numeric AS v
                FROM pedidos
                WHERE data_entrada::date >= %s
                  AND data_entrada::date < %s
                  AND include_nif = true
                """,
                (start_month, next_month),
            )
            sales_month_fr = float(cur.fetchone()["v"] or 0)

            # Mês anterior (comparação)
            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::numeric AS v
                FROM pedidos
                WHERE data_entrada::date >= %s
                  AND data_entrada::date < %s
                """,
                (prev_month_start, prev_month_end),
            )
            sales_prev_month_all = float(cur.fetchone()["v"] or 0)

            cur.execute(
                """
                SELECT COALESCE(SUM(preco_total),0)::numeric AS v
                FROM pedidos
                WHERE data_entrada::date >= %s
                  AND data_entrada::date < %s
                  AND include_nif = true
                """,
                (prev_month_start, prev_month_end),
            )
            sales_prev_month_fr = float(cur.fetchone()["v"] or 0)

    def pct_change(cur_v: float, prev_v: float) -> float:
        if prev_v == 0:
            return 0.0 if cur_v == 0 else 100.0
        return ((cur_v - prev_v) / prev_v) * 100.0

    return jsonify(
        {
            "day": day.strftime("%Y-%m-%d"),
            "month": start_month.strftime("%Y-%m"),
            "pending_today": pending_today,
            "completed_today": completed_today,
            "overdue": overdue,
            "orders_today": orders_today,

            "sales_day_all": sales_day_all,
            "sales_day_fr": sales_day_fr,
            "sales_month_all": sales_month_all,
            "sales_month_fr": sales_month_fr,
            "sales_prev_month_all": sales_prev_month_all,
            "sales_prev_month_fr": sales_prev_month_fr,
            "pct_month_all": pct_change(sales_month_all, sales_prev_month_all),
            "pct_month_fr": pct_change(sales_month_fr, sales_prev_month_fr),
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
    return render_template("Gerenciamento.html")


@app.get("/costureiras")
@login_required(["admin", "costureira"])
@handle_errors
def serve_seamstress_page():
    return render_template("costureiras.html")


# -----------------------------------------------------------------------------
# APIs do Painel Costureiras
# -----------------------------------------------------------------------------
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



# -----------------------------------------------------------------------------
# Controle de Caixa - Páginas e APIs
# -----------------------------------------------------------------------------
def _parse_day(s: str | None) -> date:
    if not s:
        return date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return date.today()


def _month_range(month_str: str) -> tuple[date, date]:
    # month_str: YYYY-MM
    y, m = month_str.split("-", 1)
    y = int(y)
    m = int(m)
    start = date(y, m, 1)
    if m == 12:
        end = date(y + 1, 1, 1)
    else:
        end = date(y, m + 1, 1)
    return start, end


def _caixa_totals_for_day(day: date) -> dict:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN paid_by_card THEN preco_total ELSE 0 END),0)::float AS total_card,
                  COALESCE(SUM(CASE WHEN NOT paid_by_card THEN preco_total ELSE 0 END),0)::float AS total_cash,
                  COALESCE(SUM(CASE WHEN include_nif THEN preco_total ELSE 0 END),0)::float AS total_with_nif,
                  COALESCE(SUM(CASE WHEN NOT include_nif THEN preco_total ELSE 0 END),0)::float AS total_without_nif
                FROM pedidos
                WHERE data_entrada::date = %s
                """,
                (day,),
            )
            r = cur.fetchone() or {}
    return {
        "total_card": float(r.get("total_card") or 0),
        "total_cash": float(r.get("total_cash") or 0),
        "total_with_nif": float(r.get("total_with_nif") or 0),
        "total_without_nif": float(r.get("total_without_nif") or 0),
    }


@app.get("/controle-caixa")
@login_required(["admin"])
def controle_caixa_page():
    day = _parse_day(request.args.get("day"))
    # mês default = mês do day
    month = f"{day.year:04d}-{day.month:02d}"
    return render_template_string(
        CONTROLE_CAIXA_HTML,
        user=session.get("user"),
        day=day.strftime("%Y-%m-%d"),
        month=month,
    )


@app.get("/exportacoes-caixa")
@login_required(["admin"])
def exportacoes_caixa_page():
    today = date.today()
    month = request.args.get("month") or f"{today.year:04d}-{today.month:02d}"
    return render_template_string(EXPORTACOES_CAIXA_HTML, user=session.get("user"), month=month)


@app.get("/controle-caixa/data")
@login_required(["admin"])
@handle_errors
def controle_caixa_data():
    day = _parse_day(request.args.get("day"))
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  p.id_pedido,
                  to_char(p.data_entrada, 'HH24:MI') AS time,
                  p.preco_total,
                  p.include_nif,
                  p.paid_by_card,
                  c.name AS client_name
                FROM pedidos p
                JOIN clients c ON c.id_client = p.id_client
                WHERE p.data_entrada::date = %s
                ORDER BY p.id_pedido DESC
                """,
                (day,),
            )
            rows = cur.fetchall() or []

    return jsonify({"rows": rows, "totals": _caixa_totals_for_day(day)}), 200


@app.get("/controle-caixa/totals")
@login_required(["admin"])
@handle_errors
def controle_caixa_totals():
    day = _parse_day(request.args.get("day"))
    return jsonify(_caixa_totals_for_day(day)), 200


@app.get("/controle-caixa/export")
@login_required(["admin"])
@handle_errors
def controle_caixa_export():
    month = (request.args.get("month") or "").strip()  # YYYY-MM
    if not month or len(month) != 7 or "-" not in month:
        # fallback: mês atual
        today = date.today()
        month = f"{today.year:04d}-{today.month:02d}"

    start, end = _month_range(month)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  p.id_pedido,
                  to_char(p.data_entrada, 'YYYY-MM-DD HH24:MI:SS') AS data_entrada,
                  c.name AS cliente,
                  c.phone AS telefone,
                  p.include_nif,
                  p.paid_by_card,
                  p.preco_total,
                  p.desconto,
                  COALESCE(p.observacoes,'') AS observacoes
                FROM pedidos p
                JOIN clients c ON c.id_client = p.id_client
                WHERE p.data_entrada::date >= %s
                  AND p.data_entrada::date < %s
                ORDER BY p.id_pedido ASC
                """,
                (start, end),
            )
            rows = cur.fetchall() or []

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "id_pedido",
            "data_entrada",
            "cliente",
            "telefone",
            "include_nif",
            "paid_by_card",
            "preco_total",
            "desconto",
            "observacoes",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.get("id_pedido"),
                r.get("data_entrada"),
                r.get("cliente"),
                r.get("telefone"),
                "1" if r.get("include_nif") else "0",
                "1" if r.get("paid_by_card") else "0",
                f"{float(r.get('preco_total') or 0):.2f}".replace(".", ","),
                f"{float(r.get('desconto') or 0):.2f}".replace(".", ","),
                r.get("observacoes", ""),
            ]
        )

    csv_data = output.getvalue().encode("utf-8-sig")  # BOM para Excel PT
    filename = f"controle_caixa_{month}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )






@app.get("/exportacoes-caixa/detalhado")
@login_required(["admin"])
@handle_errors
def exportacoes_caixa_detalhado():
    month = (request.args.get("month") or "").strip()  # YYYY-MM
    if not month or len(month) != 7 or "-" not in month:
        today = date.today()
        month = f"{today.year:04d}-{today.month:02d}"

    start, end = _month_range(month)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id_pedido,
                    p.data_entrada::date AS dia,
                    c.name AS nome,
                    CASE
                        WHEN p.include_nif = true
                             AND c.nif IS NOT NULL
                             AND c.nif <> ''
                        THEN c.nif
                        ELSE ''
                    END AS nif,
                    p.paid_by_card,
                    STRING_AGG(
                        s.name || ' x' || COALESCE(ps.quantity,1)::text ||
                        CASE
                            WHEN ps.description IS NOT NULL
                                 AND ps.description <> ''
                            THEN ' (' || ps.description || ')'
                            ELSE ''
                        END,
                        ' | '
                        ORDER BY s.name
                    ) AS servicos,
                    p.preco_total
                FROM pedidos p
                JOIN clients c ON c.id_client = p.id_client
                LEFT JOIN pedido_servicos ps ON ps.id_pedido = p.id_pedido
                LEFT JOIN services s ON s.id_service = ps.id_service
                WHERE p.data_entrada::date >= %s
                  AND p.data_entrada::date <  %s
                GROUP BY
                    p.id_pedido,
                    p.data_entrada::date,
                    c.name,
                    c.nif,
                    p.include_nif,
                    p.paid_by_card,
                    p.preco_total
                ORDER BY
                    p.data_entrada::date ASC,
                    p.id_pedido ASC
                """,
                (start, end),
            )
            rows = cur.fetchall() or []

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["dia", "nome", "nif", "cartao", "servicos", "valor_total_eur", "iva_23", "moeda"])

    for r in rows:
        total = Decimal(str(r.get("preco_total") or 0))
        iva = (total - (total / Decimal("1.23"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        writer.writerow(
            [
                r.get("dia").strftime("%Y-%m-%d") if r.get("dia") else "",
                r.get("nome") or "",
                r.get("nif") or "",
                "1" if r.get("paid_by_card") else "0",
                r.get("servicos") or "",
                f'="{total:.2f}"',
                f'="{iva:.2f}"',
                "EUR",
            ]
        )



    csv_data = output.getvalue().encode("utf-8-sig")
    filename = f"export_caixa_detalhado_{month}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



@app.put("/pedidos/<int:pedido_id>/payment")
@login_required(["admin"])
@handle_errors
def set_payment(pedido_id: int):
    data = request.get_json(force=True) or {}
    paid_by_card = bool(data.get("paid_by_card") or data.get("paidByCard") or False)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pedidos SET paid_by_card = %s WHERE id_pedido = %s",
                (paid_by_card, pedido_id),
            )
            updated = cur.rowcount
        conn.commit()

    if updated == 0:
        return jsonify({"error": "Pedido não encontrado."}), 404
    return jsonify({"ok": True, "pedido_id": pedido_id, "paid_by_card": paid_by_card}), 200


@app.delete("/pedidos/<int:pedido_id>")
@login_required(["admin"])
@handle_errors
def delete_pedido(pedido_id: int):
    # Apaga primeiro filhos para não violar FK
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pedido_servicos WHERE id_pedido = %s", (pedido_id,))
            cur.execute("DELETE FROM pedidos WHERE id_pedido = %s", (pedido_id,))
            deleted = cur.rowcount
        conn.commit()

    if deleted == 0:
        return jsonify({"error": "Pedido não encontrado."}), 404
    return jsonify({"ok": True, "pedido_id": pedido_id}), 200


# -----------------------------------------------------------------------------
# API - Clients
# -----------------------------------------------------------------------------
@app.get("/clients/<phone>")
@login_required(["admin", "caixa"])
@handle_errors
def get_client_by_phone(phone):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id_client, name, nif FROM clients WHERE phone = %s", (phone,))
            row = cur.fetchone()

    if row:
        return jsonify({"id": row["id_client"], "name": row["name"], "nif": row["nif"]}), 200
    return jsonify({"error": "Cliente não encontrado."}), 404


@app.get("/clients/search")
@login_required(["admin", "caixa"])
@handle_errors
def search_clients():
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

    return (
        jsonify(
            {
                "results": [
                    {"id": r["id_client"], "name": r["name"], "phone": r["phone"], "nif": r["nif"]}
                    for r in rows
                ]
            }
        ),
        200,
    )


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
            cur.execute("UPDATE clients SET nif = %s WHERE id_client = %s", (nif, client_id))
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

    return jsonify({"id": row["id_client"], "name": row["name"], "phone": row["phone"], "nif": row["nif"]}), 200


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
            cur.execute("SELECT id_service, name, price, category FROM services ORDER BY category, name")
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
            cur.execute("INSERT INTO seamstresses (name) VALUES (%s) RETURNING id_seamstress", (name,))
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
            cur.execute("UPDATE seamstresses SET name=%s WHERE id_seamstress=%s", (name, seamstress_id))
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


# -----------------------------------------------------------------------------
# Debug (protegido)
# -----------------------------------------------------------------------------
@app.get("/debug-env")
@login_required(["admin"])
def debug_env():
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
