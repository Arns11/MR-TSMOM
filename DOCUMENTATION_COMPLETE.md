# Combo XNDX MR LO + TSMOM LO sauf CL
## Documentation Complète

**Version** : 1.0.0  
**Date validation §8.3** : 15 mai 2026  
**Démarrage live** : 15 mai 2026  
**Statut** : VALIDÉE

---

## 1. Vue d'ensemble

Le combo est une stratégie 50/50 entre deux moteurs indépendants et complémentaires :

| Composante | Allocation | Type | Logique |
|---|---|---|---|
| **XNDX MR LO** | 50% | Mean-reversion court terme | Achète Nasdaq sur z-score < -seuil, sort sur z-score ≥ 0 ou time-stop |
| **TSMOM LO sauf CL** | 50% | Trend-following 12 mois | Long ou cash selon signe momentum 252j (sauf CL en long/short) |

**Performances historiques (2005-11-17 à 2026-05-13)** :
- Sharpe : **1.128**
- CAGR : **12.32%**
- MaxDD : **-15.37%**
- Vol annualisée : 11.19%
- Années positives : 19/22 (86%)

**Hold-out 2024-2026** :
- Sharpe : 1.742
- CAGR : 21.84%
- MaxDD : -9.21%

---

## 2. Composante 1 : XNDX MR LO

### Logique
Stratégie de mean-reversion sur le Nasdaq 100 Total Return.

### Paramètres figés
- **Univers** : XNDX (Nasdaq 100 Total Return, ticker EODHD `XNDX.INDX`)
- **Signal** : Z-score = (Close - SMA_N) / std_N
- **Entrée** : z-score ≤ -threshold (achat)
- **Sortie** : z-score ≥ 0 OU time-stop atteint (vente)
- **Time-stop** : 20 jours ouvrés
- **Vol cible** : 20% annualisée
- **Fenêtre vol** : 60 jours
- **Long-only** (pas de short)
- **Levier max théorique** : ~3× (selon vol-targeting)
- **Frais** : 3 bps par trade
- **Exécution** : signal au close J, ordre MOC J+1

### WFO (Walk-Forward Optimization)
- **Phase 1 figée** (1999-2013) : N=5, threshold=1.0
- **Phase 2 WFO** (2014+) : recalibration tous les 2 ans
  - IS : 8 ans
  - OOS : 2 ans
  - Grille N : {5, 7, 10, 12, 15}
  - Grille threshold : {0.9, 1.0, 1.1, 1.3}
  - Critère : Sharpe IS

### État WFO live
Stocké dans `config/wfo_state_xndx_mr.json`. À recalibrer chaque 2 ans.

### Caractéristiques opérationnelles
- ~19 trades par an
- Durée moyenne par trade : 4 jours ouvrés
- En position : 30% du temps
- En cash : 70% du temps
- 71% de trades gagnants

---

## 3. Composante 2 : TSMOM LO sauf CL

### Logique
Trend-following classique sur un panier de 5 actifs, avec une variante importante : **long-only ou cash sur 4 actifs**, **long/short sur CL uniquement**.

### Pourquoi cette asymétrie ?
Analyse historique 2005-2026 : retirer les shorts AMÉLIORE 5 actifs sur 6.
- SPXTR, XNDX, GLD, TLT : les shorts coûtent en moyenne -2 à -3 pp de CAGR
- CL : les shorts rapportent (+2.7 pp de CAGR) car le pétrole a connu plusieurs crashs majeurs

### Paramètres figés
- **Panier** : 5 actifs équipondérés (1/5 = 10% du capital combo chacun)
  - TLT (Treasury 20+ ans)
  - SPXTR (S&P 500 Total Return)
  - XNDX (Nasdaq 100 Total Return)
  - GLD (Or)
  - CL (Pétrole WTI)
- **Long-only** : TLT, SPXTR, XNDX, GLD
- **Long/Short** : CL uniquement
- **Signal momentum** : sign(rendement 252 jours)
- **Rebalance** : tous les 20 jours ouvrés glissants
- **Vol cible** : 21% (calibré pour vol réalisée combo ~11%)
- **Fenêtre vol** : 60 jours
- **Cap levier par actif** : 2×
- **Frais** : 3 bps par rebalance
- **Borrow shorts CL** : 1% annuel (estimation IBKR margin)
- **Exécution** : signal au close J, ordre MOC J+1

