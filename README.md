# MUMU

> MUMU pense à tout, sans que les utilisateurs aient l'impression de devoir penser à MUMU.

MUMU est un agent pour la vie de la maison, accessible par WhatsApp. Ce dépôt contient le socle de la bêta.

## Ce que fait cette version

- Reçoit les messages WhatsApp (texte, photo, PDF) via Twilio.
- Comprend le message avec Claude et en tire ce qu'il faut suivre : tâches, échéances, paiements, rendez-vous, courses. Une photo de plusieurs papiers est traitée papier par papier.
- Retient les faits durables sur le foyer. Chaque élément et chaque fait garde un lien vers le message d'origine.
- Répond avec la personnalité de MUMU (`app/persona.py`) et fait l'accueil au premier message.
- Garde le contexte : mémoire du foyer, éléments en cours, dix derniers échanges.
- **Vie connectée** : sur demande, se connecte à Gmail et à Google Agenda en lecture seule, lit les nouveaux mails en arrière-plan, en tire factures, échéances, commandes et rendez-vous, repère les points serrés de l'agenda, et ne prévient que si c'est utile. Voir `docs/vie-connectee.md`.

## Ce qui n'est pas encore là

- Vocaux (MUMU dit qu'il ne sait pas encore les écouter).
- Rappels programmés, question du jour, récap du dimanche.
- Plusieurs membres dans un même foyer : chaque numéro ouvre son propre foyer.
- Page d'admin pour lire et corriger les conversations.
- Toute action extérieure (appel, envoi, paiement).
- Outlook, pièces jointes des mails, modèle WhatsApp approuvé pour écrire hors de la fenêtre de 24 h.

## Organisation du code

| Fichier | Rôle |
| --- | --- |
| `app/main.py` | Serveur FastAPI et webhook WhatsApp |
| `app/service.py` | Chaîne de traitement d'un message |
| `app/brain.py` | Appel au modèle et validation de sa sortie |
| `app/persona.py` | Personnalité et règles de MUMU |
| `app/models.py` | Schéma de la base |
| `app/whatsapp.py` | Envoi, pièces jointes et signature Twilio |
| `app/connectors/` | Connecteurs vers les services (Google) |
| `app/sync.py` | Lecture en arrière-plan des comptes connectés |
| `app/calendar_logic.py` | Points serrés de l'agenda |
| `app/crypto.py` | Chiffrement des jetons, liens signés |
| `tests/` | Tests de la chaîne avec un faux modèle |

## Lancer en local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # puis remplis les valeurs
set -a && source .env && set +a
python -m pytest -q
uvicorn app.main:app --factory --reload
```

Sans `DATABASE_URL`, une base SQLite `mumu.db` est créée dans le dossier.

## Déployer

1. **Supabase** : crée un projet, copie la chaîne de connexion Postgres et mets-la dans `DATABASE_URL` sous la forme `postgresql+psycopg://...`. Les tables sont créées au démarrage.
2. **Railway ou Render** : crée un service à partir de ce dépôt. La commande de démarrage est dans `Procfile`. Ajoute les variables de `.env.example`.
3. **Twilio** : dans le bac à sable WhatsApp, règle « When a message comes in » sur `https://TON-DOMAINE/webhook/whatsapp` en POST.
4. Mets ce même domaine dans `PUBLIC_URL` (sans `/` final). Il sert à vérifier la signature des requêtes Twilio.
5. Envoie le code d'activation du bac à sable depuis ton téléphone, puis écris à MUMU.

## Activer la connexion Google (facultatif)

1. Dans Google Cloud, crée un projet, active les API Gmail et Google Calendar.
2. Configure l'écran de consentement en mode « Test » et ajoute les adresses Gmail de tes testeurs.
3. Crée un identifiant OAuth de type « Application Web » avec l'URI de redirection `https://TON-DOMAINE/connect/google/callback`.
4. Renseigne `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` et `MUMU_SECRET_KEY` (une longue chaîne aléatoire, à ne jamais changer ensuite).
5. Écris « connecte mon Gmail » à MUMU.

En mode test, Google limite à 100 comptes et l'autorisation expire au bout de 7 jours. Les détails et les autres limites sont dans `docs/vie-connectee.md`.

Si la base a été créée avec la première version, supprime ses tables avant de redéployer : le schéma a changé et il n'y a pas encore de migrations.

## Données personnelles

Ce dépôt est public. N'y mets jamais de clé, de fichier `.env`, de base de données ni de conversation réelle. Les exemples de test doivent être inventés.
