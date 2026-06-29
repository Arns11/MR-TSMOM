# Réflexion sur la stratégie d'abonnements
## Combo XNDX MR LO + TSMOM LO sauf CL

**Date** : 15 mai 2026
**Auteur** : analyse stratégique pour Arns

---

## 1. Architecture recommandée : 2 tiers

### Tier 1 : **XNDX MR LO** (entrée de gamme)
- 1 actif unique (QQQ ou MNQ futures)
- Capital min recommandé : **5 000 EUR**
- Fréquence : ~20 alertes par an (quand signal change d'état)
- Sharpe : 0.90
- CAGR : 10%
- MaxDD : -19%

### Tier 2 : **Combo complet** (premium)
- XNDX MR + TSMOM 5 actifs
- Capital min recommandé : **10 000 EUR** (version ETF/CFD) ou **150 000 EUR** (version Futures)
- Fréquence : ~32 alertes par an (20 MR + 12 TSMOM)
- Sharpe : 1.13
- CAGR : 12.3%
- MaxDD : -15%

---

## 2. Pourquoi pas TSMOM seul

J'ai exclu TSMOM en standalone pour plusieurs raisons :

1. **Complexité opérationnelle** : 5 actifs (TLT, SPXTR, XNDX, GLD, CL), avec CL en long/short. Difficile à exécuter pour un abonné débutant.
2. **Granularité** : la version ETF requiert au minimum ~50k EUR pour répliquer fidèlement, et CL via CFD WTI nécessite un broker spécialisé (IBKR Pro). Beaucoup de friction côté client.
3. **Différenciation faible** : TSMOM est une stratégie connue dans la littérature et déjà implémentée par AQR, Man AHL, etc. L'angle commercial est faible.
4. **Sharpe seul (1.01)** moins attractif que combo (1.13) pour un tarif premium

XNDX MR au contraire est :
- **Simple** : 1 actif (QQQ)
- **Original** : peu de produits retail proposent du mean-reversion court terme sur Nasdaq
- **Accessible** : capital min très bas (5k EUR)

---

## 3. Pricing suggéré

### Tier 1 (XNDX MR seul) : **39 EUR/mois** ou **390 EUR/an** (~17% remise)

**Justification** :
- Concurrence retail (Composer, Quantra, signaux divers) : 20-50 EUR/mois pour des stratégies simples
- Tarif accessible aux petits capitaux (5k EUR de capital → 39 EUR/mois = 0.78%/mois de frais = ~9%/an de frais sur 5k. Trop cher en absolu).
- Mais pour 20-30k EUR de capital : 39 EUR/mois = 0.19%/mois = 2.3%/an de frais. Acceptable pour un Sharpe 0.90.

**Risque** : pricing trop bas peut donner l'impression d'une stratégie "low value". Tester aussi 49 EUR/mois.

### Tier 2 (Combo complet) : **129 EUR/mois** ou **1 290 EUR/an** (~17% remise)

**Justification** :
- 3.3× le prix du Tier 1 (justifié par Sharpe +25%, MaxDD -20% relatif, et 5 actifs gérés)
- Capital min 10k EUR → 129 EUR/mois = 15.5%/an de frais. **Trop cher**
- Capital 30k EUR → 129 EUR/mois = 5.2%/an. **Encore cher** mais acceptable si on délivre Sharpe 1.13
- Capital 50k+ EUR → 129 EUR/mois = 3.1%/an. **Cible idéale**

**Conclusion pricing combo** : à 129 EUR/mois, la cible réelle est **30k EUR+ de capital**, pas 10k. Il faut soit baisser le prix pour viser plus bas, soit assumer la cible 30k+ et le communiquer.

---

## 4. Alternatives à considérer

### Pricing à la performance (success fee)
- Frais fixe bas (ex 19 EUR/mois) + 20% des gains au-delà d'un seuil (high-water mark)
- Aligne intérêts client/fournisseur
- Mais **complexité administrative** (calcul mensuel/trimestriel des gains de chaque abonné)
- Et **dépendance forte** aux performances réelles (un mauvais semestre → revenus en chute)

### Pricing freemium
- **Free** : Tier 1 sans signal live, juste backtest accessible
- **Tier 1 payant** : signal live + alertes
- **Tier 2 payant** : combo + analyse mensuelle
- Permet de capter l'audience, convertir 5-10% en payant
- Plus de temps à déployer (site web freemium)

### Trial gratuit
- 1 mois gratuit pour découvrir
- Très efficace pour la conversion mais coût d'acquisition élevé si beaucoup d'inscrits sans conversion

**Ma recommandation** : commencer **simple** avec 2 tiers tarif fixe. Itérer plus tard.

---

## 5. Roadmap de lancement

### Phase 1 (juin-juillet 2026) : Site basique + lancement Tier 1
- Site simple (Notion ou Webflow) qui décrit Tier 1
- Inscription via Stripe (39 EUR/mois)
- Distribution signaux par email automatique
- Cible : 20-50 premiers abonnés en 2 mois

### Phase 2 (août-septembre 2026) : Ouverture Tier 2
- Une fois Tier 1 stabilisé, ouvrir Combo complet à 129 EUR/mois
- Pousser cible patrimoine moyen (30k+ EUR)
- Track record sur 3-4 mois live permet preuve sociale

### Phase 3 (Q4 2026) : Marketing et scaling
- Si traction OK : LinkedIn Ads, partenariats avec créateurs finance
- Témoignages clients pour crédibilité
- Sur 12 mois : objectif réaliste 200-500 abonnés Tier 1 + 30-80 Tier 2

### Revenus potentiels à 12 mois
- 300 abonnés Tier 1 × 39 EUR = 11 700 EUR/mois
- 50 abonnés Tier 2 × 129 EUR = 6 450 EUR/mois
- **Total : ~18 000 EUR/mois récurrents** = ~216k EUR/an
- À ajuster selon conversion réelle

---

## 6. Points d'attention

### Réglementaire
- Statut **conseiller en investissements financiers (CIF)** obligatoire en France si tu donnes des recommandations personnalisées
- Alternatives :
  - **Signal éducationnel** : présenter comme "données pour aide à la décision", pas comme recommandation
  - **Statut tiers** : passer par une plateforme déjà régulée (ex Composer, Quantflow)
  - **Création SAS** avec CIF désigné

### Responsabilité
- Disclaimer fort dans CGV : "performances passées non garantes des performances futures"
- "Chaque abonné est responsable de ses ordres"
- "L'éditeur n'exécute pas les trades"
- Assurance responsabilité civile professionnelle recommandée

### Service client
- Ticketing minimal (email)
- FAQ détaillée pour absorber 80% des questions
- Réactivité essentielle dans les 24 premières heures de chaque signal (les nouveaux abonnés vont avoir des questions sur "comment exécuter")

### Distribution
- Email automatique via SendGrid (déjà setup pour test perso)
- Site abonnés avec dashboard live (Streamlit ou Webflow)
- WhatsApp/Telegram pour alertes urgentes (option avancée)

---

## 7. Prochaines décisions à prendre

1. **Validation tier 1 = XNDX MR seul** ou autre stratégie ? (mon choix : XNDX MR)
2. **Pricing exact** : 39/129 ou 49/149 ? (à tester via sondages prospects)
3. **Statut juridique** : CIF, SAS, freelance simple ?
4. **Plateforme distribution** : Stripe + Webflow simple, ou Composer/Quantflow ?
5. **Période d'engagement** : mensuel sans engagement ? annuel obligatoire ?
6. **Quand publier ?** : attendre 2-3 mois de track record live, ou ouvrir tout de suite ?

---

## 8. Risques principaux

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Drawdown -15% au lancement | Moyenne | Élevé (churn massif) | Communiquer clairement le profil de risque dès l'inscription |
| Problème EODHD/data en live | Moyenne | Critique | Fallback Yahoo déjà en place + alerte admin |
| Concurrence directe (autre prov. similaire) | Basse | Moyen | Originalité XNDX MR difficile à copier |
| Régulation française CIF requise | Élevée | Critique | Démarche statutaire dès Q3 2026 |
| Capital min trop élevé pour public visé | Élevée | Moyen | Adapter pricing au capital cible |

---

**Conclusion** : architecture 2 tiers (XNDX MR seul à 39 EUR + Combo à 129 EUR), lancement par phases en commençant simple, démarche réglementaire CIF en parallèle.
