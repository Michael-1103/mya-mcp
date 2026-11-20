# Contribuer

Les contributions sont les bienvenues (corrections, nouveaux filtres, documentation).

## Mise en place

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q tests
```

Les tests ne nécessitent pas de réseau : le scrape est simulé.

## Règles

- Ouvrir une issue avant un changement important.
- Une pull request = un sujet, avec des tests pour le comportement ajouté ou corrigé.
- Commentaires et documentation en français, descriptions des outils MCP en anglais (elles sont lues par le modèle).
- Ne jamais committer de secret (`.env`, token) ni de base `*.db`.
- Les versions de `requirements.txt` sont épinglées : tester avant de les mettre à jour.
- Respecter le site source : pas d'augmentation de la concurrence ni de la fréquence de scrape sans raison.
