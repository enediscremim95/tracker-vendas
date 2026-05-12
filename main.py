from flask import Flask, request, jsonify, render_template_string, abort
import sqlite3, os, json
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "tracker.db")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "veritas2026")
THEMEMBERS_TOKEN = os.environ.get("THEMEMBERS_TOKEN", "")

# ── Banco ────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                plataforma  TEXT,
                produto     TEXT,
                valor       REAL,
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
                criado_em    TEXT
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
            conn.execute("""
                INSERT INTO vendas
                  (plataforma, produto, valor, status, cliente_nome, cliente_email,
                   utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                   order_id, payload_raw, criado_em)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                json.dumps(body, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
            conn.execute("""
                INSERT INTO vendas
                  (plataforma, produto, valor, status, cliente_nome, cliente_email,
                   utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                   order_id, payload_raw, criado_em)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                json.dumps(data, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

@app.route("/dashboard")
def dashboard():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)

    with get_db() as conn:
        vendas = conn.execute("""
            SELECT * FROM vendas WHERE status = 'pago'
            ORDER BY criado_em DESC
        """).fetchall()
        vendas = [dict(v) for v in vendas]

        por_campanha = conn.execute("""
            SELECT utm_campaign, COUNT(*) as qtd, SUM(valor) as total
            FROM vendas WHERE status='pago' AND utm_campaign != ''
            GROUP BY utm_campaign ORDER BY total DESC
        """).fetchall()

        por_fonte = conn.execute("""
            SELECT utm_source, COUNT(*) as qtd, SUM(valor) as total
            FROM vendas WHERE status='pago' AND utm_source != ''
            GROUP BY utm_source ORDER BY total DESC
        """).fetchall()

        por_criativo = conn.execute("""
            SELECT utm_content, COUNT(*) as qtd, SUM(valor) as total
            FROM vendas WHERE status='pago' AND utm_content != ''
            GROUP BY utm_content ORDER BY total DESC
        """).fetchall()

        resumo = conn.execute("""
            SELECT COUNT(*) as total_vendas, SUM(valor) as receita_total,
                   AVG(valor) as ticket_medio
            FROM vendas WHERE status='pago'
        """).fetchone()

    return render_template_string(
        DASHBOARD_HTML,
        vendas=vendas,
        por_campanha=[dict(r) for r in por_campanha],
        por_fonte=[dict(r) for r in por_fonte],
        por_criativo=[dict(r) for r in por_criativo],
        resumo=dict(resumo),
        token=token
    )

# ── Endpoint para ver payload bruto (debug) ──────────────────────────────────

@app.route("/debug/vendas")
def debug_vendas():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)
    with get_db() as conn:
        rows = conn.execute("SELECT id, plataforma, criado_em, payload_raw FROM vendas ORDER BY id DESC LIMIT 20").fetchall()
    return jsonify([dict(r) for r in rows])

# ── HTML do Dashboard ────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard de Rastreamento</title>
<style>
  :root {
    --bg: #0d0d0d; --card: #161616; --border: #222;
    --green: #00ff88; --text: #f0f0f0; --muted: #888;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; padding: 24px; }
  h1 { font-size: 1.4rem; margin-bottom: 24px; color: var(--green); }
  h2 { font-size: 1rem; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 32px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .card .val { font-size: 1.8rem; font-weight: 700; color: var(--green); }
  .card .label { font-size: 0.8rem; color: var(--muted); margin-top: 4px; }
  .section { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 24px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th { text-align: left; color: var(--muted); padding: 8px 12px; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid #1a1a1a; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 0.75rem; }
  .badge-pago { background: #00ff8820; color: var(--green); }
  .badge-meta { background: #1877f220; color: #1877f2; }
  .badge-google { background: #fbbc0520; color: #fbbc05; }
  .empty { color: var(--muted); font-size: 0.9rem; text-align: center; padding: 32px; }
</style>
</head>
<body>
<h1>Rastreamento de Vendas</h1>

<div class="grid">
  <div class="card">
    <div class="val">{{ resumo.total_vendas or 0 }}</div>
    <div class="label">Vendas pagas</div>
  </div>
  <div class="card">
    <div class="val">R$ {{ "%.2f"|format(resumo.receita_total or 0) }}</div>
    <div class="label">Receita total</div>
  </div>
  <div class="card">
    <div class="val">R$ {{ "%.2f"|format(resumo.ticket_medio or 0) }}</div>
    <div class="label">Ticket médio</div>
  </div>
</div>

<div class="section">
  <h2>Por Campanha</h2>
  {% if por_campanha %}
  <table>
    <tr><th>Campanha</th><th>Vendas</th><th>Receita</th></tr>
    {% for r in por_campanha %}
    <tr>
      <td>{{ r.utm_campaign }}</td>
      <td>{{ r.qtd }}</td>
      <td>R$ {{ "%.2f"|format(r.total) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="empty">Nenhuma venda com UTM de campanha ainda</p>
  {% endif %}
</div>

<div class="section">
  <h2>Por Fonte</h2>
  {% if por_fonte %}
  <table>
    <tr><th>Fonte</th><th>Vendas</th><th>Receita</th></tr>
    {% for r in por_fonte %}
    <tr>
      <td>{{ r.utm_source }}</td>
      <td>{{ r.qtd }}</td>
      <td>R$ {{ "%.2f"|format(r.total) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="empty">Nenhuma venda com UTM de fonte ainda</p>
  {% endif %}
</div>

<div class="section">
  <h2>Por Criativo</h2>
  {% if por_criativo %}
  <table>
    <tr><th>Criativo</th><th>Vendas</th><th>Receita</th></tr>
    {% for r in por_criativo %}
    <tr>
      <td>{{ r.utm_content }}</td>
      <td>{{ r.qtd }}</td>
      <td>R$ {{ "%.2f"|format(r.total) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="empty">Nenhuma venda com UTM de criativo ainda</p>
  {% endif %}
</div>

<div class="section">
  <h2>Últimas vendas</h2>
  {% if vendas %}
  <table>
    <tr><th>Data</th><th>Produto</th><th>Cliente</th><th>Valor</th><th>Campanha</th><th>Fonte</th><th>Criativo</th></tr>
    {% for v in vendas[:50] %}
    <tr>
      <td>{{ v.criado_em[:16] }}</td>
      <td>{{ v.produto or '-' }}</td>
      <td>{{ v.cliente_nome or '-' }}</td>
      <td>R$ {{ "%.2f"|format(v.valor) }}</td>
      <td>{{ v.utm_campaign or '-' }}</td>
      <td>{{ v.utm_source or '-' }}</td>
      <td>{{ v.utm_content or '-' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="empty">Nenhuma venda registrada ainda. Aguardando primeiros webhooks.</p>
  {% endif %}
</div>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
