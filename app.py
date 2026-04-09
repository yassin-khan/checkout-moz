# =============================================================================
#  CHECKOUT NATIVO - MOÇAMBIQUE (M-Pesa & e-Mola)
#  Backend: Python + Flask
#  Autor: Guia passo-a-passo para yassin
# =============================================================================

from flask import (
    Flask, request, jsonify, send_file,
    render_template, abort, redirect, url_for
)
import sqlite3
import uuid
import os
import requests
import base64
import json
import hmac
import hashlib
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)

# =============================================================================
# SECÇÃO 1 — CONFIGURAÇÕES (preencha com os seus dados reais)
# =============================================================================

# --- Produto ---
PRODUCT_NAME    = "Ebook: [Nome do seu Ebook]"
PRODUCT_PRICE   = 350        # Valor em MZN (Meticais)
PDF_FILENAME    = "ebook.pdf" # Nome do ficheiro PDF que vai carregar no servidor

# --- M-Pesa Vodacom Mozambique ---
MPESA_API_KEY             = os.environ.get("MPESA_API_KEY", "COLOQUE_AQUI_A_SUA_API_KEY")
MPESA_PUBLIC_KEY          = os.environ.get("MPESA_PUBLIC_KEY", "COLOQUE_AQUI_A_PUBLIC_KEY")
MPESA_SERVICE_PROVIDER    = os.environ.get("MPESA_SP_CODE", "XXXXXXX")  # Código de lojista
MPESA_BASE_URL            = "https://api.vm.co.mz/m-pesa/v1"
MPESA_CALLBACK_URL        = os.environ.get("MPESA_CALLBACK_URL", "https://SEU-SITE.onrender.com/webhook/mpesa")

# --- e-Mola Movitel ---
EMOLA_API_KEY             = os.environ.get("EMOLA_API_KEY", "COLOQUE_AQUI_A_SUA_API_KEY")
EMOLA_SECRET              = os.environ.get("EMOLA_SECRET", "COLOQUE_AQUI_O_SECRET")
EMOLA_MERCHANT_ID         = os.environ.get("EMOLA_MERCHANT_ID", "XXXXXXX")
EMOLA_BASE_URL            = "https://api.emola.co.mz/v1"
EMOLA_CALLBACK_URL        = os.environ.get("EMOLA_CALLBACK_URL", "https://SEU-SITE.onrender.com/webhook/emola")

# --- Segurança ---
WEBHOOK_SECRET            = os.environ.get("WEBHOOK_SECRET", "uma_chave_secreta_longa_e_aleatoria_123!")
DOWNLOAD_TOKEN_EXPIRY_MIN = 30  # O token de download expira em 30 minutos

# =============================================================================
# SECÇÃO 2 — BASE DE DADOS (SQLite — ficheiro local no servidor)
# =============================================================================

DB_PATH = os.path.join(os.path.dirname(__file__), "pagamentos.db")