### Logique signal
- Si rendement 252j > 0 → LONG (long-only) ou LONG (long/short)
- Si rendement 252j < 0 → CASH (long-only) ou SHORT (long/short)
- Taille position = vol_target / vol_60j_annualisée, capée à 2×

---

## 4. Combo : allocation 50/50 FIXE

**Décision** : allocation strictement 50/50 entre MR et TSMOM, **pas de rebalancing dynamique**.

Tests d'allocation dynamique effectués (Sharpe glissant 63j/126j/252j, SMA200, etc.) :
- Gains marginaux non significatifs
- Aucune méthode dynamique ne bat le 50/50 sur IS+OOS
- Conclusion : on garde la simplicité

**Corrélation MR vs TSMOM** : 0.38 sur 2005-2026 (faible, donc combo additif).

---

## 5. Robustesse de la décision LO sauf CL

Vérification sur 3 sous-périodes indépendantes :

| Actif | 2005-2012 | 2012-2019 | 2019-2026 | Décision retenue |
|---|---|---|---|---|
| TLT | LO ✅ | LO ✅ | L/S ❌ | **LO (2/3)** |
| SPXTR | L/S ❌ | LO ✅ | LO ✅ | **LO (2/3)** |
| XNDX | LO ✅ | LO ✅ | LO ✅ | **LO (3/3) parfait** |
| GLD | LO ✅ | L/S ❌ | LO ✅ | **LO (2/3)** |
| CL | LO ❌ | L/S ✅ | L/S ✅ | **L/S (2/3)** |

Tous les actifs sont robustes à majorité 2/3 minimum. Décision validée.

---

## 6. Véhicules d'exécution

### Version "petit capital" (10k - 150k EUR) : ETF + CFD

| Actif théorique | Instrument live | Granularité |
|---|---|---|
| XNDX | QQQ (ETF) | Excellente (1 share ~600 USD) |
| SPXTR | SPY ou VOO (ETF) | Excellente |
| GLD | GLD (ETF) | Excellente (1 share ~200 USD) |
| TLT | TLT (ETF) | Excellente (1 share ~95 USD) |
| CL | CFD WTI IBKR (perpetual rolling) | Fine, pas de financement overnight |

**Avantages** :
- Granularité parfaite
- Short possible sur compte margin IBKR (TLT, etc.)
- Capital min : 10 000 EUR

**Inconvénients** :
- Coût de financement pour les CFD (sauf WTI qui est perpetual rolling)
- Coût de borrow pour les shorts (~1%/an typiquement sur margin IBKR)

### Version "capital conséquent" (≥ 150k EUR) : Futures

| Actif | Future | Mult | Marge IBKR (mai 2026) |
|---|---|---|---|
| XNDX | MNQ | 2 USD/pt | 2 200 USD |
| SPXTR | MES | 5 USD/pt | 1 600 USD |
| GLD | MGC | 10 USD/oz | 1 100 USD |
| CL | MCL | 100 USD/baril | 800 USD |
| TLT | ZN | 1 000 USD/pt | 2 000 USD |

**Avantages** :
- Pas de coût de financement
- Marges réduites, capital efficient
- Pas de borrow sur les shorts

**Inconvénients** :
- Granularité grossière (notional gros)
- ZN n'a pas de micro (notional ~95k USD)
- Roulement de contrat à gérer
- **Capital min : 150 000 EUR**

---

## 7. Exécution opérationnelle

### Horaires (DST géré automatiquement)
- **Close NYSE** : 16:00 ET (constant)
- **Alerte email** : 17:00 ET (1h après close, pour fraicheur data EODHD)
- **Heure Paris** :
  - Été (mars-novembre) : 23:00 (DST US et FR alignés)
  - Hiver (novembre-mars) : 22:00 (Paris en hiver, NY en hiver)

### GitHub Actions
2 cron schedules dans `.github/workflows/alert.yml` :
```
- cron: '0 21 * 3-11 1-5'   # Été : 17h ET = 21h UTC
- cron: '0 22 * 11,12,1,2 1-5'  # Hiver : 17h ET = 22h UTC
```

### Garde-fou données
Si EODHD pas à jour :
- Retry 3 fois à 15 min d'intervalle
- Si toujours pas fresh : email d'alerte technique à l'admin

### Ordres
- Tous les ordres sont **MOC J+1** (Market On Close)
- L'abonné reçoit l'alerte à 17h ET (23h/22h Paris)
- Il dispose de ~24h pour préparer ses ordres
- Les ordres MOC s'exécutent au close du jour suivant

---

## 8. Calibration capital min

