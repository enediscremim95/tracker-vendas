from flask import Flask, request, jsonify, render_template_string, abort, redirect
import os, json, secrets
from datetime import datetime
from urllib.parse import urlencode
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
DATABASE_URL    = os.environ.get("DATABASE_URL", "")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "veritas2026")
THEMEMBERS_TOKEN= os.environ.get("THEMEMBERS_TOKEN", "")
BASE_URL        = os.environ.get("BASE_URL", "https://performance.programagestarbem.com.br")

# ── Banco ─────────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vendas (
                    id            SERIAL PRIMARY KEY,
                    plataforma    TEXT,
                    produto       TEXT,
                    valor         NUMERIC(10,2),
                    moeda         TEXT DEFAULT 'BRL',
                    status        TEXT,
                    cliente_nome  TEXT,
                    cliente_email TEXT,
                    utm_source    TEXT,
                    utm_medium    TEXT,
                    utm_campaign  TEXT,
                    utm_content   TEXT,
                    utm_term      TEXT,
                    order_id      TEXT,
                    payload_raw   TEXT,
                    criado_em     TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS links (
                    id           SERIAL PRIMARY KEY,
                    slug         TEXT UNIQUE NOT NULL,
                    nome         TEXT DEFAULT '',
                    url_destino  TEXT NOT NULL,
                    utm_source   TEXT DEFAULT '',
                    utm_medium   TEXT DEFAULT '',
                    utm_campaign TEXT DEFAULT '',
                    utm_content  TEXT DEFAULT '',
                    utm_term     TEXT DEFAULT '',
                    produto      TEXT DEFAULT '',
                    cliques      INTEGER DEFAULT 0,
                    criado_em    TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()

init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────

def gerar_slug(n=6):
    chars = 'abcdefghjkmnpqrstuvwxyz23456789'
    return ''.join(secrets.choice(chars) for _ in range(n))

def extrair_utms(data: dict) -> dict:
    campos = ["utm_source","utm_medium","utm_campaign","utm_content","utm_term"]
    utms = {}
    for c in campos:
        val = data.get(c) or data.get(c.upper())
        if not val:
            for sub in ["tracking","utm","metadata","custom_fields","order"]:
                if isinstance(data.get(sub), dict):
                    val = data[sub].get(c) or data[sub].get(c.upper())
                    if val: break
        utms[c] = val or ""
    return utms

# ── Webhook TheMembers ────────────────────────────────────────────────────────

def extrair_utms_themembers(utms_raw) -> dict:
    campos = ["utm_source","utm_medium","utm_campaign","utm_content","utm_term"]
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
        if THEMEMBERS_TOKEN:
            # TheMembers pode mandar o token em headers diferentes ou no body
            sig = (request.headers.get("x-signature") or
                   request.headers.get("x-webhook-token") or
                   request.headers.get("authorization", "").replace("Bearer ", "") or
                   (request.get_json(force=True, silent=True) or {}).get("token") or "")
            if sig != THEMEMBERS_TOKEN:
                app.logger.warning(f"Token inválido recebido: '{sig[:20]}...' esperado: '{THEMEMBERS_TOKEN[:10]}...'")
                # Logar headers para diagnóstico sem bloquear (modo debug)
                app.logger.warning(f"Headers recebidos: {dict(request.headers)}")
                return jsonify({"ok": False, "erro": "token inválido"}), 401

        body    = request.get_json(force=True) or {}
        payload = body.get("payload") or body
        event   = payload.get("event") or payload.get("tags", {}).get("event") or ""
        data    = payload.get("data") or {}

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

        order   = data.get("order") or {}
        cliente = (order.get("customer") or data.get("customer") or
                   data.get("subscriber") or {})
        if isinstance(cliente, str):
            cliente = {}

        main_product = (order.get("main_product") or data.get("main_product") or
                        data.get("product") or {})
        produto = (main_product.get("title") or main_product.get("name") or
                   (data.get("product", {}).get("title")
                    if isinstance(data.get("product"), dict) else None) or "")

        trans       = data.get("transaction") or {}
        valor_cents = (trans.get("amount") or trans.get("total_amount") or
                       order.get("total") or main_product.get("price") or 0)
        try:
            valor = float(valor_cents) / 100
        except:
            valor = 0.0

        utms_raw = order.get("utms") or data.get("utms") or {}
        utms     = extrair_utms_themembers(utms_raw)
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
                    "themembers", produto, valor, status,
                    cliente.get("name") or cliente.get("full_name") or "",
                    cliente.get("email") or "",
                    utms["utm_source"], utms["utm_medium"], utms["utm_campaign"],
                    utms["utm_content"], utms["utm_term"],
                    order_id, json.dumps(body, ensure_ascii=False)
                ))
            conn.commit()

        return jsonify({"ok": True}), 200
    except Exception as e:
        app.logger.error(f"Erro webhook themembers: {e}")
        return jsonify({"ok": False, "erro": str(e)}), 500

