# Painel de Carrego RF — atualização automática

Página web que replica a tabela de carrego de renda fixa e se atualiza sozinha
todo dia útil, a partir de fontes públicas e gratuitas (sem API paga, sem
credencial).

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `painel-carrego-rf.html` | A página em si. Lê `dados.json` ao abrir. |
| `atualizar_dados.py` | Robô que coleta os dados e gera `dados.json`. |
| `dados.json` | Dados de mercado do dia (gerado pelo robô). |
| `atualizar-dados.yml` | Agenda o robô no GitHub Actions (1x/dia útil). |

## Como funciona

```
GitHub Actions (18:30 BRT, dias úteis)
        │
        ├─ Tesouro Direto (JSON D0)  → taxas e vencimentos
        ├─ Banco Central (SGS)       → Selic e CDI
        ├─ calcula duration e implícita (breakeven)
        ▼
   gera dados.json  →  commit no repositório
        ▼
   GitHub Pages serve a página + dados.json
        ▼
   a mesa abre a URL e vê os números do dia
```

A página tem um fechamento embutido como reserva: se o `dados.json` não estiver
disponível, ela ainda abre com os últimos dados conhecidos.

## Passo a passo (uma vez só)

1. Crie um repositório no GitHub (pode ser **privado**).
2. Suba os 4 arquivos. O workflow precisa ficar em:
   ```
   .github/workflows/atualizar-dados.yml
   ```
   Os outros três (`painel-carrego-rf.html`, `atualizar_dados.py`, `dados.json`)
   ficam na raiz.
3. Em **Settings → Pages**, ative o GitHub Pages na branch `main` (pasta `/root`).
   A URL fica tipo `https://SUA-ORG.github.io/SEU-REPO/painel-carrego-rf.html`.
4. Em **Settings → Actions → General → Workflow permissions**, marque
   **Read and write permissions** (deixa o robô commitar o `dados.json`).
5. Pronto. O robô roda sozinho todo dia útil às 18:30. Para rodar na hora,
   vá na aba **Actions → Atualizar dados do Carrego → Run workflow**.

## Rodar/testar localmente

```bash
python atualizar_dados.py            # gera dados.json com os dados de hoje
python atualizar_dados.py --selftest # testa a matemática sem acessar a rede
```

> Abrindo o HTML direto do disco (file://), o navegador bloqueia a leitura do
> `dados.json` por segurança — a página cai no fechamento embutido. Servido pelo
> Pages (ou por `python -m http.server`), ele carrega normalmente.

## O que vem de cada fonte

- **Tesouro Direto** (`treasurybondsinfo.json`): taxas reais das NTN-B (IPCA+),
  taxas nominais dos prefixados (LTN/NTN-F) e os vencimentos. Reflete o mercado
  secundário de títulos públicos.
- **BACEN / SGS**: Selic meta (série 432) e CDI a.a. (série 4389).
- **Calculado pelo robô**: duration de Macaulay (anos) e inflação implícita
  (breakeven nominal × real interpolado no vencimento).

## Limites honestos

- O Tesouro Direto oferta um **subconjunto** das vértices do mercado secundário,
  então a curva pode ter menos pontos que a tabela original da Expert. O painel
  usa o que estiver disponível.
- A taxa do Tesouro é a indicativa de fechamento — base levemente diferente da
  "Anbima D-1", porém muito próxima e adequada para carrego.
- A implícita é **calculada** (breakeven), não puxada pronta. Se um dia houver
  acesso à API da ANBIMA (curva + breakeven oficiais), basta trocar a coleta no
  `atualizar_dados.py`; o resto continua igual.
