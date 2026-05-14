from flask import Flask, request, jsonify, render_template_string, abort
import os, json
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "veritas2026")
THEMEMBERS_TOKEN = os.environ.get("THEMEMBERS_TOKEN", "")

# ── Banco ────────────────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vendas (
                    id          SERIAL PRIMARY KEY,
                    plataforma  TEXT,
                    produto     TEXT,
                    valor       NUMERIC(10,2),
                    moeda       TEXT DEFAULT 'BRL',
                    status      TEXT,
                    cliente_nome TEXT,
                    cliente_email TEXT,
                    utm_source   TEXT,
                    utm_medium   TEXT,
                    utm_campaign TEXT,
                    utm_content  TEXT,
                    utm_term     TEXT,
                    order_id     TEXT,
                    payload_raw  TEXT,
                    criado_em    TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()

init_db()

# ── Helpers ──────────────────────────────────────────────────────────────────

def extrair_utms(data: dict) -> dict:
    """Tenta extrair UTMs de qualquer estrutura de payload."""
    campos = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]
    utms = {}
    for c in campos:
        # Busca direta
        val = data.get(c) or data.get(c.upper())
        # Busca em sub-dicts comuns
        if not val:
            for sub in ["tracking", "utm", "metadata", "custom_fields", "order"]:
                if isinstance(data.get(sub), dict):
                    val = data[sub].get(c) or data[sub].get(c.upper())
                    if val:
                        break
        utms[c] = val or ""
    return utms

# ── Webhook TheMembers ───────────────────────────────────────────────────────

def extrair_utms_themembers(utms_raw) -> dict:
    """Extrai UTMs do campo utms da TheMembers (pode ser lista ou dict)."""
    campos = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]
    result = {c: "" for c in campos}
    if isinstance(utms_raw, dict):
        for c in campos:
            result[c] = utms_raw.get(c) or ""
    elif isinstance(utms_raw, list):
        for item in utms_raw:
            if isinstance(item, dict):
                for c in campos:
                    if not result[c]:
                        result[c] = item.get(c) or ""
    return result

