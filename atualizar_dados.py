#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_dados.py
==================
Robô diário do Painel de Carrego RF.

Coleta dados públicos (sem credencial / sem API paga) e gera o arquivo
`dados.json` que o painel lê ao abrir.

  Fontes
  ------
  - Tesouro Transparente — arquivo oficial de preços e taxas do Tesouro Direto
    (CSV diário, público e estável):
      https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv
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
from urllib.parse import quote

# Fonte leve (JSON, resposta rápida) — espelho com a mesma estrutura da
# antiga API do Tesouro Direto (response.TrsrBdTradgList[].TrsrBd).
TESOURO_JSON = "https://api.radaropcoes.com/bonds.json"
# Fonte oficial de reserva: CSV diário do Tesouro Transparente (pesado).
TESOURO_CSV = ("https://www.tesourotransparente.gov.br/ckan/dataset/"
               "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
               "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/"
               "precotaxatesourodireto.csv")
SGS_URL = ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.{cod}/dados/"
           "ultimos/1?formato=json")
# Focus (expectativas anuais): Selic e IPCA por ano de referência
FOCUS_URL = ("https://olinda.bcb.gov.br/olinda/servico/Expectativas/"
             "versao/v1/odata/ExpectativasMercadoAnuais")

TIMEOUT = 30          # fonte leve
TIMEOUT_CSV = 90      # reserva pesada
HOJE = dt.date.today()

# cupons (compostos): NTN-B 6% a.a., NTN-F 10% a.a.
CUPOM_NTNB = (1.06) ** 0.5 - 1.0      # ~2,9563% ao semestre
CUPOM_NTNF = (1.10) ** 0.5 - 1.0      # ~4,8809% ao semestre


# --------------------------------------------------------------------------- #
# Coleta
# --------------------------------------------------------------------------- #
def _get_json(url, timeout=TIMEOUT):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 carrego-rf-bot/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse_br_date(s):
    """'DD/MM/AAAA' -> 'AAAA-MM-DD'."""
    d, m, y = s.strip().split("/")
    return f"{y}-{m.zfill(2)}-{d.zfill(2)}"


def _col(header, name):
    name = name.lower()
    for i, h in enumerate(header):
        if h == name:
            return i
    for i, h in enumerate(header):
        if name in h:
            return i
    raise SystemExit(f"ERRO: coluna '{name}' não encontrada no CSV do Tesouro.")


def fetch_tesouro():
    """Tenta a fonte leve (JSON) e, se falhar, cai no CSV oficial (pesado)."""
    try:
        titulos = _fetch_tesouro_json()
        if len(titulos) >= 3:
            print(f"  fonte: JSON ({len(titulos)} títulos)")
            return titulos
    except Exception as e:
        print(f"  fonte JSON indisponível ({e}); tentando CSV oficial...")
    titulos = _fetch_tesouro_csv()
    print(f"  fonte: CSV oficial ({len(titulos)} títulos)")
    return titulos


def _fetch_tesouro_json():
    """Fonte leve: JSON no formato TrsrBdTradgList[].TrsrBd."""
    data = _get_json(TESOURO_JSON, timeout=TIMEOUT)
    lista = data["response"]["TrsrBdTradgList"]
    titulos = []
    for item in lista:
        b = item.get("TrsrBd", {})
        nome = b.get("nm", "")
        venc = (b.get("mtrtyDt") or "")[:10]
        taxa = b.get("anulInvstmtRate") or b.get("anulRedRate")
        if not nome or not venc or not taxa:
            continue
        titulos.append({"nome": nome, "venc": venc, "taxa": float(taxa)})
    return titulos


