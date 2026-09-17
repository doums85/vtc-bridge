# Premortem — Lancement de PROXIA
_Généré le 25 juillet 2026 · méthode Klein (prospective hindsight) · 8 enquêteurs en parallèle_

## Contexte
- **Objet** : PROXIA, SaaS de dispatch VTC B2B2C. Un « pont » (prouvé en perso) lit les groupes Telegram de dispatch, suit la position GPS des chauffeurs, géocode les courses, alerte sur les courses proches/rentables. Le SaaS : vendre un **bot officiel** aux **chefs** de groupes VTC (ils l'ajoutent à leur groupe ; il dispatche aux chauffeurs membres qui partagent leur position en opt-in).
- **Cible / payeur** : le chef de groupe (B2B2C). **Bénéficiaire** : les chauffeurs membres.
- **Modèle** : abonnement par groupe (49–299 €/mois, hypothèse) + socle gratuit viral.
- **Fondateur** : chauffeur VTC à plein temps, développe seul avec Claude Code.
- **Succès à 6 mois** : des chefs qui PAIENT et RENOUVELLENT ; des chauffeurs qui décrochent réellement des courses grâce au bot.

## Cadre
On est le 25 janvier 2027. 6 mois après le lancement commercial, PROXIA a échoué : aucun chef externe ne renouvelle.

## Les 8 causes de mort (premortem brut)
1. Risque plateforme Telegram (bans / CGU / API).
2. Payeur ≠ bénéficiaire (le chef paie, la valeur va au chauffeur ; chef parfois hostile/désintermédié).
3. Le bot officiel conforme est moins bon que le userbot perso (admin refusé, opt-in position pénible).
4. Cold-start bilatéral dans chaque groupe (pas assez de chauffeurs qui partagent la position).
5. Le parsing/géo ne se généralise pas au-delà des 11 groupes du fondateur.
6. Effondrement de la bande passante du fondateur solo.
7. RGPD / incident de responsabilité.
8. Marché plus petit / fragmenté, ou migration hors Telegram (WhatsApp).

---

## Enquêtes approfondies

### 1 — Risque plateforme Telegram
Le userbot reste en prod « le temps de migrer ». Au 1er pilote payant, le bot officiel pousse des DM en rafale → 429 FloodWait (30 s → 300 s), courses en retard de minutes = sans valeur. Un signalement suspend le bot ; l'activité auto sur le compte perso fait tomber un ban du numéro → perte des groupes source, de l'historique, de la prod, et du gagne-pain de chauffeur.
- **Hypothèse** : Telegram est un canal neutre et stable (c'est un tiers hostile qui peut couper l'infra du jour au lendemain).
- **Signes** : FloodWait de ~0 à plusieurs/heure au-delà de 20-30 DM/min/groupe ; délai course→DM > 60-90 s ; 1er mail « bot reported ».

### 2 — Payeur ≠ bénéficiaire
3 chefs signent en gratuit, les chauffeurs adorent ; au passage à 49 €/mois : « moi j'y gagne quoi ? ». Deux chefs sur cinq vivent de l'apport d'affaires → le dispatch auto les désintermédie → ils retirent le bot et préviennent les autres. À 6 mois, seul le groupe du fondateur « paie ».
- **Hypothèse** : le chef veut optimiser son groupe — alors qu'il veut le contrôler, et parfois le monétiser lui-même.
- **Signes** : conversion gratuit→payant = 0 ; en entretien, les chefs parlent des bénéfices « pour leurs chauffeurs », jamais pour eux.

### 3 — Le bot conforme est moins bon que le userbot
6 chefs sur 10 refusent l'admin « accès messages » à un bot inconnu. L'opt-in position (expire 8 h, batterie, méfiance) tue tout : 7/30 partagent à l'heure de pointe → courses fantômes à des chauffeurs absents. La magie de l'accès total et silencieux du compte perso ne survit pas à la conformité.
- **Hypothèse** : la magie venait de l'algorithme — alors qu'elle venait de l'accès total sans friction d'un compte perso.
- **Signes** : acceptation admin des chefs < 50 % ; < ⅓ des chauffeurs avec position active en pointe.

### 4 — Cold-start dans chaque groupe
Groupe de 40 : 3 activent la position, 37 non. Le bot reste muet (pas de chauffeur proche partageant), les 3 actifs ne décrochent rien et désactivent. Le chef ne renouvelle pas : « personne l'utilise ». Rien ne récompense le 1er chauffeur qui partage avant la masse.
- **Hypothèse** : les chauffeurs partagent par confiance en amont — alors qu'ils n'activent qu'après avoir vu une course tomber (œuf-et-poule).
- **Signes** : opt-in < 30 % les 2 premières semaines ; zéro course matchée avant J+10.

### 5 — Le parsing ne se généralise pas
Lyon : « Part-Dieu → St-Ex » géocodé au hasard ; chaque correction lyonnaise casse un cas parisien. « 4K5 », départ-arrivée collés, vocaux fautifs → recall qui s'effondre en silence. Faux positifs (« retour Courbevoie » pris comme départ → « proche 6 min ») → le chauffeur fonce à vide, désinstalle.
- **Hypothèse** : le parseur encode une compétence générale — alors qu'il encode la connaissance de 11 groupes précis.
- **Signes** : extraction qui chute sur tout groupe non-parisien ; règles codées « en dur » ajoutées à chaque onboarding.

### 6 — Effondrement du fondateur solo
Bug Android vu à un feu rouge → attend 9 jours. OSRM plante un dimanche → un groupe sans dispatch tout un week-end. Tickets non lus. Le volant gagne l'arbitrage (il paie le loyer). À 6 mois, code figé sur un bug de 6 semaines.
- **Hypothèse** : un SaaS 24/7 (vente+support+conformité) tient sur les heures résiduelles d'un homme à plein temps ailleurs.
- **Signes** : réponse support > 24 h dès le mois 2 ; incidents résolus seulement le soir/week-end.

### 7 — RGPD / responsabilité
Un chef en litige exige ses données + celles de ses clients → aucune réponse propre → il part et prévient le réseau. Adresses domicile de clientes exposées → captures dans un groupe de 800. Chauffeur banni qui accuse l'outil ; rumeur « l'appli qui te fait conduire sur ton tel ». Ni temps ni budget avocat.
- **Hypothèse** : traiter géoloc + adresses tiers + paiements sans cadre RGPD, « parce que je suis petit ».
- **Signes** : 1re demande d'accès/suppression sans réponse ; « où sont hébergées les données ? » sans réponse écrite.

### 8 — Marché fragmenté / migration WhatsApp
Les mêmes 30 courses repostées dans 15 groupes ; un chauffeur suit 8-12 groupes. Le chef relaie, ne possède rien. Vrais chefs payeurs = une poignée (40 contactés → 1 teste → 0 renouvelle). À M4, du dispatch sérieux bascule sur WhatsApp → produit Telegram hors-sol.
- **Hypothèse** : un groupe Telegram est un actif captif possédé par un chef — alors que c'est un flux fongible, redondant, volatile.
- **Signes** : recoupement des courses > 70 % ; < 10 % des chefs acceptent une démo, 0 engagement récurrent au mois 1.

---

## Synthèse

**Défaillance la plus probable** — Le chef ne paie pas : payeur ≠ bénéficiaire, ET un groupe n'est pas un actif captif (courses repostées partout, chauffeurs multi-groupes). Premier mur, atteint dès le passage au payant.

**Défaillance la plus dangereuse** — Le ban Telegram du compte/numéro perso : irréversible, il emporte les groupes source, les données ET le gagne-pain de chauffeur. À couvrir même si la proba est moyenne.

**Hypothèse cachée (le cœur)** — « Il y a un produit ici, et il se transfère aux autres. » Ce qui marche pour toi (N=1) ne survit pas à la mise en produit : la magie venait de l'accès total d'un compte perso (pas de l'algo), le parseur encode tes 11 groupes, tu es l'unique expert. Les causes 3, 4, 5 frappent toutes cette illusion.

**Plan révisé**
1. Ne construis pas le SaaS. Valide d'abord : pitch payant à 10 chefs, tente d'encaisser un acompte 49 €/LOI. < 3/10 → thèse morte, pivote.
2. Mesure le recoupement des courses entre groupes ; > 70 % → « par groupe » sans valeur.
3. Garde la valeur B2C solo comme fer de lance (utile à 1 seul chauffeur dès J1) → contourne le cold-start.
4. Sors le userbot du numéro principal ; throttle+jitter ; humain dans la boucle du POST.
5. Extraction LLM + mesure du recall ; n'onboarde pas un groupe sans recall mesuré.
6. Tranche le format : co-fondateur technique, OU B2C self-serve léger, OU outil perso + petit payant.
7. Socle RGPD minimal (FR hosting, pas d'adresses clients stockées, opt-in) avant le 1er client externe.

**Checklist avant lancement**
1. Test « argent » sur 10 chefs (≥ 3 acomptes/LOI).
2. Mesure du recoupement des courses (< 70 %).
3. Test de densité dans 1 groupe ami (≥ 60 % position active 2 semaines).
4. Audit anti-ban (userbot hors numéro principal, FloodWait mesuré).
5. Recall parsing sur un groupe non-fondateur + socle RGPD avant le 1er payant.