@app.route("/webhook/themembers", methods=["POST"])
def webhook_themembers():
    try:
        # Validar token de segurança
        if THEMEMBERS_TOKEN:
            sig = request.headers.get("x-signature", "")
            if sig != THEMEMBERS_TOKEN:
                app.logger.warning(f"Token inválido: {sig}")
                return jsonify({"ok": False, "erro": "token inválido"}), 401

        body = request.get_json(force=True) or {}
        payload = body.get("payload") or body
        event = payload.get("event") or payload.get("tags", {}).get("event") or ""
        data = payload.get("data") or {}

        # Só processa eventos de venda aprovada
        status_map = {
            "transaction.approved": "pago",
            "order.completed": "pago",
            "release.access": "pago",
            "transaction.refunded": "reembolsado",
            "transaction.charged_back": "chargeback",
            "transaction.failed": "recusado",
            "order.canceled": "cancelado",
            "order.expired": "expirado",
        }
        status = status_map.get(event, event)

        # Extrair order (pode estar em data.order ou direto em data)
        order = data.get("order") or {}
        cliente = (order.get("customer") or data.get("customer") or
                   data.get("subscriber") or {})
        if isinstance(cliente, str):
            cliente = {}

        # Produto
        main_product = (order.get("main_product") or data.get("main_product") or
                        data.get("product") or {})
        produto = (main_product.get("title") or main_product.get("name") or
                   data.get("product", {}).get("title") if isinstance(data.get("product"), dict) else None or "")

        # Valor em centavos → reais
        trans = data.get("transaction") or {}
        valor_cents = (trans.get("amount") or trans.get("total_amount") or
                       order.get("total") or main_product.get("price") or 0)
        try:
            valor = float(valor_cents) / 100
        except:
            valor = 0.0

        # UTMs
        utms_raw = order.get("utms") or data.get("utms") or {}
        utms = extrair_utms_themembers(utms_raw)

        # Order ID
        order_id = str(order.get("id") or data.get("id") or payload.get("id") or "")

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO vendas
                      (plataforma, produto, valor, status, cliente_nome, cliente_email,
                       utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                       order_id, payload_raw, criado_em)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """, (
                "themembers",
                produto,
                valor,
                status,
                cliente.get("name") or cliente.get("full_name") or "",
                cliente.get("email") or "",
                utms["utm_source"], utms["utm_medium"], utms["utm_campaign"],
                utms["utm_content"], utms["utm_term"],
                order_id,
                json.dumps(body, ensure_ascii=False)
            ))
            conn.commit()

        return jsonify({"ok": True}), 200
    except Exception as e:
        app.logger.error(f"Erro webhook themembers: {e}")
        return jsonify({"ok": False, "erro": str(e)}), 500

# Alias para compatibilidade com URL antiga
@app.route("/webhook/thebank", methods=["POST"])
def webhook_thebank():
    return webhook_themembers()

# ── Webhook Kiwify ───────────────────────────────────────────────────────────

@app.route("/webhook/kiwify", methods=["POST"])
def webhook_kiwify():
    try:
        data = request.get_json(force=True) or {}
        utms = extrair_utms(data)

        order = data.get("order") or data
        try:
            valor = float(order.get("amount") or order.get("value") or 0) / 100
        except:
            valor = 0.0

        status_map = {
            "paid": "pago", "approved": "pago",
            "refunded": "reembolsado", "chargedback": "chargeback",
            "abandoned": "abandonado"
        }
        status_raw = (data.get("order_status") or order.get("status") or "").lower()
        status = status_map.get(status_raw, status_raw)

        cliente = order.get("Customer") or order.get("customer") or {}
        if isinstance(cliente, str):
            cliente = {}

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO vendas
                      (plataforma, produto, valor, status, cliente_nome, cliente_email,
                       utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                       order_id, payload_raw, criado_em)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """, (
                "kiwify",
                order.get("product_name") or data.get("product_name") or "",
                valor,
                status,
                cliente.get("full_name") or cliente.get("name") or "",
                cliente.get("email") or "",
                utms["utm_source"], utms["utm_medium"], utms["utm_campaign"],
                utms["utm_content"], utms["utm_term"],
                order.get("order_id") or order.get("id") or data.get("order_id") or "",
                json.dumps(data, ensure_ascii=False)
            ))
            conn.commit()

        return jsonify({"ok": True}), 200
    except Exception as e:
        app.logger.error(f"Erro webhook kiwify: {e}")
        return jsonify({"ok": False, "erro": str(e)}), 500

# ── Health ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ── Dashboard ────────────────────────────────────────────────────────────────

def periodo_filtro(periodo):
    """Retorna cláusula SQL de data conforme período selecionado (PostgreSQL)."""
    if periodo == "hoje":
        return "AND criado_em::date = CURRENT_DATE"
    elif periodo == "7d":
        return "AND criado_em >= NOW() - INTERVAL '7 days'"
    elif periodo == "30d":
        return "AND criado_em >= NOW() - INTERVAL '30 days'"
    return ""  # tudo

