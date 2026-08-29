# Faire tourner le moniteur 24/7 gratuitement avec GitHub Actions

Ce guide met en ligne ton moniteur One Piece pour qu'il tourne **tout seul,
toutes les ~5 minutes, sans que ton PC soit allumé** — gratuitement, grâce à
GitHub Actions.

Le principe : GitHub exécute `monitor.py --once` toutes les 5 min sur ses
serveurs. L'état (la liste des produits déjà vus) est mémorisé dans le fichier
`state.json`, réécrit dans ton dépôt à chaque changement. Tes identifiants
Telegram ne sont **jamais** dans le code : ils vivent dans les « Secrets » du
dépôt.

---

## Ce que contient ce dossier

```
monitor.py                     ← le script (identique à ta version locale)
requirements.txt               ← dépendances
config.ini                     ← réglages du site (SANS identifiants Telegram)
.gitignore
.github/workflows/monitor.yml  ← la tâche planifiée (cron 5 min)
GUIDE_GITHUB.md                ← ce guide
```

---

## Étape 1 — Créer un compte GitHub (si tu n'en as pas)

Va sur https://github.com et crée un compte gratuit.

## Étape 2 — Créer un dépôt

Clique sur **New repository** (bouton vert, ou https://github.com/new).

- Donne-lui un nom, ex. `optcg-monitor`.
- **Choisis « Public »** — important : les dépôts **publics** ont des minutes
  GitHub Actions **illimitées et gratuites**. Un dépôt **privé** n'a que
  2000 min/mois gratuites, ce qui serait dépassé en quelques jours à raison
  d'un passage toutes les 5 min. Ton code n'a rien de sensible (les
  identifiants Telegram restent dans les Secrets, pas dans le code), donc
  public est parfaitement sûr ici.
- Laisse le reste par défaut, clique **Create repository**.

## Étape 3 — Envoyer les fichiers dans le dépôt

Le plus simple depuis un navigateur :

1. Sur la page du dépôt, clique **Add file → Upload files**.
2. Glisse-dépose `monitor.py`, `requirements.txt`, `config.ini`, `.gitignore`.
3. Clique **Commit changes**.

⚠️ Le fichier du workflow doit être à un chemin précis. Le plus fiable :

4. Clique **Add file → Create new file**.
5. Dans le champ du nom, tape exactement :
   `.github/workflows/monitor.yml`
   (les `/` créent automatiquement les dossiers).
6. Colle dedans tout le contenu du fichier `monitor.yml` fourni.
7. Clique **Commit changes**.

## Étape 4 — Ajouter tes identifiants Telegram en « Secrets »

Dans ton dépôt : **Settings → Secrets and variables → Actions →
New repository secret**. Crée **deux** secrets :

| Nom du secret          | Valeur                                  |
|------------------------|-----------------------------------------|
| `TELEGRAM_BOT_TOKEN`   | ton token BotFather (`123456789:AAE…`)  |
| `TELEGRAM_CHAT_ID`     | ton chat_id (le nombre)                 |

⚠️ Respecte **exactement** ces noms (majuscules comprises). Ces valeurs sont
chiffrées et invisibles, même pour toi, une fois enregistrées.

## Étape 5 — Activer et tester

1. Va dans l'onglet **Actions** du dépôt. Si GitHub demande d'activer les
   workflows, accepte.
2. Clique sur le workflow **« Moniteur One Piece »** dans la liste de gauche.
3. Clique **Run workflow → Run workflow** (déclenchement manuel).
4. Au bout de ~30 s, tu devrais recevoir sur Telegram le message
   « 🟢 Moniteur démarré ». 🎉

À partir de là, c'est automatique : GitHub relance le script **toutes les
~5 minutes**, et tu reçois une alerte à chaque nouveau produit ou retour en
stock. Tu peux fermer ton PC, tout tourne côté GitHub.

---

## Bon à savoir

- **Ponctualité** : les crons GitHub visent 5 min mais peuvent être retardés
  de quelques minutes en cas de forte charge (surtout en haut de l'heure), et
  très rarement un passage est sauté. Sans importance pour surveiller une
  boutique.
- **Suivre l'activité** : onglet **Actions** → tu vois chaque exécution (verte
  = OK). Clique dedans pour lire les logs (nombre de produits vus, etc.).
- **Voir l'état mémorisé** : le fichier `state.json` apparaît dans ton dépôt
  après le 1er passage et se met à jour quand le catalogue change.
- **Mise en pause de 60 jours** : GitHub désactive un cron si le dépôt n'a
  eu **aucune activité pendant 60 jours**. Nos commits d'état comptent comme
  activité ; si jamais c'est désactivé, un simple clic « Enable workflow »
  suffit à relancer.
- **Changer de catégorie / de réglages** : édite `config.ini` directement sur
  GitHub (crayon ✏️ → Commit). Pas besoin de toucher au reste.
- **Arrêter** : onglet Actions → le workflow → « … » → **Disable workflow**.

## Ajouter une boutique à surveiller

Tout se passe dans `config.ini`, sans toucher au code. Une « source » = une
boutique + **une** collection. Le moniteur y détecte à la fois les nouveaux
produits et les retours en stock.

Pour une boutique **Shopify** (ses URLs contiennent `/collections/` et
`/products/`), copie ce bloc à la fin du fichier :

```ini
[source:mon-nom-court]
platform   = shopify
base_url   = https://laboutique.com
collection = le-handle-de-la-collection
label      = Nom affiché dans les alertes
```

Le `collection` est le morceau d'URL juste après `/collections/`. Si le site
est servi sous un préfixe de langue (ex. `.../en/collections/...`), mets ce
préfixe dans `base_url` : `https://laboutique.com/en`.

Deux points utiles :

- Pour vérifier qu'une boutique est bien du Shopify, ouvre
  `https://laboutique.com/collections/<handle>/products.json` dans ton
  navigateur : tu dois voir du JSON.
- Une collection ne contient parfois qu'une partie du catalogue. Vérifie le
  nombre de produits avant de te fier à une seule collection — sur CardLab,
  la collection `one-piece` n'en contient qu'un seul, d'où les cinq sources.

Pour désactiver une source sans la supprimer : `enabled = false`.
La première exécution d'une nouvelle source sert de référence et n'envoie
aucune alerte — tu ne seras donc pas noyé sous les notifications.

## Sécurité

Ne mets **jamais** ton token Telegram dans `config.ini` ou dans le code sur un
dépôt public. Il ne doit exister que dans les **Secrets** (Étape 4). Le
`config.ini` fourni a volontairement les champs Telegram vides.
