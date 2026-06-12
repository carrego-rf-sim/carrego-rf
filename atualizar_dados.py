#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_dados.py
==================
Robô diário do Painel de Carrego RF.

Coleta dados públicos (sem credencial / sem API paga) e gera o arquivo
`dados.json` que o painel lê ao abrir:

  Fontes
  ------
  - Tesouro Direto (preços e taxas D0, JSON público):
        https://www.tesourodireto.com.br/json/br/com/b3/tesourodireto/service/api/treasurybondsinfo.json
    -> taxas reais (NTN-B / IPCA+) e nominais (prefixados / NTN-F) + vencimentos
  - Banco Central (séries públicas SGS, sem credencial):
        Selic meta (432) e CDI a.a. (4389)

  O que é calculado aqui (não vem pronto das fontes)
  --------------------------------------------------
  - Duration de Macaulay (em anos) de cada NTN-B
  - Inflação implícita (breakeven) = (1 + nominal) / (1 + real) - 1,
    interpolando a curva prefixada no vencimento de cada NTN-B.

Uso:
    python atualizar_dados.py            # gera dados.json
    python atualizar_dados.py --selftest # valida a matemática sem rede
"""

import json
import sys
import datetime as dt
from urllib.request import urlopen, Request

TESOURO_URL = ("https://www.tesourodireto.com.br/json/br/com/b3/"
               "tesourodireto/service/api/treasurybondsinfo.json")
SGS_URL = ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.{cod}/dados/"
           "ultimos/1?formato=json")

# vértices que queremos na curva (rótulo -> ano). Se o Tesouro não ofertar
# algum, ele simplesmente não entra; o painel usa o que vier.
TIMEOUT = 25
HOJE = dt.date.today()

# cupons (compostos): NTN-B 6% a.a., NTN-F 10% a.a.
CUPOM_NTNB = (1.06) ** 0.5 - 1.0      # ~2,9563% ao semestre
CUPOM_NTNF = (1.10) ** 0.5 - 1.0      # ~4,8809% ao semestre


# --------------------------------------------------------------------------- #
# Coleta
# --------------------------------------------------------------------------- #
def _get_json(url):
    req = Request(url, headers={"User-Agent": "carrego-rf-bot/1.0"})
    with urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_tesouro():
    """Retorna lista de títulos normalizados a partir do JSON do Tesouro."""
    data = _get_json(TESOURO_URL)
    lista = data["response"]["TrsrBdTradgList"]
    titulos = []
    for item in lista:
        b = item.get("TrsrBd", {})
        nome = b.get("nm", "")
        venc = b.get("mtrtyDt", "")[:10]            # "AAAA-MM-DD"
        # taxa de compra (investir); cai p/ resgate se faltar
        taxa = b.get("anulInvstmtRate") or b.get("anulRedRate")
        if not venc or taxa in (None, 0):
            continue
        titulos.append({"nome": nome, "venc": venc, "taxa": float(taxa)})
    return titulos


def fetch_bacen(cod, default):
    try:
        d = _get_json(SGS_URL.format(cod=cod))
        return float(str(d[-1]["valor"]).replace(",", "."))
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Classificação dos títulos
# --------------------------------------------------------------------------- #
def classifica(nome):
    n = nome.lower()
    if "ipca" in n:
        return "ntnb_cup" if "juros semestrais" in n else "ntnb_prin"
    if "prefixado" in n:
        return "ntnf" if "juros semestrais" in n else "ltn"
    if "selic" in n:
        return "lft"
    return "outro"


def ano_venc(venc):
    return int(venc[:4])


def vertice_label(venc):
    return "NTN-B " + venc[2:4]      # 2032 -> "NTN-B 32"


# --------------------------------------------------------------------------- #
# Cálculos
# --------------------------------------------------------------------------- #
def anos_ate(venc, base=HOJE):
    d = dt.date.fromisoformat(venc)
    return max((d - base).days / 365.25, 0.0001)


def macaulay(venc, taxa_aa, cupom_semestral, base=HOJE):
    """Duration de Macaulay em anos. Cupom 0 => título zero (duration = prazo)."""
    venc_d = dt.date.fromisoformat(venc)
    r = taxa_aa / 100.0
    if cupom_semestral <= 0:
        return anos_ate(venc, base)
    # monta datas de cupom retrocedendo de 6 em 6 meses
    datas = []
    d = venc_d
    while d > base:
        datas.append(d)
        # 6 meses atrás (aprox. por 182,625 dias para evitar mês inválido)
        d = d - dt.timedelta(days=182)
    datas.sort()
    num = den = 0.0
    for i, d in enumerate(datas):
        t = max((d - base).days / 365.25, 0.0001)
        cf = 100.0 * cupom_semestral
        if i == len(datas) - 1:
            cf += 100.0                      # principal no último fluxo
        pv = cf / (1 + r) ** t
        num += t * pv
        den += pv
    return num / den if den else anos_ate(venc, base)


def curva_nominal(titulos):
    """(anos, taxa) ordenado, a partir de LTN + NTN-F (taxas nominais)."""
    pts = []
    for t in titulos:
        c = classifica(t["nome"])
        if c in ("ltn", "ntnf"):
            pts.append((anos_ate(t["venc"]), t["taxa"]))
    pts.sort()
    return pts


def interp(pts, x):
    """Interpolação linear simples; extrapola plano nas pontas."""
    if not pts:
        return None
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def implicita(venc, taxa_real, pts_nominal):
    """Breakeven de inflação no vencimento da NTN-B (em % a.a.)."""
    nominal = interp(pts_nominal, anos_ate(venc))
    if nominal is None:
        return 0.0
    bk = (1 + nominal / 100.0) / (1 + taxa_real / 100.0) - 1
    return round(bk * 100, 4)


# --------------------------------------------------------------------------- #
# Montagem do dados.json
# --------------------------------------------------------------------------- #
def montar(titulos, selic, cdi):
    pts_nom = curva_nominal(titulos)
    ntnb = {}
    for t in titulos:
        c = classifica(t["nome"])
        if c not in ("ntnb_cup", "ntnb_prin"):
            continue
        venc = t["venc"]
        lbl = vertice_label(venc)
        cupom = CUPOM_NTNB if c == "ntnb_cup" else 0.0
        du = round(macaulay(venc, t["taxa"], cupom), 2)
        impl = implicita(venc, t["taxa"], pts_nom)
        # prioriza a cuponada caso haja duas no mesmo vértice
        if lbl not in ntnb or c == "ntnb_cup":
            ntnb[lbl] = {
                "v": lbl, "venc": venc,
                "anbima": round(t["taxa"], 4), "expect": round(t["taxa"], 4),
                "du": du, "impl": impl,
            }
    curva = sorted(ntnb.values(), key=lambda r: r["venc"])
    return {
        "date": HOJE.strftime("%d/%m/%Y"),
        "selic": round(selic, 2),
        "cdi": round(cdi, 2),
        "fonte": "Tesouro Direto + BACEN",
        "atualizado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "ntnb": curva,
    }


def main():
    titulos = fetch_tesouro()
    selic = fetch_bacen(432, 14.40)     # Meta Selic % a.a.
    cdi = fetch_bacen(4389, selic)      # CDI a.a. base 252
    saida = montar(titulos, selic, cdi)
    if len(saida["ntnb"]) < 3:
        raise SystemExit("ERRO: poucos vértices NTN-B coletados; nada gravado.")
    with open("dados.json", "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2)
    print(f"OK: {len(saida['ntnb'])} vértices · {saida['date']} · "
          f"Selic {saida['selic']} · CDI {saida['cdi']}")


# --------------------------------------------------------------------------- #
# Auto-teste (roda sem rede)
# --------------------------------------------------------------------------- #
def selftest():
    base = dt.date(2026, 6, 11)
    global HOJE
    HOJE = base
    titulos = [
        {"nome": "Tesouro Prefixado 2028", "venc": "2028-01-01", "taxa": 13.50},
        {"nome": "Tesouro Prefixado com Juros Semestrais 2033",
         "venc": "2033-01-01", "taxa": 13.80},
        {"nome": "Tesouro IPCA+ 2029", "venc": "2029-05-15", "taxa": 7.60},
        {"nome": "Tesouro IPCA+ com Juros Semestrais 2032",
         "venc": "2032-08-15", "taxa": 7.96},
        {"nome": "Tesouro IPCA+ com Juros Semestrais 2045",
         "venc": "2045-05-15", "taxa": 7.42},
    ]
    out = montar(titulos, 14.40, 14.40)
    assert len(out["ntnb"]) == 3, out["ntnb"]
    for r in out["ntnb"]:
        # duration positiva e menor que o prazo até o vencimento
        prazo = anos_ate(r["venc"], base)
        assert 0 < r["du"] <= prazo + 0.01, r
        # implícita num intervalo plausível (0% a 12%)
        assert -1 < r["impl"] < 12, r
        print(f"  {r['v']:9s} venc {r['venc']}  dur {r['du']:5.2f}a  "
              f"real {r['anbima']:.2f}%  implícita {r['impl']:.2f}%")
    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
