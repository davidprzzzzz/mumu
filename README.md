# MUMU

> MUMU pense à tout, sans que les utilisateurs aient l'impression de devoir penser à MUMU.
>
> Chaque échange doit laisser la personne avec moins de choses en tête qu'avant.

MUMU est un assistant de vie quotidienne, accessible par WhatsApp, pour les personnes seules, les couples, les familles et les colocations. Il lit ce qu'on lui envoie et, avec l'accord de la personne, sa boîte Gmail et son agenda Google. Il suit chaque démarche jusqu'à ce qu'elle soit réglée, coordonne les membres du foyer et ne parle que quand c'est utile.

## État réel du projet

**Tout ce qui suit a été testé uniquement avec des doublures** : WhatsApp, Google, Claude et la transcription sont simulés. Aucune intégration n'a encore tourné contre un vrai service ni un vrai compte. Deux tests « live » existent pour le faire (voir [Tests](#tests)) ; ils n'ont jamais été lancés.

Ce que prouvent les tests : ce que MUMU fait de ce qu'il a compris (processus, confidentialité, notifications). Ce qu'ils ne prouvent pas : que le vrai modèle comprend correctement un vrai mail ou une vraie photo.

## L'expérience, en quatre idées

| | Ce que vit la personne | Ce qui le porte dans le code |
| --- | --- | --- |
| **MUMU SAIT** | Elle transfère, photographie, dicte ou connecte sa boîte mail. Elle ne remplit rien. | `brain.py`, `sync.py`, `documents.py`, `money.py` |
| **MUMU RÉFLÉCHIT** | « Qu'est-ce que je dois faire cette semaine ? » reçoit une réponse classée, tirée de données réelles. | `agenda.py`, `calendar_logic.py`, `money.py` |
| **MUMU S'EN OCCUPE** | Une facture, un courrier de la CAF, un retour de colis deviennent un processus suivi jusqu'au bout, avec des textes prêts à envoyer. | `signals.py`, `workflows/` |
| **MUMU RELIE** | Une tâche se propose à un autre membre, qui accepte d'un mot. Rien de privé n'est partagé sans accord. | `household.py`, `proactive.py` |

Le détail de chaque fonctionnalité (problème, expérience, code, état, identifiants, manques) est dans [`docs/fonctionnalites.md`](docs/fonctionnalites.md).

## Architecture

```
WhatsApp (Twilio) ──► main.py ──► service.py ──► brain.py (Claude : comprend)
Gmail / Agenda ─────► sync.py ─┘        │
                                        ▼
                               signals.py (décide, déterministe)
                                        │
            ┌───────────────┬───────────┼──────────────┬──────────────┐
            ▼               ▼           ▼              ▼              ▼
     workflows/        documents.py  money.py     household.py    Item / Fact
  (processus suivis)   (pièces)     (montants)   (coordination)  (mémoire simple)
            │
            ▼
     proactive.py ──► un message, seulement s'il est utile ──► WhatsApp
```

Le modèle propose, le code décide. Chaque identifiant que le modèle renvoie est vérifié : il doit appartenir au foyer et être visible par la personne qui parle.

### Les règles de confiance, appliquées dans le code

- **Un processus n'est terminé** que quand toutes ses étapes sont faites, sur parole de la personne ou sur preuve (mail de remboursement), ou quand la personne le ferme. Jamais parce que MUMU a répondu.
- **Rien n'est envoyé à un tiers par MUMU.** Un texte approuvé est donné à la personne, prêt à envoyer ; l'étape reste ouverte jusqu'à ce qu'elle confirme.
- **Pas de paiement, pas de mail envoyé, pas de surveillance cachée.** Gmail et l'agenda sont en lecture seule.
- **Le privé reste privé.** Tout ce qui vient d'une boîte mail est visible de son seul propriétaire. Partager un processus demande un geste explicite du propriétaire. Les conversations des autres membres ne sont jamais montrées.
- **Le silence par défaut.** Un message spontané doit passer un seuil d'importance, d'urgence et de confiance. Une même intervention n'est jamais envoyée deux fois. Rien entre 21 h et 8 h, deux messages spontanés par jour au plus, et seulement dans la fenêtre de 24 h permise par WhatsApp.
- **Traçabilité.** Chaque action importante est écrite dans `audit_log`. Chaque processus garde ses sources.
- **Effacement.** « Déconnecte » révoque l'accès Google et efface tout ce qui en a été tiré. Le texte des conversations est effacé après `MESSAGE_RETENTION_DAYS` jours.
- **Idempotence.** Un mail n'est lu qu'une fois, un document n'est gardé qu'une fois, un message WhatsApp livré deux fois n'est traité qu'une fois.
- **Jetons.** Les jetons Google sont chiffrés. Les liens signés expirent et ne servent qu'à l'usage pour lequel ils ont été créés.

## Organisation du code