def _fetch_tesouro_csv():
    """Baixa o CSV oficial (streaming) e retorna os títulos da data base mais recente.

    Mantém em memória apenas as linhas da última data (à prova de qualquer
    ordenação do arquivo, pois só reinicia ao achar uma data MAIOR).
    """
    req = Request(TESOURO_CSV, headers={"User-Agent": "Mozilla/5.0 carrego-rf-bot/1.0"})
    latest = None
    rows = []
    idx = None
    with urlopen(req, timeout=TIMEOUT_CSV) as resp:
        first = True
        for raw in resp:
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                line = raw.decode("latin-1")
            line = line.strip()
            if not line:
                continue
            parts = line.split(";")
            if first:
                first = False
                header = [c.strip().lower() for c in parts]
                idx = {
                    "tipo": _col(header, "tipo titulo"),
                    "venc": _col(header, "data vencimento"),
                    "base": _col(header, "data base"),
                    "compra": _col(header, "taxa compra manha"),
                    "venda": _col(header, "taxa venda manha"),
                }
                continue
            if len(parts) <= max(idx.values()):
                continue
            try:
                base_iso = _parse_br_date(parts[idx["base"]])
            except Exception:
                continue
            if latest is None or base_iso > latest:
                latest = base_iso
                rows = []
            if base_iso == latest:
                rows.append(parts)

    titulos = []
    for parts in rows:
        tipo = parts[idx["tipo"]].strip()
        try:
            venc = _parse_br_date(parts[idx["venc"]])
        except Exception:
            continue
        raw_taxa = (parts[idx["compra"]].strip() or parts[idx["venda"]].strip())
        taxa = raw_taxa.replace(".", "").replace(",", ".")
        try:
            taxa = float(taxa)
        except Exception:
            continue
        if taxa <= 0 or venc <= (latest or ""):
            continue
        titulos.append({"nome": tipo, "venc": venc, "taxa": taxa})
    if latest:
        global HOJE
        HOJE = dt.date.fromisoformat(latest)   # usa a data base do arquivo
    return titulos


def fetch_bacen(cod, default):
    try:
        d = _get_json(SGS_URL.format(cod=cod))
        return float(str(d[-1]["valor"]).replace(",", "."))
    except Exception:
        return default


def fetch_focus():
    """Projeções anuais do Focus (mediana) p/ Selic e IPCA, por ano de referência.
    Sem $filter (que dava problema de codificação): baixa as linhas mais recentes
    e filtra no Python. Pega a divulgação mais recente. Falha silenciosa -> {}."""
    try:
        url = (FOCUS_URL + "?$format=json&$top=4000"
               "&$orderby=Data%20desc"
               "&$select=Indicador,DataReferencia,Mediana,Data,baseCalculo")
        data = _get_json(url, timeout=45)
        best = {}   # (indicador, ano) -> (Data, Mediana) mais recente
        for r in data.get("value", []):
            ind = r.get("Indicador")
            if ind not in ("Selic", "IPCA"):
                continue
            if r.get("baseCalculo") not in (0, "0", None):
                continue
            ano = str(r.get("DataReferencia", ""))[:4]
            med = r.get("Mediana")
            d = r.get("Data", "")
            if not ano.isdigit() or med is None:
                continue
            k = (ind, ano)
            if k not in best or d > best[k][0]:
                best[k] = (d, float(med))
        selic = {a: v for (i, a), (d, v) in best.items() if i == "Selic"}
        ipca = {a: v for (i, a), (d, v) in best.items() if i == "IPCA"}
        return {"selic": selic, "ipca": ipca}
    except Exception as e:
        print(f"  Focus indisponível ({e}); seguindo sem projeção.")
        return {"selic": {}, "ipca": {}}


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


def vertice_label(venc):
    return "NTN-B " + venc[2:4]      # 2032 -> "NTN-B 32"


# --------------------------------------------------------------------------- #
# Cálculos
# --------------------------------------------------------------------------- #
def anos_ate(venc, base=None):
    base = base or HOJE
    d = dt.date.fromisoformat(venc)
    return max((d - base).days / 365.25, 0.0001)


def macaulay(venc, taxa_aa, cupom_semestral, base=None):
    """Duration de Macaulay em anos. Cupom 0 => título zero (duration = prazo)."""
    base = base or HOJE
    venc_d = dt.date.fromisoformat(venc)
    r = taxa_aa / 100.0
    if cupom_semestral <= 0:
        return anos_ate(venc, base)
    datas = []
    d = venc_d
    while d > base:
        datas.append(d)
        d = d - dt.timedelta(days=182)
    datas.sort()
    num = den = 0.0
    for i, d in enumerate(datas):
        t = max((d - base).days / 365.25, 0.0001)
        cf = 100.0 * cupom_semestral
        if i == len(datas) - 1:
            cf += 100.0
        pv = cf / (1 + r) ** t
        num += t * pv
        den += pv
    return num / den if den else anos_ate(venc, base)


def curva_nominal(titulos):
    """(anos, taxa) ordenado, a partir de LTN + NTN-F (taxas nominais)."""
    pts = []
    for t in titulos:
        if classifica(t["nome"]) in ("ltn", "ntnf"):
            pts.append((anos_ate(t["venc"]), t["taxa"]))
    pts.sort()
    return pts


