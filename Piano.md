# Piano: Sentiment Analyzer — Notizie vs Mercati

## Obiettivo
Un'app web interattiva che:
1. Raccoglie notizie finanziarie reali (o simulate)
2. Le valuta con Claude API per estrarre un sentiment score
3. Sovrappone il sentiment ai prezzi storici di un indice (es. S&P 500)
4. Permette di esplorare visivamente la correlazione — chi anticipa chi?

---

## Stack tecnico

- **Frontend**: React + Recharts (grafici) + Tailwind CSS
- **Backend/API**: Node.js o Python (FastAPI) — oppure tutto client-side se si usa solo Claude API
- **Dati di prezzo**: Yahoo Finance via `yfinance` (Python) o `yahoo-finance2` (Node)
- **Dati notizie**: NewsAPI (piano gratuito) oppure notizie hard-coded per il prototipo
- **Sentiment**: Claude API (`claude-sonnet-4-20250514`) — chiamata per ogni titolo/articolo
- **Deployment**: opzionale, può restare in locale

---

## Architettura

```
[NewsAPI / notizie hard-coded]
        ↓
[Backend: chiama Claude API per ogni notizia]
  → prompt: "Dai uno score da -1 a +1 a questa notizia finanziaria. Rispondi solo in JSON: {score, sentiment, reasoning}"
        ↓
[Aggrega score per giorno → sentiment_index giornaliero]
        ↓
[Scarica prezzi storici da Yahoo Finance per lo stesso periodo]
        ↓
[Frontend: grafico con doppio asse — prezzo + sentiment sovrapposti]
```

---

## Fasi di sviluppo

### Fase 1 — Prototipo con dati sintetici (niente API esterne)
**Obiettivo**: avere l'interfaccia funzionante end-to-end prima di integrare dati reali.

- Genera un array di notizie fake hard-coded (es. 30 giorni di titoli inventati)
- Genera prezzi simulati (random walk con drift)
- Chiama Claude API per calcolare il sentiment di ogni notizia
- Visualizza il grafico sovrapposto

**Output**: app funzionante al 100%, solo con dati finti

---

### Fase 2 — Integrazione prezzi reali (Yahoo Finance)
**Obiettivo**: sostituire i prezzi simulati con dati reali.

- Aggiungi chiamata a `yfinance` o `yahoo-finance2` per scaricare storico S&P 500 (ticker: `^GSPC`) o altro indice
- Parametro configurabile: ticker, periodo (es. ultimi 6 mesi)
- Allinea le date notizie ↔ date prezzi

---

### Fase 3 — Integrazione notizie reali (NewsAPI)
**Obiettivo**: sostituire le notizie fake con titoli reali.

- Registra account gratuito su [newsapi.org](https://newsapi.org)
- Chiama endpoint `/v2/everything?q=stock+market&language=en&from=...&to=...`
- Estrai titolo + data di pubblicazione
- Passa ogni titolo a Claude per lo score
- Attenzione: piano gratuito NewsAPI limita lo storico a 1 mese e 100 req/giorno

**Alternativa gratuita**: scraping titoli da RSS feed di Reuters o FT (niente API key)

---

### Fase 4 — Analisi della correlazione
**Obiettivo**: rispondere alla domanda originale — le notizie anticipano i prezzi o li seguono?

- Calcola correlazione di Pearson tra sentiment(t) e rendimento(t), (t+1), (t+2)... con lag variabile
- Mostra un grafico a barre della correlazione per ogni lag (es. da -5 a +5 giorni)
- Se la correlazione è massima a lag negativo → le notizie anticipano il mercato
- Se la correlazione è massima a lag positivo → il mercato anticipa le notizie (o le genera)

---

### Fase 5 — UI/UX finale
**Obiettivo**: app bella e usabile.

- Selettore ticker (S&P 500, Nasdaq, singole azioni)
- Selettore periodo (1 mese, 3 mesi, 1 anno)
- Tooltip sui punti del grafico che mostra le notizie di quel giorno
- Pannello laterale con le singole notizie e il loro score
- Indicatore visivo: verde/rosso per sentiment positivo/negativo

---

## Prompt Claude per il sentiment

```
Sei un analista finanziario. Ti darò il titolo di un articolo di notizie.
Valuta il sentiment dal punto di vista dell'impatto sui mercati azionari.

Rispondi SOLO con un oggetto JSON, senza testo aggiuntivo:
{
  "score": <numero da -1.0 (molto negativo) a +1.0 (molto positivo)>,
  "sentiment": <"positive" | "neutral" | "negative">,
  "reasoning": "<spiegazione breve in italiano>"
}

Titolo: "{TITOLO_NOTIZIA}"
```

---

## Struttura file suggerita

```
sentiment-market-app/
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── components/
│   │   │   ├── MarketChart.jsx       # grafico prezzi + sentiment
│   │   │   ├── NewsPanel.jsx         # lista notizie con score
│   │   │   ├── CorrelationChart.jsx  # analisi lag
│   │   │   └── Controls.jsx          # selettori ticker/periodo
│   │   ├── hooks/
│   │   │   └── useSentimentData.js   # fetch + chiamate Claude
│   │   └── utils/
│   │       ├── claudeClient.js       # wrapper Claude API
│   │       └── correlation.js        # calcolo Pearson + lag
│   └── package.json
├── backend/ (opzionale se tutto client-side)
│   ├── main.py                       # FastAPI
│   ├── routes/
│   │   ├── prices.py                 # Yahoo Finance
│   │   └── news.py                   # NewsAPI / RSS
│   └── requirements.txt
└── README.md
```

---

## Note importanti per Claude Code

1. **Chiave API**: la Claude API key va in `.env` come `ANTHROPIC_API_KEY` — non hardcodarla mai
2. **Rate limiting**: se ci sono molte notizie, chiama Claude in batch o con un piccolo delay tra le chiamate per evitare rate limit
3. **Costi**: ogni titolo di notizia è ~50 token → 100 notizie costano pochi centesimi con Sonnet
4. **CORS**: se frontend e backend sono separati, configura CORS nel backend
5. **Fallback**: se NewsAPI non è disponibile, usa sempre le notizie hard-coded della Fase 1 come fallback

---

## Domanda di ricerca finale

> *Le notizie negative precedono i crolli di mercato, o i crolli generano un'ondata di notizie negative?*

L'analisi del lag nella Fase 4 risponderà empiricamente a questa domanda.