@app.route("/dashboard")
def dashboard():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)

    periodo = request.args.get("periodo", "30d")
    filtro = periodo_filtro(periodo)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT * FROM vendas WHERE status = 'pago' {filtro}
                ORDER BY criado_em DESC
            """)
            vendas = cur.fetchall()
            vendas = [dict(v) for v in vendas]
            # Converter datetime para string
            for v in vendas:
                if v.get("criado_em") and not isinstance(v["criado_em"], str):
                    v["criado_em"] = v["criado_em"].strftime("%Y-%m-%d %H:%M")

            cur.execute(f"""
                SELECT utm_campaign, COUNT(*) as qtd, SUM(valor) as total
                FROM vendas WHERE status='pago' AND utm_campaign != '' {filtro}
                GROUP BY utm_campaign ORDER BY total DESC
            """)
            por_campanha = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT utm_source, COUNT(*) as qtd, SUM(valor) as total
                FROM vendas WHERE status='pago' AND utm_source != '' {filtro}
                GROUP BY utm_source ORDER BY total DESC
            """)
            por_fonte = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT utm_content, COUNT(*) as qtd, SUM(valor) as total
                FROM vendas WHERE status='pago' AND utm_content != '' {filtro}
                GROUP BY utm_content ORDER BY total DESC LIMIT 10
            """)
            por_criativo = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT COUNT(*) as total_vendas, SUM(valor) as receita_total,
                       AVG(valor) as ticket_medio
                FROM vendas WHERE status='pago' {filtro}
            """)
            resumo = dict(cur.fetchone())

            # Vendas por dia (últimos 30 dias para o gráfico)
            cur.execute("""
                SELECT criado_em::date AS dia, COUNT(*) as qtd, SUM(valor) as total
                FROM vendas WHERE status='pago'
                AND criado_em >= NOW() - INTERVAL '30 days'
                GROUP BY dia ORDER BY dia ASC
            """)
            por_dia = [dict(r) for r in cur.fetchall()]
            for r in por_dia:
                if r.get("dia") and not isinstance(r["dia"], str):
                    r["dia"] = r["dia"].strftime("%Y-%m-%d")

    return render_template_string(
        DASHBOARD_HTML,
        vendas=vendas,
        por_campanha=[dict(r) for r in por_campanha],
        por_fonte=[dict(r) for r in por_fonte],
        por_criativo=[dict(r) for r in por_criativo],
        resumo=dict(resumo),
        por_dia=[dict(r) for r in por_dia],
        periodo=periodo,
        token=token
    )

# ── Admin: limpar dados de teste ─────────────────────────────────────────────