| Fichier | Rôle |
| --- | --- |
| `app/main.py` | Serveur FastAPI : webhook WhatsApp, connexion Google, liens de documents, boucle d'arrière-plan |
| `app/service.py` | Traitement d'un message : contexte, appel au modèle, application contrôlée |
| `app/brain.py` | Schémas des outils, appel à Claude, validation stricte des sorties |
| `app/persona.py` | Personnalité et règles de MUMU (le fichier à retoucher le plus souvent) |
| `app/signals.py` | Transforme ce qui a été compris en processus, achats et montants |
| `app/workflows/` | Moteur de processus (`engine.py`) et types de processus (`templates.py`) |
| `app/household.py` | Invitations, partage, propositions de responsabilité |
| `app/proactive.py` | Moteur proactif : qui prévenir, quand, et seulement une fois |
| `app/agenda.py` | Vue priorisée de la semaine |
| `app/documents.py` | Documents : doublons, validité, liens de récupération |
| `app/money.py` | Montants réels, hausses de prix, prélèvements attendus |
| `app/calendar_logic.py` | Points serrés de l'agenda et créneau libre |
| `app/sync.py` | Lecture en arrière-plan des comptes connectés |
| `app/connectors/` | Interface commune (`base.py`) et connecteur Google |
| `app/voice.py` | Transcription des vocaux |
| `app/messaging.py`, `app/audit.py`, `app/retention.py`, `app/crypto.py` | Règles d'envoi, journal, effacement, chiffrement |
| `migrations/` | Migrations Alembic du schéma |
| `docs/` | Fonctionnalités, intégrations, feuille de route |

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

1. **Supabase** : crée un projet et mets la chaîne de connexion dans `DATABASE_URL`, sous la forme `postgresql+psycopg://...`.
2. **Railway ou Render** : crée un service à partir de ce dépôt. La commande de démarrage est dans `Procfile` : elle applique les migrations (`alembic upgrade head`) puis lance le serveur. Ajoute les variables de `.env.example`.
3. **Twilio** : dans le bac à sable WhatsApp, règle « When a message comes in » sur `https://TON-DOMAINE/webhook/whatsapp` en POST.
4. Mets ce même domaine dans `PUBLIC_URL`, sans `/` final.
5. Envoie le code d'activation du bac à sable depuis ton téléphone, puis écris à MUMU.

**Si une base existe déjà avec une version précédente**, elle n'a pas été créée par Alembic. Supprime ses tables une fois avant de redéployer. Ensuite, chaque changement de schéma passe par une migration :

```bash
alembic revision --autogenerate -m "ce qui change"
alembic upgrade head
```

Les migrations n'ont été vérifiées que sur SQLite. Le SQL généré pour Postgres a été relu, mais jamais exécuté sur une vraie base Postgres.

## Activer la connexion Google (facultatif)

1. Dans Google Cloud, crée un projet et active les API Gmail et Google Calendar.
2. Configure l'écran de consentement en mode « Test » et ajoute les adresses Gmail de tes testeurs.
3. Crée un identifiant OAuth « Application Web » avec l'URI de redirection `https://TON-DOMAINE/connect/google/callback`.
4. Renseigne `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` et `MUMU_SECRET_KEY` (une longue chaîne aléatoire, à ne jamais changer ensuite).
5. Écris « connecte mon Gmail » à MUMU.

En mode test, Google limite à 100 comptes et l'autorisation expire au bout de 7 jours. Les contraintes de chaque intégration sont dans [`docs/integrations.md`](docs/integrations.md).

## Activer les vocaux (facultatif)

Renseigne `OPENAI_API_KEY`. Les vocaux sont transcrits puis traités comme un message écrit. Sans clé, MUMU répond qu'il ne sait pas encore écouter.

## Tests

| Type | Où | Ce qu'ils prouvent | Lancement |
| --- | --- | --- | --- |
| Unitaires | `tests/test_engine.py`, `tests/test_hardening.py`, une partie de `tests/test_connected.py` | Moteur de processus, montants, dates légales, jetons, migrations, idempotence | `python -m pytest -q` |
| Intégration avec doublures | `tests/test_pipeline.py`, `tests/test_connected.py`, `tests/test_acceptance.py` | La chaîne complète (HTTP, base, routage, confidentialité, moteur proactif) avec WhatsApp, Google et le modèle simulés | `python -m pytest -q` |
| Contre les vrais services, avec identifiants | `tests/live/` | Que ta vraie connexion Google lit tes mails et ton agenda, et que le vrai modèle comprend une facture | `MUMU_LIVE=1 python -m pytest -m live tests/live` |

Les dix scénarios d'acceptation sont dans `tests/test_acceptance.py` : mail de l'école partagé entre parents, attestation d'assurance demandée, hausse d'abonnement, retour de colis, courrier de la CAF, responsabilité acceptée, conflit d'agenda, mail privé dans une coloc, une seule relance pour une échéance, vue de la semaine.

**Aucun test live n'a été lancé à ce jour.** Les deux tests de `tests/live/` sont ignorés sans `MUMU_LIVE=1`.

## Feuille de route

Voir [`docs/roadmap.md`](docs/roadmap.md).

## Données personnelles

Ce dépôt est public. N'y mets jamais de clé, de fichier `.env`, de base de données ni de conversation réelle. Les exemples de test sont inventés. Passe le dépôt en privé avant d'y mettre quoi que ce soit lié à de vrais utilisateurs.