def init_db():
    """Cria as tabelas da base de dados se não existirem."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS transacoes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            referencia       TEXT    UNIQUE NOT NULL,
            telefone         TEXT    NOT NULL,
            operadora        TEXT    NOT NULL,
            valor            REAL    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'PENDENTE',
            token_download   TEXT,
            criado_em        TEXT    NOT NULL,
            expira_em        TEXT,
            dados_callback   TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# =============================================================================
# SECÇÃO 3 — INTEGRAÇÃO M-PESA (Vodacom Mozambique)
# =============================================================================

def mpesa_get_token():
    """
    Gera o Bearer Token da M-Pesa.
    A M-Pesa usa RSA para encriptar a API Key com a Public Key.
    """
    try:
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_v1_5

        public_key_pem = f"""-----BEGIN PUBLIC KEY-----
{MPESA_PUBLIC_KEY}
-----END PUBLIC KEY-----"""
        rsa_key = RSA.import_key(public_key_pem)
        cipher  = PKCS1_v1_5.new(rsa_key)
        encrypted = cipher.encrypt(MPESA_API_KEY.encode("utf-8"))
        token = base64.b64encode(encrypted).decode("utf-8")
        return token
    except Exception as e:
        print(f"[ERRO M-Pesa Token] {e}")
        return None


def mpesa_initiate_payment(telefone: str, valor: float, referencia: str) -> dict:
    """
    Inicia um pagamento C2B na M-Pesa.
    O cliente recebe um pedido USSD no telemóvel para confirmar.
    """
    token = mpesa_get_token()
    if not token:
        return {"success": False, "message": "Erro ao gerar token M-Pesa"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": "*"
    }

    # Formatar número: remover +258 ou 258 do início
    numero = telefone.strip().replace("+", "").replace(" ", "")
    if numero.startswith("258"):
        numero = numero[3:]

    payload = {
        "input_TransactionReference": referencia,
        "input_CustomerMSISDN":       f"258{numero}",
        "input_Amount":               str(int(valor)),
        "input_ThirdPartyReference":  referencia[:20],
        "input_ServiceProviderCode":  MPESA_SERVICE_PROVIDER,
        "input_InitiatorIdentifier":  MPESA_API_KEY[:10],
        "input_SecurityCredential":   token,
        "input_CallBackChannel":      "3",
        "input_CallBackDestination":  MPESA_CALLBACK_URL,
        "input_PurchasedItemsDesc":   PRODUCT_NAME[:100]
    }

    try:
        resp = requests.post(
            f"{MPESA_BASE_URL}/c2bPayment/singleStage/",
            headers=headers,
            json=payload,
            timeout=30
        )
        data = resp.json()
        print(f"[M-Pesa Response] {data}")

        if resp.status_code in [200, 201] and data.get("output_ResponseCode") == "INS-0":
            return {
                "success": True,
                "conversation_id": data.get("output_ConversationID", ""),
                "message": "Pedido enviado! Verifique o seu telemóvel para confirmar."
            }
        else:
            return {
                "success": False,
                "message": data.get("output_ResponseDesc", "Erro na M-Pesa. Tente novamente.")
            }
    except requests.exceptions.Timeout:
        return {"success": False, "message": "Tempo esgotado. Tente novamente."}
    except Exception as e:
        print(f"[ERRO M-Pesa] {e}")
        return {"success": False, "message": "Erro de ligação com M-Pesa."}


# =============================================================================
# SECÇÃO 4 — INTEGRAÇÃO e-MOLA (Movitel)
# =============================================================================

def emola_get_token() -> str | None:
    """Autentica na API e-Mola e devolve o access token."""
    try:
        credentials = base64.b64encode(
            f"{EMOLA_API_KEY}:{EMOLA_SECRET}".encode()
        ).decode()

        resp = requests.post(
            f"{EMOLA_BASE_URL}/oauth/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={"grant_type": "client_credentials"},
            timeout=20
        )
        return resp.json().get("access_token")
    except Exception as e:
        print(f"[ERRO e-Mola Token] {e}")
        return None


def emola_initiate_payment(telefone: str, valor: float, referencia: str) -> dict:
    """Inicia um pagamento na e-Mola."""
    token = emola_get_token()
    if not token:
        return {"success": False, "message": "Erro ao autenticar na e-Mola"}

    numero = telefone.strip().replace("+", "").replace(" ", "")
    if numero.startswith("258"):
        numero = numero[3:]

    payload = {
        "msisdn":       f"258{numero}",
        "amount":       str(int(valor)),
        "reference":    referencia,
        "merchant_id":  EMOLA_MERCHANT_ID,
        "callback_url": EMOLA_CALLBACK_URL,
        "description":  PRODUCT_NAME[:100]
    }

    try:
        resp = requests.post(
            f"{EMOLA_BASE_URL}/payments/initiate",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )
        data = resp.json()
        print(f"[e-Mola Response] {data}")

        if resp.status_code in [200, 201] and data.get("status") in ["pending", "initiated", "success"]:
            return {
                "success": True,
                "transaction_id": data.get("transaction_id", ""),
                "message": "Pedido enviado! Verifique o seu telemóvel para confirmar."
            }
        else:
            return {
                "success": False,
                "message": data.get("message", "Erro na e-Mola. Tente novamente.")
            }
    except Exception as e:
        print(f"[ERRO e-Mola] {e}")
        return {"success": False, "message": "Erro de ligação com e-Mola."}


# =============================================================================
# SECÇÃO 5 — FUNÇÕES AUXILIARES
# =============================================================================

def gerar_token_download() -> str:
    """Gera um token único e seguro para o download."""
    return secrets_token()

def secrets_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)

def criar_transacao(referencia: str, telefone: str, operadora: str, valor: float):
    """Regista uma nova transação na base de dados."""
    db = get_db()
    db.execute(
        """INSERT INTO transacoes (referencia, telefone, operadora, valor, criado_em)
           VALUES (?, ?, ?, ?, ?)""",
        (referencia, telefone, operadora, valor, datetime.utcnow().isoformat())
    )
    db.commit()
    db.close()

def confirmar_pagamento(referencia: str, dados_callback: dict):
    """
    Marca a transação como PAGA e gera o token de download seguro.
    Chamado pelo webhook quando a operadora confirma o pagamento.
    """
    token    = secrets_token()
    expira   = (datetime.utcnow() + timedelta(minutes=DOWNLOAD_TOKEN_EXPIRY_MIN)).isoformat()
    db       = get_db()
    db.execute(
        """UPDATE transacoes
           SET status='PAGO', token_download=?, expira_em=?, dados_callback=?
           WHERE referencia=?""",
        (token, expira, json.dumps(dados_callback), referencia)
    )
    db.commit()
    db.close()
    return token

def buscar_transacao_por_referencia(referencia: str):
    db = get_db()
    row = db.execute(
        "SELECT * FROM transacoes WHERE referencia=?", (referencia,)
    ).fetchone()
    db.close()
    return dict(row) if row else None

def buscar_transacao_por_token(token: str):
    db = get_db()
    row = db.execute(
        "SELECT * FROM transacoes WHERE token_download=?", (token,)
    ).fetchone()
    db.close()
    return dict(row) if row else None


# =============================================================================
# SECÇÃO 6 — ROTAS DO SERVIDOR (URLs do site)
# =============================================================================

@app.route("/")
def pagina_checkout():
    """Página principal de checkout."""
    return render_template(
        "checkout.html",
        produto=PRODUCT_NAME,
        preco=PRODUCT_PRICE
    )


@app.route("/iniciar-pagamento", methods=["POST"])
def iniciar_pagamento():
    """
    Recebe o formulário do cliente e inicia o pagamento.
    Chamado pelo JavaScript da página de checkout.
    """
    dados = request.get_json()
    if not dados:
        return jsonify({"success": False, "message": "Dados inválidos"}), 400

    telefone  = dados.get("telefone", "").strip()
    operadora = dados.get("operadora", "").strip().lower()  # "mpesa" ou "emola"

    # --- Validação básica ---
    if not telefone or len(telefone) < 9:
        return jsonify({"success": False, "message": "Número de telemóvel inválido"}), 400
    if operadora not in ["mpesa", "emola"]:
        return jsonify({"success": False, "message": "Escolha M-Pesa ou e-Mola"}), 400

    # --- Gerar referência única ---
    referencia = f"CHK-{uuid.uuid4().hex[:12].upper()}"

    # --- Registar na base de dados ---
    criar_transacao(referencia, telefone, operadora, PRODUCT_PRICE)

    # --- Chamar API da operadora ---
    if operadora == "mpesa":
        resultado = mpesa_initiate_payment(telefone, PRODUCT_PRICE, referencia)
    else:
        resultado = emola_initiate_payment(telefone, PRODUCT_PRICE, referencia)

    if resultado["success"]:
        return jsonify({
            "success": True,
            "referencia": referencia,
            "message": resultado["message"]
        })
    else:
        return jsonify({
            "success": False,
            "message": resultado["message"]
        }), 400


@app.route("/verificar-pagamento/<referencia>")
def verificar_pagamento(referencia: str):
    """
    Verifica o estado de uma transação.
    O JavaScript da página chama esta rota a cada 5 segundos (polling).
    """
    transacao = buscar_transacao_por_referencia(referencia)
    if not transacao:
        return jsonify({"status": "NAO_ENCONTRADO"}), 404

    if transacao["status"] == "PAGO":
        return jsonify({
            "status": "PAGO",
            "token": transacao["token_download"],
            "message": "Pagamento confirmado! O seu download está pronto."
        })

    return jsonify({"status": transacao["status"]})


# =============================================================================
# SECÇÃO 7 — WEBHOOKS (Callbacks das operadoras)
# =============================================================================

@app.route("/webhook/mpesa", methods=["POST"])
def webhook_mpesa():
    """
    A Vodacom envia para esta URL a confirmação do pagamento.
    NUNCA altere o caminho desta rota sem actualizar na plataforma M-Pesa.
    """
    dados = request.get_json(force=True) or {}
    print(f"[WEBHOOK M-Pesa] Dados recebidos: {dados}")

    # A M-Pesa envia: output_ResponseCode = "INS-0" para pagamento bem sucedido
    codigo    = dados.get("output_ResponseCode", "")
    referencia = dados.get("output_ThirdPartyReference", "")

    if codigo == "INS-0" and referencia:
        token = confirmar_pagamento(referencia, dados)
        print(f"[WEBHOOK M-Pesa] Pagamento confirmado. Ref={referencia} Token={token}")
        return jsonify({"output_ResponseDesc": "Accepted"}), 200

    print(f"[WEBHOOK M-Pesa] Pagamento NÃO confirmado. Código={codigo}")
    return jsonify({"output_ResponseDesc": "Received"}), 200


@app.route("/webhook/emola", methods=["POST"])
def webhook_emola():
    """
    A Movitel envia para esta URL a confirmação do pagamento e-Mola.
    """
    dados = request.get_json(force=True) or {}
    print(f"[WEBHOOK e-Mola] Dados recebidos: {dados}")

    # A e-Mola envia: status = "completed" ou "success"
    status     = dados.get("status", "").lower()
    referencia = dados.get("reference", "") or dados.get("merchant_reference", "")

    if status in ["completed", "success", "paid"] and referencia:
        token = confirmar_pagamento(referencia, dados)
        print(f"[WEBHOOK e-Mola] Pagamento confirmado. Ref={referencia} Token={token}")
        return jsonify({"message": "OK"}), 200

    print(f"[WEBHOOK e-Mola] Pagamento NÃO confirmado. Status={status}")
    return jsonify({"message": "Received"}), 200


# =============================================================================
# SECÇÃO 8 — ENTREGA NATIVA (Download Seguro do PDF)
# =============================================================================

@app.route("/download/<token>")
def download_ebook(token: str):
    """
    Rota de download seguro.
    Só funciona se:
    1. O token existir na base de dados
    2. O pagamento estiver marcado como PAGO
    3. O token não tiver expirado (30 minutos)
    """
    transacao = buscar_transacao_por_token(token)

    if not transacao:
        abort(403)  # Acesso negado — token inválido

    if transacao["status"] != "PAGO":
        abort(403)  # Acesso negado — pagamento não confirmado

    # Verificar expiração
    if transacao.get("expira_em"):
        expira = datetime.fromisoformat(transacao["expira_em"])
        if datetime.utcnow() > expira:
            abort(410)  # Recurso expirado

    # Caminho do PDF no servidor
    pdf_path = os.path.join(os.path.dirname(__file__), PDF_FILENAME)

    if not os.path.exists(pdf_path):
        print(f"[ERRO] Ficheiro PDF não encontrado: {pdf_path}")
        abort(500)

    return send_file(
        pdf_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{PRODUCT_NAME.replace(' ', '_')}.pdf"
    )


@app.route("/sucesso/<token>")
def pagina_sucesso(token: str):
    """Página de sucesso mostrada após o pagamento confirmado."""
    transacao = buscar_transacao_por_token(token)

    if not transacao or transacao["status"] != "PAGO":
        return redirect(url_for("pagina_checkout"))

    return render_template(
        "sucesso.html",
        produto=PRODUCT_NAME,
        token=token
    )


# =============================================================================
# SECÇÃO 9 — ERROS PERSONALIZADOS
# =============================================================================

@app.errorhandler(403)
def acesso_negado(e):
    return render_template("erro.html",
        titulo="Acesso Negado",
        mensagem="O link de download é inválido ou o pagamento não foi confirmado.",
        codigo=403
    ), 403

@app.errorhandler(410)
def link_expirado(e):
    return render_template("erro.html",
        titulo="Link Expirado",
        mensagem="O seu link de download expirou (válido por 30 minutos). Contacte o suporte.",
        codigo=410
    ), 410

@app.errorhandler(500)
def erro_servidor(e):
    return render_template("erro.html",
        titulo="Erro no Servidor",
        mensagem="Ocorreu um erro. Por favor contacte o suporte.",
        codigo=500
    ), 500


# =============================================================================
# PONTO DE ENTRADA
# =============================================================================

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