@app.route("/webhook/thebank", methods=["POST"])
def webhook_thebank():
    return webhook_themembers()

# ── Webhook Kiwify ────────────────────────────────────────────────────────────

@app.route("/webhook/kiwify", methods=["POST"])
def webhook_kiwify():
    try:
        data  = request.get_json(force=True) or {}
        utms  = extrair_utms(data)
        order = data.get("order") or data
        try:
            valor = float(order.get("amount") or order.get("value") or 0) / 100
        except:
            valor = 0.0

        status_map = {
            "paid": "pago","approved": "pago",
            "refunded": "reembolsado","chargedback": "chargeback",
            "abandoned": "abandonado"
        }
        status_raw = (data.get("order_status") or order.get("status") or "").lower()
        status     = status_map.get(status_raw, status_raw)

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
                    valor, status,
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

# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ── Links UTM — Encurtador com rastreamento de cliques ────────────────────────

@app.route("/r/<slug>")
def redirect_link(slug):
    """Redirect que conta o clique antes de mandar pro destino."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT url_destino FROM links WHERE slug=%s", (slug,))
            row = cur.fetchone()
            if not row:
                abort(404)
            cur.execute("UPDATE links SET cliques=cliques+1 WHERE slug=%s", (slug,))
        conn.commit()
    return redirect(row["url_destino"], 302)

@app.route("/links/criar", methods=["POST"])
def criar_link():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)

    data         = request.get_json(force=True) or {}
    url_base     = (data.get("url") or "").strip()
    if not url_base:
        return jsonify({"ok": False, "erro": "URL obrigatória"}), 400

    utm_source   = data.get("utm_source",   "").strip()
    utm_medium   = data.get("utm_medium",   "").strip()
    utm_campaign = data.get("utm_campaign", "").strip()
    utm_content  = data.get("utm_content",  "").strip()
    utm_term     = data.get("utm_term",     "").strip()
    nome         = data.get("nome",         "").strip()
    produto      = data.get("produto",      "").strip()

    params = [(k, v) for k, v in [
        ("utm_source", utm_source), ("utm_medium", utm_medium),
        ("utm_campaign", utm_campaign), ("utm_content", utm_content),
        ("utm_term", utm_term)
    ] if v]
    sep          = "&" if "?" in url_base else "?"
    url_completa = url_base + (sep + urlencode(params) if params else "")

    slug = None
    for _ in range(10):
        tentativa = gerar_slug()
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO links
                          (slug, nome, url_destino, utm_source, utm_medium,
                           utm_campaign, utm_content, utm_term, produto)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (tentativa, nome, url_completa, utm_source, utm_medium,
                          utm_campaign, utm_content, utm_term, produto))
                conn.commit()
            slug = tentativa
            break
        except Exception:
            continue

    if not slug:
        return jsonify({"ok": False, "erro": "Erro ao salvar"}), 500

    return jsonify({
        "ok": True,
        "slug": slug,
        "short": f"{BASE_URL}/r/{slug}",
        "url_completa": url_completa
    })

@app.route("/links/stats")
def links_stats():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    l.id, l.slug, l.nome, l.url_destino,
                    l.utm_campaign, l.utm_content, l.utm_source, l.utm_medium,
                    l.produto, l.cliques, l.criado_em,
                    COUNT(v.id) AS vendas,
                    COALESCE(SUM(v.valor), 0) AS receita
                FROM links l
                LEFT JOIN vendas v ON
                    v.status = 'pago'
                    AND (l.utm_campaign = '' OR v.utm_campaign = l.utm_campaign)
                    AND (l.utm_content  = '' OR v.utm_content  = l.utm_content)
                GROUP BY l.id
                ORDER BY l.criado_em DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        if r.get("criado_em") and not isinstance(r["criado_em"], str):
            r["criado_em"] = r["criado_em"].strftime("%d/%m/%Y")
        r["vendas"]    = int(r["vendas"]  or 0)
        r["receita"]   = float(r["receita"] or 0)
        r["cliques"]   = int(r["cliques"] or 0)
        r["conversao"] = round(r["vendas"] / r["cliques"] * 100, 1) if r["cliques"] > 0 else 0

    return jsonify(rows)

@app.route("/links/deletar/<int:link_id>", methods=["DELETE"])
def deletar_link(link_id):
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM links WHERE id=%s", (link_id,))
        conn.commit()
    return jsonify({"ok": True})

# ── Dashboard ─────────────────────────────────────────────────────────────────

def periodo_filtro(periodo):
    if periodo == "hoje":
        return "AND criado_em::date = CURRENT_DATE"
    elif periodo == "7d":
        return "AND criado_em >= NOW() - INTERVAL '7 days'"
    elif periodo == "30d":
        return "AND criado_em >= NOW() - INTERVAL '30 days'"
    return ""

@app.route("/dashboard")
def dashboard():
    token = request.args.get("token", "")
    if token != DASHBOARD_TOKEN:
        abort(403)

    periodo = request.args.get("periodo", "30d")
    filtro  = periodo_filtro(periodo)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM vendas WHERE status='pago' {filtro} ORDER BY criado_em DESC")
            vendas = [dict(v) for v in cur.fetchall()]
            for v in vendas:
                if v.get("criado_em") and not isinstance(v["criado_em"], str):
                    v["criado_em"] = v["criado_em"].strftime("%Y-%m-%d %H:%M")

            cur.execute(f"""
                SELECT utm_campaign, COUNT(*) as qtd, SUM(valor) as total
                FROM vendas WHERE status='pago' AND utm_campaign!='' {filtro}
                GROUP BY utm_campaign ORDER BY total DESC
            """)
            por_campanha = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT utm_source, COUNT(*) as qtd, SUM(valor) as total
                FROM vendas WHERE status='pago' AND utm_source!='' {filtro}
                GROUP BY utm_source ORDER BY total DESC
            """)
            por_fonte = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT utm_content, COUNT(*) as qtd, SUM(valor) as total
                FROM vendas WHERE status='pago' AND utm_content!='' {filtro}
                GROUP BY utm_content ORDER BY total DESC LIMIT 10
            """)
            por_criativo = [dict(r) for r in cur.fetchall()]

            cur.execute(f"""
                SELECT COUNT(*) as total_vendas, SUM(valor) as receita_total,
                       AVG(valor) as ticket_medio
                FROM vendas WHERE status='pago' {filtro}
            """)
            resumo = dict(cur.fetchone())

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
        por_campanha=por_campanha,
        por_fonte=por_fonte,
        por_criativo=por_criativo,
        resumo=resumo,
        por_dia=por_dia,
        periodo=periodo,
        token=token,
        base_url=BASE_URL
    )

# ── Admin ─────────────────────────────────────────────────────────────────────

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
                   OR cliente_nome  LIKE '%Teste%'
            """)
            removidos = cur.rowcount
        conn.commit()
    return jsonify({"ok": True, "removidos": removidos})

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

