# Veille créative : briefing quotidien par IA

Chaque matin, le script lit des flux RSS (cinéma, production, publicité, photo), ne garde que les gros
projets (par mots-clés, ou via une IA au choix), les résume, puis publie une page web (et un e-mail, si vous le
souhaitez). Tout tourne gratuitement sur GitHub, sans serveur à maintenir.

## Coût : zéro

GitHub Actions et GitHub Pages sont gratuits pour un dépôt **public** (Pages sur dépôt privé exige un
abonnement payant). Trois modes pour la sélection, réglables sans toucher au code :

| Mode (`LLM_PROVIDER`) | Coût | Résultat |
|---|---|---|
| `none` (défaut) | Gratuit | Tri par mots-clés et liste de suivi. Titres et extraits restent en anglais. |
| `gemini` | Gratuit (offre gratuite de Google, soumise à quotas) | Sélection plus fine, titres et résumés en français. |
| `claude` | Payant (quelques centimes par jour) | Idem, avec Claude. |

Si l'IA est indisponible ou dépasse son quota, le script bascule automatiquement sur le mode `none`.

## Mise en place (15 minutes)

1. Créez un dépôt GitHub **public** et déposez-y tout ce dossier.
2. Activez la page : Settings > Pages > Source « Deploy from a branch », branche `main`, dossier `/docs`.
3. Lancez un essai : onglet Actions > « Briefing quotidien » > Run workflow. Votre site apparaît à
   l'adresse indiquée dans Settings > Pages. À ce stade, vous êtes en mode `none`.
4. Pour les résumés en français, gratuits : créez une clé sur https://aistudio.google.com, puis dans le dépôt
   Settings > Secrets and variables > Actions : secret `GEMINI_API_KEY` (onglet Secrets) et variable
   `LLM_PROVIDER` = `gemini` (onglet Variables).

Le briefing se régénère ensuite tous les jours à 5h UTC (modifiable dans `.github/workflows/briefing.yml`).

## Personnaliser

Tout se règle dans `sources.json` :
- `liste_de_suivi` : les réalisateurs, sociétés de production et photographes à privilégier. Leurs projets
  sont marqués d'un filet bleu et « Dans votre liste ».
- `flux` : ajoutez ou retirez des sources (nom, URL du flux RSS, catégorie affichée sur la page).
- `max_elements` et `fenetre_heures` : nombre de projets par jour et ancienneté maximale des articles.

## E-mail quotidien (optionnel)

Ajoutez les secrets `SMTP_HOST` (ex. `smtp.gmail.com`), `SMTP_PORT` (`465`), `SMTP_USER`, `SMTP_PASSWORD`
(mot de passe d'application) et `EMAIL_TO`. Sans eux, aucun e-mail n'est envoyé.

## Point d'attention

Les adresses de flux RSS ont été écrites de mémoire et n'ont pas pu être testées. Au premier lancement,
le journal de l'action affiche `[avertissement] NomDuSite : aucun article lu` pour chaque URL invalide :
corrigez-la dans `sources.json`.

Les conditions de l'offre gratuite de Gemini (quotas, nom du modèle, usage des données) peuvent changer :
vérifiez-les sur le site de Google. Le nom du modèle se règle avec la variable `GEMINI_MODEL`.