def interp(pts, x):
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
    nominal = interp(pts_nominal, anos_ate(venc))
    if nominal is None:
        return 0.0
    bk = (1 + nominal / 100.0) / (1 + taxa_real / 100.0) - 1
    return round(bk * 100, 4)


# --------------------------------------------------------------------------- #
# Montagem do dados.json
# --------------------------------------------------------------------------- #
def montar(titulos, selic, cdi, focus=None):
    pts_nom = curva_nominal(titulos)
    ntnb = {}
    ltn = {}
    ntnf = {}
    for t in titulos:
        c = classifica(t["nome"])
        venc = t["venc"]
        ano = venc[:4]
        if c in ("ntnb_cup", "ntnb_prin"):
            lbl = vertice_label(venc)
            cupom = CUPOM_NTNB if c == "ntnb_cup" else 0.0
            du = round(macaulay(venc, t["taxa"], cupom), 2)
            impl = implicita(venc, t["taxa"], pts_nom)
            if lbl not in ntnb or c == "ntnb_cup":
                ntnb[lbl] = {"v": lbl, "venc": venc,
                             "anbima": round(t["taxa"], 4), "expect": round(t["taxa"], 4),
                             "du": du, "impl": impl}
        elif c == "ltn":
            du = round(macaulay(venc, t["taxa"], 0.0), 2)   # zero cupom
            ltn[venc] = {"v": f"LTN {ano}", "venc": venc,
                         "taxa": round(t["taxa"], 4), "du": du}
        elif c == "ntnf":
            du = round(macaulay(venc, t["taxa"], CUPOM_NTNF), 2)
            ntnf[venc] = {"v": f"NTN-F {ano}", "venc": venc,
                          "taxa": round(t["taxa"], 4), "du": du}

    curva = sorted(ntnb.values(), key=lambda r: r["venc"])
    return {
        "date": HOJE.strftime("%d/%m/%Y"),
        "selic": round(selic, 2),
        "cdi": round(cdi, 2),
        "fonte": "Tesouro Transparente + BACEN",
        "atualizado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "ntnb": curva,
        "ltn": sorted(ltn.values(), key=lambda r: r["venc"]),
        "ntnf": sorted(ntnf.values(), key=lambda r: r["venc"]),
        "focus": focus or {"selic": {}, "ipca": {}},
    }


def main():
    titulos = fetch_tesouro()
    selic = fetch_bacen(432, 14.40)     # Meta Selic % a.a.
    cdi = fetch_bacen(4389, selic)      # CDI a.a. base 252
    focus = fetch_focus()               # projeções anuais Selic/IPCA
    saida = montar(titulos, selic, cdi, focus)
    if len(saida["ntnb"]) < 3:
        raise SystemExit(f"ERRO: poucos vértices NTN-B coletados "
                         f"({len(saida['ntnb'])}); nada gravado.")
    with open("dados.json", "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2)
    nf = len(saida["focus"].get("selic", {}))
    print(f"OK: {len(saida['ntnb'])} vértices · {saida['date']} · "
          f"Selic {saida['selic']} · CDI {saida['cdi']} · Focus {nf} anos")


# --------------------------------------------------------------------------- #
# Auto-teste (roda sem rede)
# --------------------------------------------------------------------------- #
def selftest():
    global HOJE
    HOJE = dt.date(2026, 6, 11)
    titulos = [
        {"nome": "Tesouro Prefixado", "venc": "2028-01-01", "taxa": 13.50},
        {"nome": "Tesouro Prefixado com Juros Semestrais", "venc": "2033-01-01", "taxa": 13.80},
        {"nome": "Tesouro IPCA+", "venc": "2029-05-15", "taxa": 7.60},
        {"nome": "Tesouro IPCA+ com Juros Semestrais", "venc": "2032-08-15", "taxa": 7.96},
        {"nome": "Tesouro IPCA+ com Juros Semestrais", "venc": "2045-05-15", "taxa": 7.42},
    ]
    out = montar(titulos, 14.40, 14.40)
    assert len(out["ntnb"]) == 3, out["ntnb"]
    for r in out["ntnb"]:
        prazo = anos_ate(r["venc"], HOJE)
        assert 0 < r["du"] <= prazo + 0.01, r
        assert -1 < r["impl"] < 12, r
        print(f"  {r['v']:9s} venc {r['venc']}  dur {r['du']:5.2f}a  "
              f"real {r['anbima']:.2f}%  implícita {r['impl']:.2f}%")
    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
