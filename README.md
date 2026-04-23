# 📊 Insider Trading Tracker

> Autonomní virtuální portfolio řízené na základě insider tradingu detekovaného z SEC Form 4 podání.

⚠️ **DISCLAIMER: Toto je čistě vzdělávací projekt. NEJEDNÁ SE o investiční doporučení. Všechny obchody jsou virtuální – žádné skutečné peníze nejsou zapojeny. Historické vzory insider tradingu nezaručují budoucí výnosy. Používejte na vlastní riziko.**

---

## 🏗️ Jak to funguje

```
SEC EDGAR Form 4 → Parsování XML → Scoring insiderů → Virtuální obchody → Dashboard
```

1. **Fetcher** – stahuje čerstvá Form 4 podání z SEC EDGAR (open-market nákupy insiderů)
2. **Scorer** – hodnotí každý nákup na škále 0–100 podle role insidera, jeho hit rate, pravidelnosti obchodů a velikosti transakce
3. **Trader** – autonomní virtuální obchodní engine: nakupuje při skóre ≥ 70, prodává při +25% zisku, -12% ztrátě nebo po 180 dnech
4. **Portfolio** – denní snapshot hodnoty portfolia a výpočet Sharpe, drawdown, win rate
5. **Web Builder** – generuje statický dashboard s Chart.js grafem a tabulkami

## 📈 Scoring Formula

```
skóre = 40 × role_weight + 30 × hit_rate + 20 × opportunistic + 10 × size_zscore
```

| Faktor | Popis | Zdroj |
|--------|-------|-------|
| **Role** (40%) | CEO/CFO = 1.0, Director = 0.6, 10% Owner = 0.4 | Form 4 reporting relationship |
| **Hit Rate** (30%) | % nákupů, po kterých akcie překonala S&P 500 za 6 měsíců | Historická price data |
| **Opportunistic** (20%) | Nepravidelnost obchodů (vyšší = lepší signál) | Koeficient variace intervalů |
| **Size z-score** (10%) | Jak velký je nákup vs. historie insidera | Z-score normalizace |
| **Cluster bonus** (+15) | ≥2 insideři nakoupili stejný ticker za posledních 30 dní | |

## 🚀 Lokální spuštění

### Požadavky
- Python 3.11+
- pip

### Instalace

```bash
# Klonování
git clone https://github.com/<your-username>/insider-tracker.git
cd insider-tracker

# Instalace závislostí
pip install -r requirements.txt

# Spuštění pipeline
python -m src.main
```

Dashboard se vygeneruje do `docs/index.html` – otevřete v prohlížeči.

### Spuštění testů

```bash
pytest tests/ -v
```

## 🌐 GitHub Pages Deployment

1. Pushněte kód na GitHub
2. Jděte do **Settings → Pages**
3. Nastavte **Source** na `Deploy from a branch`
4. Vyberte branch `main` a složku `/docs`
5. Uložte – za pár minut bude dashboard živý

GitHub Actions automaticky aktualizuje data 3× denně v pracovní dny (14:00, 18:00, 22:00 UTC).

## 📁 Struktura projektu

```
insider-tracker/
├── .github/workflows/run.yml    # GitHub Actions cron
├── src/
│   ├── __init__.py
│   ├── config.py                # Všechna nastavení a limity
│   ├── db.py                    # SQLite schema a connection
│   ├── fetcher.py               # SEC EDGAR scraper
│   ├── scorer.py                # Scoring engine
│   ├── trader.py                # Virtuální obchodní engine
│   ├── portfolio.py             # Výpočet metrik
│   ├── web_builder.py           # Generátor HTML dashboardu
│   └── main.py                  # Orchestrátor pipeline
├── tests/
│   ├── test_scorer.py           # Testy scoreru
│   └── test_trader.py           # Testy tradera
├── data/
│   └── portfolio.db             # SQLite databáze (generováno automaticky)
├── docs/
│   ├── index.html               # Dashboard (generováno)
│   └── data.json                # Data pro dashboard (generováno)
├── requirements.txt
├── README.md
└── .gitignore
```

## ⚙️ Konfigurace

Všechny parametry jsou v `src/config.py`:

| Parametr | Hodnota | Popis |
|----------|---------|-------|
| `STARTING_CAPITAL` | $100,000 | Počáteční virtuální kapitál |
| `SCORE_THRESHOLD` | 70 | Minimální skóre pro nákup |
| `TAKE_PROFIT_PCT` | +25% | Take profit úroveň |
| `STOP_LOSS_PCT` | -12% | Stop loss úroveň |
| `MAX_POSITION_PCT` | 5% | Max. alokace na jednu pozici |
| `MAX_HOLD_DAYS` | 180 | Max. doba držení pozice |

## 📊 Datový model (SQLite)

- **insider_transactions** – parsované Form 4 transakce
- **insider_scores** – vypočítaná skóre
- **portfolio_state** – aktuální cash
- **positions** – otevřené pozice
- **virtual_trades** – historie virtuálních obchodů
- **portfolio_history** – denní snapshoty hodnoty portfolia

## 🔒 Rate Limiting & Compliance

- SEC EDGAR vyžaduje User-Agent header → nastaveno v `config.py`
- Rate limit: max 8 req/s (SEC doporučuje max 10)
- Žádné API klíče nejsou potřeba

## 📝 License

MIT

---

⚠️ **Znovu zdůrazňuji: Toto je vzdělávací projekt. Nejedná se o investiční poradenství.  
Insider trading data jsou veřejně dostupná, ale interpretace vyžaduje odborné znalosti.  
Virtuální portfolio používá zjednodušené modely, které nemusí odrážet reálné tržní podmínky.**