# ── HTML ──────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Performance — Gestar Bem</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root {
  --bg:#080808; --card:#111; --card2:#141414; --border:#1e1e1e;
  --green:#00ff88; --green-dim:#00ff8818; --green-mid:#00ff8840;
  --text:#f0f0f0; --muted:#555; --muted2:#333; --red:#ff4455;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;min-height:100vh}
.wrap{max-width:1280px;margin:0 auto;padding:28px 20px}

/* topbar */
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:14px}
.brand{display:flex;align-items:center;gap:10px}
.brand-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes spin{to{transform:rotate(360deg)}}
.brand h1{font-size:1.1rem;font-weight:600;letter-spacing:-.3px}
.brand h1 span{color:var(--green)}
.filtros{display:flex;gap:6px;background:var(--card);border:1px solid var(--border);border-radius:24px;padding:4px}
.filtros a{padding:6px 16px;border-radius:20px;font-size:.78rem;font-weight:500;text-decoration:none;color:var(--muted);transition:.15s}
.filtros a.ativo{background:var(--green);color:#000;font-weight:600}
.filtros a:hover:not(.ativo){color:var(--text)}

/* abas */
.abas{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:4px;margin-bottom:24px;width:fit-content}
.aba-btn{padding:8px 20px;border-radius:9px;border:none;background:transparent;color:var(--muted);font-size:.82rem;font-weight:500;cursor:pointer;transition:.15s;font-family:inherit}
.aba-btn.ativa{background:var(--card2);color:var(--text);border:1px solid var(--border)}
.aba-btn:hover:not(.ativa){color:var(--text)}

/* kpi */
.kpi-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:22px 20px;position:relative;overflow:hidden}
.kpi::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,var(--green-dim) 0%,transparent 60%);pointer-events:none}
.kpi-icon{font-size:1.4rem;margin-bottom:10px}
.kpi-val{font-size:2rem;font-weight:700;color:var(--green);letter-spacing:-1px;line-height:1}
.kpi-label{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-top:6px}