### Pourquoi 10k EUR en ETF/CFD ?
- L'écart d'arrondi (% du capital) reste < 5% en moyenne
- Suffisant pour répliquer fidèlement les positions
- Granularité QQQ/SPY/GLD/TLT excellente

### Pourquoi 150k EUR en futures ?
- ZN (10Y T-Note) notional ~95k USD
- Allocation cible TLT/IEF ~10% du capital = ~10k USD
- Avec 150k EUR de capital : 15k USD sur TLT → écart d'arrondi gérable
- Avec 50k EUR : écart d'arrondi >20% → pas fidèle

### Warnings dashboard
- Capital < seuil minimum du véhicule choisi : warning rouge
- Levier choisi qui aurait causé un margin call historiquement : warning rouge avec date
- Marge utilisée > 80% du capital sur signal courant : warning orange

---

## 9. Subtilités techniques importantes

### Bug temporel corrigé (15 mai 2026)
Dans le backtest réel avec arrondis (parts entières), il y avait un **décalage d'un jour** :
- Bug : positions placées au close J avec eff[J], PnL calculé J→J+1
- Correction : positions placées au close J avec eff[J+1]
- Impact : Sharpe réel passe de 0.89 à 1.05 (convergence vers théo)

### Mode "ETF v Futures" et impact arrondi
- En ETF/CFD : granularité fine, écart d'arrondi négligeable
- En futures : écart d'arrondi significatif aux petits capitaux
- Backtest théorique = allocation continue, ne tient pas compte des arrondis

### Calibration vol TSMOM
- Vol cible 21% par actif individuel
- Donne une vol réalisée portfolio ~11%
- Si on change cap levier (actuel 2×), la calibration vol doit être refaite

### Indices Total Return obligatoires
- SPX_TR (SPXTR.INDX EODHD) au lieu de SPX prix nu
- NDX_TR (XNDX.INDX EODHD) au lieu de NDX prix nu
- Actions : Adjusted_close EODHD (= TR au niveau action)
- ETF (QQQ, SPY, etc.) : Adjusted_close (dividendes réinvestis)

---

## 10. Backlog

### Court terme
- [ ] Test paper trading IBKR (1-2 mois pour valider exécution)
- [ ] Comptabilité réelle vs backtest (réconciliation hebdomadaire)
- [ ] Documenter procédure roll futures (MNQ, MES, MGC, MCL, ZN)

### Moyen terme
- [ ] Module `execute_orders_paper.py` (connexion IBKR API via ib_insync)
- [ ] Alertes via SMS en plus de l'email (Twilio)
- [ ] Test combo MR + TSMOM en EUR-hedgé (couverture currency risk)

### Long terme (chantiers dédiés)
- [ ] **Momentum asymétrique** : signal long lent (252j) + signal short rapide (40-60j)
  - Hypothèse : marchés montent lentement, baissent vite
  - À tester sur 1980+ pour éviter biais bull market 2005-2026
- [ ] **Stratégie mean-reversion sur les mêmes actifs futures** (complémentaire au TSMOM)
- [ ] Étendre l'univers (commodités agricoles, métaux industriels)

---

## 11. Structure du projet

```
COMBO MR + TSMOM/
├── src/
│   └── strategy.py                    # Logique XNDX MR + TSMOM
├── scripts/
│   ├── download_data.py               # EODHD pour 5 actifs
│   ├── backtest.py                    # Backtest complet
│   └── alert_email.py                 # Alerte hebdo avec garde-fou data
├── dashboard/
│   └── app.py                         # Streamlit 5 onglets
├── config/
│   ├── parameters.json                # Paramètres figés
│   └── wfo_state_xndx_mr.json         # État WFO live
├── data/                              # CSV téléchargés
├── results/                           # Outputs backtest
├── .github/workflows/
│   └── alert.yml                      # GitHub Actions
├── lancer_dashboard_mac.command       # Lanceur Mac
├── lancer_dashboard_windows.bat       # Lanceur Windows
├── lancer_backtest.bat
├── lancer_download.bat
└── DOCUMENTATION_COMPLETE.md          # Ce fichier
```

---

## 12. Contacts & support

- **Données** : EODHD (clé API dans `config/parameters.json`)
- **Broker testé** : Interactive Brokers (IBKR)
- **Emails alerte** : configurés via SendGrid (`SENDGRID_API_KEY` dans GitHub secrets)
- **GitHub repo** : à créer (`Arns11/combo-mr-tsmom`)

---

**Dernière mise à jour** : 15 mai 2026