@app.route("/admin/limpar-testes", methods=["POST"])
def limpar_testes():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM vendas
                WHERE order_id LIKE 'TEST%'
                OR cliente_email LIKE '%teste%'
                OR cliente_email LIKE '%test%'
                OR cliente_nome LIKE '%Teste%'
            """)
            removidos = cur.rowcount
        conn.commit()
    return jsonify({"ok": True, "removidos": removidos})

# ── Endpoint para ver payload bruto (debug) ──────────────────────────────────

@app.route("/debug/vendas")
def debug_vendas():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, plataforma, criado_em, payload_raw FROM vendas ORDER BY id DESC LIMIT 20")
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get("criado_em") and not isinstance(r["criado_em"], str):
            r["criado_em"] = r["criado_em"].strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(rows)

# ── HTML do Dashboard ────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Performance — Gestar Bem</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root {
  --bg: #080808;
  --card: #111;
  --card2: #141414;
  --border: #1e1e1e;
  --green: #00ff88;
  --green-dim: #00ff8818;
  --green-mid: #00ff8840;
  --text: #f0f0f0;
  --muted: #555;
  --muted2: #333;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font-family:'Inter',system-ui,sans-serif; min-height:100vh; }

/* ── LAYOUT ── */
.wrap { max-width:1280px; margin:0 auto; padding:28px 20px; }

/* ── TOPBAR ── */
.topbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:32px; flex-wrap:wrap; gap:14px; }
.brand { display:flex; align-items:center; gap:10px; }
.brand-dot { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 8px var(--green); animation:pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.brand h1 { font-size:1.1rem; font-weight:600; color:var(--text); letter-spacing:-.3px; }
.brand h1 span { color:var(--green); }
.filtros { display:flex; gap:6px; background:var(--card); border:1px solid var(--border); border-radius:24px; padding:4px; }
.filtros a { padding:6px 16px; border-radius:20px; font-size:0.78rem; font-weight:500; text-decoration:none; color:var(--muted); transition:.15s; }
.filtros a.ativo { background:var(--green); color:#000; font-weight:600; }
.filtros a:hover:not(.ativo) { color:var(--text); }

/* ── KPI GRID ── */
.kpi-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:20px; }
.kpi { background:var(--card); border:1px solid var(--border); border-radius:16px; padding:22px 20px; position:relative; overflow:hidden; }
.kpi::before { content:''; position:absolute; inset:0; background:linear-gradient(135deg,var(--green-dim) 0%,transparent 60%); pointer-events:none; }
.kpi-icon { font-size:1.4rem; margin-bottom:10px; }
.kpi-val { font-size:2rem; font-weight:700; color:var(--green); letter-spacing:-1px; line-height:1; }
.kpi-label { font-size:0.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.8px; margin-top:6px; }

/* ── CHART ── */
.chart-card { background:var(--card); border:1px solid var(--border); border-radius:16px; padding:22px; margin-bottom:20px; }
.card-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:18px; }
.card-title { font-size:0.72rem; font-weight:600; text-transform:uppercase; letter-spacing:.8px; color:var(--muted); }
.chart-legend { display:flex; gap:16px; }
.legend-item { display:flex; align-items:center; gap:6px; font-size:0.72rem; color:var(--muted); }
.legend-dot { width:8px; height:8px; border-radius:50%; }
.chart-wrap { height:200px; }

/* ── TWO-COL ── */
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:20px; }

/* ── SECTION CARD ── */
.scard { background:var(--card); border:1px solid var(--border); border-radius:16px; padding:22px; }

/* ── TABLE ── */
table { width:100%; border-collapse:collapse; }
th { font-size:0.68rem; font-weight:600; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); padding:0 0 12px 0; border-bottom:1px solid var(--border); text-align:left; }
td { padding:11px 0; border-bottom:1px solid var(--muted2); font-size:0.84rem; vertical-align:middle; }
tr:last-child td { border-bottom:none; }
.td-green { color:var(--green); font-weight:600; }
.td-muted { color:var(--muted); font-size:0.78rem; }
.badge-fonte { display:inline-flex; align-items:center; gap:5px; background:var(--muted2); border-radius:6px; padding:2px 8px; font-size:0.75rem; }

/* ── CRIATIVOS com barra ── */
.criativo-row { padding:10px 0; border-bottom:1px solid var(--muted2); }
.criativo-row:last-child { border-bottom:none; }
.criativo-top { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; font-size:0.83rem; }
.criativo-name { color:var(--text); max-width:70%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.criativo-val { color:var(--green); font-weight:600; font-size:0.83rem; }
.bar-track { background:var(--muted2); border-radius:4px; height:4px; }
.bar-fill { background:linear-gradient(90deg,var(--green-mid),var(--green)); border-radius:4px; height:4px; transition:width .6s ease; }
.criativo-meta { font-size:0.7rem; color:var(--muted); margin-top:4px; }

/* ── FEED ── */
.feed-item { display:flex; align-items:center; gap:14px; padding:12px 0; border-bottom:1px solid var(--muted2); }
.feed-item:last-child { border-bottom:none; }
.feed-dot { width:36px; height:36px; border-radius:10px; background:var(--green-dim); border:1px solid var(--green-mid); display:flex; align-items:center; justify-content:center; font-size:1rem; flex-shrink:0; }
.feed-info { flex:1; min-width:0; }
.feed-prod { font-size:0.84rem; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.feed-camp { font-size:0.72rem; color:var(--muted); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.feed-right { text-align:right; flex-shrink:0; }
.feed-val { color:var(--green); font-weight:700; font-size:0.95rem; }
.feed-time { font-size:0.7rem; color:var(--muted); margin-top:2px; }

/* ── EMPTY ── */
.empty { color:var(--muted); font-size:0.84rem; text-align:center; padding:40px 0; }

/* ── FOOTER ── */
.footer { text-align:center; font-size:0.7rem; color:var(--muted2); margin-top:32px; padding-top:20px; border-top:1px solid var(--border); }

/* ── RESPONSIVE ── */
@media(max-width:640px) {
  .kpi-grid { grid-template-columns:1fr 1fr; }
  .kpi-grid .kpi:last-child { grid-column:1/-1; }
  .two-col { grid-template-columns:1fr; }
  .topbar { flex-direction:column; align-items:flex-start; }
  .kpi-val { font-size:1.6rem; }
}
</style>
</head>
<body>
<div class="wrap">

<!-- TOPBAR -->
<div class="topbar">
  <div class="brand">
    <div class="brand-dot"></div>
    <h1>Performance <span>— Gestar Bem</span></h1>
  </div>
  <div class="filtros">
    <a href="?token={{ token }}&periodo=hoje" class="{{ 'ativo' if periodo=='hoje' else '' }}">Hoje</a>
    <a href="?token={{ token }}&periodo=7d"   class="{{ 'ativo' if periodo=='7d'   else '' }}">7 dias</a>
    <a href="?token={{ token }}&periodo=30d"  class="{{ 'ativo' if periodo=='30d'  else '' }}">30 dias</a>
    <a href="?token={{ token }}&periodo=tudo" class="{{ 'ativo' if periodo=='tudo' else '' }}">Tudo</a>
  </div>
</div>

<!-- KPIs -->
<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-icon">💰</div>
    <div class="kpi-val">R$ {{ "%.0f"|format(resumo.receita_total or 0) }}</div>
    <div class="kpi-label">Receita total</div>
  </div>
  <div class="kpi">
    <div class="kpi-icon">🛒</div>
    <div class="kpi-val">{{ resumo.total_vendas or 0 }}</div>
    <div class="kpi-label">Vendas pagas</div>
  </div>
  <div class="kpi">
    <div class="kpi-icon">🎯</div>
    <div class="kpi-val">R$ {{ "%.0f"|format(resumo.ticket_medio or 0) }}</div>
    <div class="kpi-label">Ticket médio</div>
  </div>
</div>

<!-- GRÁFICO -->
<div class="chart-card">
  <div class="card-header">
    <span class="card-title">Receita por dia — últimos 30 dias</span>
    <div class="chart-legend">
      <div class="legend-item"><div class="legend-dot" style="background:#00ff88"></div>Receita</div>
      <div class="legend-item"><div class="legend-dot" style="background:#ffffff44"></div>Vendas</div>
    </div>
  </div>
  <div class="chart-wrap"><canvas id="grafico"></canvas></div>
</div>

<!-- CAMPANHA + FONTE -->
<div class="two-col">
  <div class="scard">
    <div class="card-header"><span class="card-title">Por Campanha</span></div>
    {% if por_campanha %}
    <table>
      <tr><th>Campanha</th><th>Qtd</th><th>Receita</th></tr>
      {% for r in por_campanha %}
      <tr>
        <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{{ r.utm_campaign }}">{{ r.utm_campaign }}</td>
        <td class="td-muted">{{ r.qtd }}</td>
        <td class="td-green">R$ {{ "%.0f"|format(r.total) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<p class="empty">Sem dados ainda</p>{% endif %}
  </div>

  <div class="scard">
    <div class="card-header"><span class="card-title">Por Fonte</span></div>
    {% if por_fonte %}
    <table>
      <tr><th>Fonte</th><th>Qtd</th><th>Receita</th></tr>
      {% for r in por_fonte %}
      <tr>
        <td>
          <span class="badge-fonte">
            {% if 'facebook' in (r.utm_source or '') or 'fb' in (r.utm_source or '') %}📘{% elif 'google' in (r.utm_source or '') %}🟡{% elif 'instagram' in (r.utm_source or '') %}📸{% else %}🔗{% endif %}
            {{ r.utm_source or 'direto' }}
          </span>
        </td>
        <td class="td-muted">{{ r.qtd }}</td>
        <td class="td-green">R$ {{ "%.0f"|format(r.total) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<p class="empty">Sem dados ainda</p>{% endif %}
  </div>
</div>

<!-- CRIATIVOS -->
<div class="scard" style="margin-bottom:20px">
  <div class="card-header"><span class="card-title">Criativos — top 10</span></div>
  {% if por_criativo %}
  {% set max_total = por_criativo[0].total %}
  {% for r in por_criativo %}
  <div class="criativo-row">
    <div class="criativo-top">
      <span class="criativo-name" title="{{ r.utm_content }}">{{ r.utm_content }}</span>
      <span class="criativo-val">R$ {{ "%.0f"|format(r.total) }}</span>
    </div>
    <div class="bar-track">
      <div class="bar-fill" style="width:{{ ((r.total / max_total) * 100)|int }}%"></div>
    </div>
    <div class="criativo-meta">{{ r.qtd }} venda{{ 's' if r.qtd != 1 else '' }}</div>
  </div>
  {% endfor %}
  {% else %}<p class="empty">Sem dados ainda</p>{% endif %}
</div>

<!-- ÚLTIMAS VENDAS -->
<div class="scard">
  <div class="card-header"><span class="card-title">Últimas vendas</span></div>
  {% if vendas %}
  {% for v in vendas[:30] %}
  <div class="feed-item">
    <div class="feed-dot">💳</div>
    <div class="feed-info">
      <div class="feed-prod">{{ v.cliente_nome or v.produto or 'Venda' }}</div>
      <div class="feed-camp">{{ v.utm_campaign or 'sem campanha' }}{% if v.utm_content %} · {{ v.utm_content }}{% endif %}</div>
    </div>
    <div class="feed-right">
      <div class="feed-val">R$ {{ "%.0f"|format(v.valor) }}</div>
      <div class="feed-time">{{ v.criado_em }}</div>
    </div>
  </div>
  {% endfor %}
  {% else %}
  <p class="empty">⏳ Aguardando primeiras vendas...</p>
  {% endif %}
</div>

<div class="footer">performance.programagestarbem.com.br · atualiza a cada venda</div>

</div><!-- /wrap -->

<script>
const dias = {{ por_dia | map(attribute='dia')   | list | tojson }};
const qtds = {{ por_dia | map(attribute='qtd')   | list | tojson }};
const tots = {{ por_dia | map(attribute='total') | list | tojson }};

const ctx = document.getElementById('grafico').getContext('2d');
new Chart(ctx, {
  data: {
    labels: dias,
    datasets: [
      {
        type: 'bar',
        label: 'Receita (R$)',
        data: tots.map(v => parseFloat(v)||0),
        backgroundColor: '#00ff8825',
        borderColor: '#00ff88',
        borderWidth: 1.5,
        borderRadius: 5,
        yAxisID: 'y',
        order: 2,
      },
      {
        type: 'line',
        label: 'Vendas',
        data: qtds,
        borderColor: '#ffffff55',
        backgroundColor: '#ffffff08',
        borderWidth: 1.5,
        pointRadius: 3,
        pointBackgroundColor: '#ffffff88',
        tension: 0.4,
        fill: true,
        yAxisID: 'y2',
        order: 1,
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode:'index', intersect:false },
    plugins: {
      legend: { display:false },
      tooltip: {
        backgroundColor: '#1a1a1a',
        borderColor: '#2a2a2a',
        borderWidth: 1,
        titleColor: '#888',
        bodyColor: '#f0f0f0',
        padding: 10,
        callbacks: {
          label: ctx => ctx.dataset.label === 'Receita (R$)'
            ? ' R$ ' + ctx.parsed.y.toFixed(0)
            : ' ' + ctx.parsed.y + ' venda(s)'
        }
      }
    },
    scales: {
      x: { ticks:{ color:'#444', font:{size:10} }, grid:{ color:'#151515' } },
      y: {
        position:'left',
        ticks:{ color:'#444', font:{size:10}, callback: v => 'R$'+v },
        grid:{ color:'#151515' }
      },
      y2: {
        position:'right',
        ticks:{ color:'#333', font:{size:10}, stepSize:1 },
        grid:{ display:false }
      }
    }
  }
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