/* chart */
.chart-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:22px;margin-bottom:20px}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.card-title{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--muted)}
.chart-legend{display:flex;gap:16px}
.legend-item{display:flex;align-items:center;gap:6px;font-size:.72rem;color:var(--muted)}
.legend-dot{width:8px;height:8px;border-radius:50%}
.chart-wrap{height:200px}

/* two col */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:20px}

/* section card */
.scard{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:22px}

/* table */
table{width:100%;border-collapse:collapse}
th{font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);padding:0 0 12px;border-bottom:1px solid var(--border);text-align:left}
td{padding:11px 0;border-bottom:1px solid var(--muted2);font-size:.84rem;vertical-align:middle}
tr:last-child td{border-bottom:none}
.td-green{color:var(--green);font-weight:600}
.td-muted{color:var(--muted);font-size:.78rem}
.badge-fonte{display:inline-flex;align-items:center;gap:5px;background:var(--muted2);border-radius:6px;padding:2px 8px;font-size:.75rem}

/* criativos */
.criativo-row{padding:10px 0;border-bottom:1px solid var(--muted2)}
.criativo-row:last-child{border-bottom:none}
.criativo-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:.83rem}
.criativo-name{color:var(--text);max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.criativo-val{color:var(--green);font-weight:600;font-size:.83rem}
.bar-track{background:var(--muted2);border-radius:4px;height:4px}
.bar-fill{background:linear-gradient(90deg,var(--green-mid),var(--green));border-radius:4px;height:4px;transition:width .6s ease}
.criativo-meta{font-size:.7rem;color:var(--muted);margin-top:4px}

/* feed */
.feed-item{display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid var(--muted2)}
.feed-item:last-child{border-bottom:none}
.feed-dot{width:36px;height:36px;border-radius:10px;background:var(--green-dim);border:1px solid var(--green-mid);display:flex;align-items:center;justify-content:center;font-size:1rem;flex-shrink:0}
.feed-info{flex:1;min-width:0}
.feed-prod{font-size:.84rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.feed-camp{font-size:.72rem;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.feed-right{text-align:right;flex-shrink:0}
.feed-val{color:var(--green);font-weight:700;font-size:.95rem}
.feed-time{font-size:.7rem;color:var(--muted);margin-top:2px}

/* ── LINKS UTM ── */
.presets{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
.preset-btn{padding:6px 14px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--muted);font-size:.78rem;font-weight:500;cursor:pointer;transition:.15s;font-family:inherit}
.preset-btn.sel{background:var(--green-dim);border-color:var(--green-mid);color:var(--green)}
.preset-btn:hover:not(.sel){color:var(--text);border-color:var(--muted)}

.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.form-grid.full{grid-template-columns:1fr}
.finput{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-size:.84rem;font-family:inherit;width:100%;outline:none;transition:.15s}
.finput:focus{border-color:var(--green-mid)}
.finput::placeholder{color:var(--muted)}
.flabel{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px}
.fgroup{display:flex;flex-direction:column}

.btn-criar{background:var(--green);color:#000;border:none;border-radius:10px;padding:12px 24px;font-size:.88rem;font-weight:700;cursor:pointer;font-family:inherit;transition:.15s;width:100%}
.btn-criar:hover{background:#00dd77}
.btn-criar:disabled{opacity:.5;cursor:not-allowed}

.link-result{background:var(--card2);border:1px solid var(--green-mid);border-radius:12px;padding:16px;margin-top:14px;display:none}
.link-result-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.link-short{font-size:1rem;font-weight:700;color:var(--green);word-break:break-all}
.btn-copy{background:transparent;border:1px solid var(--border);border-radius:7px;padding:5px 12px;color:var(--muted);font-size:.75rem;cursor:pointer;font-family:inherit;transition:.15s}
.btn-copy:hover{color:var(--text);border-color:var(--muted)}
.btn-copy.ok{color:var(--green);border-color:var(--green-mid)}
.link-full{font-size:.7rem;color:var(--muted);word-break:break-all}

/* funil de links */
.link-row{padding:14px 0;border-bottom:1px solid var(--muted2)}
.link-row:last-child{border-bottom:none}
.link-header{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}
.link-name{font-size:.88rem;font-weight:600;flex:1}
.link-slug-tag{font-size:.7rem;background:var(--muted2);border-radius:5px;padding:2px 7px;color:var(--muted);white-space:nowrap}
.link-camp{font-size:.72rem;color:var(--muted);margin-bottom:10px}
.funil{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.funil-step{display:flex;flex-direction:column;align-items:center;min-width:64px}
.funil-val{font-size:1.1rem;font-weight:700}
.funil-label{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.funil-arrow{color:var(--muted);font-size:.8rem;padding:0 2px}
.funil-pct{font-size:.75rem;color:var(--muted);background:var(--muted2);border-radius:5px;padding:2px 6px}
.cliques-c{color:var(--text)}
.vendas-c{color:var(--green)}
.receita-c{color:var(--green)}
.conv-c{color:#f0c040}
.link-actions{margin-top:8px;display:flex;gap:6px}
.btn-del{background:transparent;border:1px solid var(--muted2);border-radius:6px;padding:4px 10px;color:var(--muted);font-size:.72rem;cursor:pointer;font-family:inherit;transition:.15s}
.btn-del:hover{color:var(--red);border-color:var(--red)}

.empty{color:var(--muted);font-size:.84rem;text-align:center;padding:40px 0}
.footer{text-align:center;font-size:.7rem;color:var(--muted2);margin-top:32px;padding-top:20px;border-top:1px solid var(--border)}

@media(max-width:640px){
  .kpi-grid{grid-template-columns:1fr 1fr}
  .kpi-grid .kpi:last-child{grid-column:1/-1}
  .two-col,.form-grid{grid-template-columns:1fr}
  .topbar{flex-direction:column;align-items:flex-start}
  .kpi-val{font-size:1.6rem}
  .funil{gap:4px}
  .funil-step{min-width:52px}
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
  <div style="display:flex;gap:8px;align-items:center">
    <div class="filtros">
      <a href="?token={{ token }}&periodo=hoje" class="{{ 'ativo' if periodo=='hoje' else '' }}">Hoje</a>
      <a href="?token={{ token }}&periodo=7d"   class="{{ 'ativo' if periodo=='7d'   else '' }}">7 dias</a>
      <a href="?token={{ token }}&periodo=30d"  class="{{ 'ativo' if periodo=='30d'  else '' }}">30 dias</a>
      <a href="?token={{ token }}&periodo=tudo" class="{{ 'ativo' if periodo=='tudo' else '' }}">Tudo</a>
    </div>
    <button onclick="atualizar()" id="btn-refresh" style="background:var(--card);border:1px solid var(--border);border-radius:24px;padding:6px 16px;color:var(--muted);font-size:.78rem;font-weight:500;cursor:pointer;font-family:inherit;transition:.15s;display:flex;align-items:center;gap:6px" onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--muted)'">
      <span id="refresh-icon">↻</span> Atualizar
    </button>
  </div>
</div>

<!-- ════ VENDAS ════ -->
<div id="aba-vendas">

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
            {% if 'facebook' in (r.utm_source or '') or 'fb' in (r.utm_source or '') %}📘
            {% elif 'google' in (r.utm_source or '') %}🟡
            {% elif 'instagram' in (r.utm_source or '') %}📸
            {% else %}🔗{% endif %}
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

</div><!-- /aba-vendas -->

<!-- ════ LINKS UTM — oculto, acessível via /links/stats ════ -->
<div id="aba-links" style="display:none">

<!-- GERADOR -->
<div class="scard" style="margin-bottom:20px">
  <div class="card-header"><span class="card-title">Novo Link UTM</span></div>

  <!-- Presets -->
  <div class="presets">
    <button class="preset-btn sel" id="p-meta"    onclick="setPreset('meta')">📘 Meta Ads</button>
    <button class="preset-btn"     id="p-google"  onclick="setPreset('google')">🟡 Google Ads</button>
    <button class="preset-btn"     id="p-org"     onclick="setPreset('organico')">📱 Orgânico</button>
    <button class="preset-btn"     id="p-custom"  onclick="setPreset('custom')">✏️ Livre</button>
  </div>

  <div class="form-grid full" style="margin-bottom:10px">
    <div class="fgroup">
      <div class="flabel">Nome do link (para identificar)</div>
      <input id="f-nome" class="finput" placeholder="ex: Anúncio Semana 1 — vídeo depoimento">
    </div>
  </div>

  <div class="form-grid full" style="margin-bottom:10px">
    <div class="fgroup">
      <div class="flabel">URL de destino *</div>
      <input id="f-url" class="finput" placeholder="https://www.programagestarbem.com.br/pv01/">
    </div>
  </div>

  <div class="form-grid">
    <div class="fgroup">
      <div class="flabel">utm_source</div>
      <input id="f-source" class="finput" placeholder="facebook">
    </div>
    <div class="fgroup">
      <div class="flabel">utm_medium</div>
      <input id="f-medium" class="finput" placeholder="cpc">
    </div>
    <div class="fgroup">
      <div class="flabel">utm_campaign</div>
      <input id="f-campaign" class="finput" placeholder="nome da campanha">
    </div>
    <div class="fgroup">
      <div class="flabel">utm_content (criativo)</div>
      <input id="f-content" class="finput" placeholder="nome do anúncio / criativo">
    </div>
    <div class="fgroup" style="grid-column:1/-1">
      <div class="flabel">utm_term (opcional)</div>
      <input id="f-term" class="finput" placeholder="palavra-chave (Google) ou público (Meta)">
    </div>
  </div>

  <button class="btn-criar" style="margin-top:14px" id="btn-criar" onclick="criarLink()">✦ Gerar Link Curto</button>

  <!-- Resultado -->
  <div class="link-result" id="link-result">
    <div class="link-result-row">
      <span class="link-short" id="link-short-txt"></span>
      <button class="btn-copy" id="btn-copy" onclick="copiarLink()">📋 Copiar</button>
    </div>
    <div class="link-full" id="link-full-txt"></div>
  </div>
</div>

<!-- FUNIL DE LINKS -->
<div class="scard">
  <div class="card-header">
    <span class="card-title">Links criados — funil de conversão</span>
    <button class="btn-copy" onclick="carregarLinks()" style="font-size:.75rem">↻ Atualizar</button>
  </div>
  <div id="links-wrap"><p class="empty">Carregando...</p></div>
</div>

</div><!-- /aba-links -->

<div class="footer">performance.programagestarbem.com.br · atualiza a cada venda</div>
</div><!-- /wrap -->

<script>
// ── ABAS ──────────────────────────────────────────────────────────────────────
function showAba(nome) {
  ['vendas','links'].forEach(a => {
    document.getElementById('aba-'+a).style.display = a===nome ? 'block' : 'none';
    document.getElementById('btn-'+a).classList.toggle('ativa', a===nome);
  });
  if (nome === 'links') carregarLinks();
}

// ── GRÁFICO ───────────────────────────────────────────────────────────────────
const dias = {{ por_dia | map(attribute='dia')   | list | tojson }};
const qtds = {{ por_dia | map(attribute='qtd')   | list | tojson }};
const tots = {{ por_dia | map(attribute='total') | list | tojson }};

new Chart(document.getElementById('grafico').getContext('2d'), {
  data: {
    labels: dias,
    datasets: [
      { type:'bar',  label:'Receita (R$)', data:tots.map(v=>parseFloat(v)||0),
        backgroundColor:'#00ff8825', borderColor:'#00ff88', borderWidth:1.5,
        borderRadius:5, yAxisID:'y', order:2 },
      { type:'line', label:'Vendas', data:qtds,
        borderColor:'#ffffff55', backgroundColor:'#ffffff08', borderWidth:1.5,
        pointRadius:3, pointBackgroundColor:'#ffffff88', tension:.4, fill:true,
        yAxisID:'y2', order:1 }
    ]
  },
  options: {
    responsive:true, maintainAspectRatio:false,
    interaction:{mode:'index',intersect:false},
    plugins: {
      legend:{display:false},
      tooltip:{
        backgroundColor:'#1a1a1a', borderColor:'#2a2a2a', borderWidth:1,
        titleColor:'#888', bodyColor:'#f0f0f0', padding:10,
        callbacks:{ label: c => c.dataset.label==='Receita (R$)'
          ? ' R$ '+c.parsed.y.toFixed(0) : ' '+c.parsed.y+' venda(s)' }
      }
    },
    scales:{
      x:{ ticks:{color:'#444',font:{size:10}}, grid:{color:'#151515'} },
      y:{ position:'left', ticks:{color:'#444',font:{size:10},callback:v=>'R$'+v}, grid:{color:'#151515'} },
      y2:{ position:'right', ticks:{color:'#333',font:{size:10},stepSize:1}, grid:{display:false} }
    }
  }
});

// ── PRESETS UTM ───────────────────────────────────────────────────────────────
const PRESETS = {
  meta:    { source:'facebook', medium:'cpc', campaign:'', content:'', term:'' },
  google:  { source:'google',   medium:'cpc', campaign:'', content:'', term:'' },
  organico:{ source:'instagram',medium:'organic',campaign:'',content:'',term:'' },
  custom:  { source:'', medium:'', campaign:'', content:'', term:'' }
};

function setPreset(p) {
  ['meta','google','org','custom'].forEach(id => {
    document.getElementById('p-'+id)?.classList.remove('sel');
  });
  const btnId = {meta:'p-meta',google:'p-google',organico:'p-org',custom:'p-custom'}[p];
  document.getElementById(btnId)?.classList.add('sel');
  const pr = PRESETS[p] || {};
  document.getElementById('f-source').value   = pr.source   || '';
  document.getElementById('f-medium').value   = pr.medium   || '';
  document.getElementById('f-campaign').value = pr.campaign || '';
  document.getElementById('f-content').value  = pr.content  || '';
  document.getElementById('f-term').value     = pr.term     || '';
}

// carrega preset Meta por padrão
setPreset('meta');

// ── CRIAR LINK ────────────────────────────────────────────────────────────────
const TOKEN = '{{ token }}';

async function criarLink() {
  const url      = document.getElementById('f-url').value.trim();
  const nome     = document.getElementById('f-nome').value.trim();
  const source   = document.getElementById('f-source').value.trim();
  const medium   = document.getElementById('f-medium').value.trim();
  const campaign = document.getElementById('f-campaign').value.trim();
  const content  = document.getElementById('f-content').value.trim();
  const term     = document.getElementById('f-term').value.trim();

  if (!url) { alert('Coloca a URL de destino primeiro!'); return; }

  const btn = document.getElementById('btn-criar');
  btn.disabled = true;
  btn.textContent = 'Gerando...';

  try {
    const r = await fetch('/links/criar?token='+TOKEN, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, nome, utm_source:source, utm_medium:medium,
                            utm_campaign:campaign, utm_content:content, utm_term:term})
    });
    const data = await r.json();
    if (!data.ok) throw new Error(data.erro || 'Erro desconhecido');

    document.getElementById('link-short-txt').textContent = data.short;
    document.getElementById('link-full-txt').textContent  = '↳ ' + data.url_completa;
    document.getElementById('link-result').style.display  = 'block';
    document.getElementById('btn-copy').textContent = '📋 Copiar';
    document.getElementById('btn-copy').classList.remove('ok');

    carregarLinks();
  } catch(e) {
    alert('Erro: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '✦ Gerar Link Curto';
  }
}

function copiarLink() {
  const txt = document.getElementById('link-short-txt').textContent;
  navigator.clipboard.writeText(txt).then(() => {
    const btn = document.getElementById('btn-copy');
    btn.textContent = '✓ Copiado!';
    btn.classList.add('ok');
    setTimeout(() => { btn.textContent = '📋 Copiar'; btn.classList.remove('ok'); }, 2000);
  });
}

// ── CARREGAR FUNIL DE LINKS ───────────────────────────────────────────────────
async function carregarLinks() {
  const wrap = document.getElementById('links-wrap');
  wrap.innerHTML = '<p class="empty">Carregando...</p>';
  try {
    const r    = await fetch('/links/stats?token='+TOKEN);
    const rows = await r.json();

    if (!rows.length) {
      wrap.innerHTML = '<p class="empty">Nenhum link criado ainda. Use o formulário acima!</p>';
      return;
    }

    wrap.innerHTML = rows.map(row => {
      const nome   = row.nome || '/r/'+row.slug;
      const camp   = [row.utm_campaign, row.utm_content].filter(Boolean).join(' · ') || 'sem UTM';
      const src    = row.utm_source ? srcEmoji(row.utm_source)+' '+row.utm_source : '';
      const convTxt= row.cliques > 0
        ? `<span class="funil-pct conv-c">${row.conversao}%</span>` : '';

      return `
      <div class="link-row">
        <div class="link-header">
          <span class="link-name">${escHtml(nome)}</span>
          <span class="link-slug-tag">/r/${row.slug}</span>
        </div>
        <div class="link-camp">${src}${src ? ' · ' : ''}${escHtml(camp)} · criado ${row.criado_em}</div>
        <div class="funil">
          <div class="funil-step">
            <span class="funil-val cliques-c">${row.cliques}</span>
            <span class="funil-label">cliques</span>
          </div>
          <span class="funil-arrow">→</span>
          <div class="funil-step">
            <span class="funil-val vendas-c">${row.vendas}</span>
            <span class="funil-label">vendas</span>
          </div>
          ${convTxt ? `<span class="funil-arrow">→</span>${convTxt}` : ''}
          <span class="funil-arrow">→</span>
          <div class="funil-step">
            <span class="funil-val receita-c">R$ ${row.receita.toFixed(0)}</span>
            <span class="funil-label">receita</span>
          </div>
        </div>
        <div class="link-actions">
          <button class="btn-copy" onclick="copiarSlug('${row.slug}')">📋 Copiar link</button>
          <button class="btn-del" onclick="deletarLink(${row.id})">🗑 Remover</button>
        </div>
      </div>`;
    }).join('');

  } catch(e) {
    wrap.innerHTML = '<p class="empty">Erro ao carregar links</p>';
  }
}

function srcEmoji(s) {
  if (!s) return '🔗';
  s = s.toLowerCase();
  if (s.includes('facebook') || s.includes('fb')) return '📘';
  if (s.includes('google'))   return '🟡';
  if (s.includes('instagram'))return '📸';
  if (s.includes('tiktok'))   return '🎵';
  return '🔗';
}

function copiarSlug(slug) {
  const url = '{{ base_url }}/r/' + slug;
  navigator.clipboard.writeText(url);
}

async function deletarLink(id) {
  if (!confirm('Remover este link?')) return;
  await fetch('/links/deletar/'+id+'?token='+TOKEN, {method:'DELETE'});
  carregarLinks();
}

function atualizar() {
  const icon = document.getElementById('refresh-icon');
  icon.style.display = 'inline-block';
  icon.style.animation = 'spin .6s linear infinite';
  setTimeout(() => location.reload(), 300);
}

function escHtml(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
