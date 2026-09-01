# Museum GitHub Balie

Eenvoudige moderne webapp voor medewerkers van Museum van de 20e Eeuw om de belangrijkste GitHub-acties uit te voeren voor de organisatie `museum-20e-eeuw`.

## Wat zit erin?

- hoofdmenu met Dashboard, Repositories, Pull Requests, Issues, Workflows en Hulp
- submenu's per repository
- museum-geinspireerde look & feel met donker paars, warm oranje en zachte zandtinten
- volledig API-based richting GitHub
- login gericht op het account `Tuerger`
- acties voor:
  - repositories bekijken
  - open pull requests bekijken en mergen
  - issues aanmaken en sluiten
  - workflow-runs opnieuw starten of annuleren

## Techniek

- Python 3.13
- Flask
- GitHub REST API
- HTML/CSS/JavaScript

## Locatie

De app staat lokaal in [C:\museum2000\Github](C:/museum2000/Github).

## Starten

1. Open een terminal in `C:\museum2000\Github`
2. Maak een virtual environment:

   ```powershell
   python -m venv .venv
   ```

3. Installeer dependencies:

   ```powershell
   .\.venv\Scripts\python -m pip install -r requirements.txt
   ```

4. Start de app:

   ```powershell
   .\.venv\Scripts\python app.py
   ```

5. Open daarna:

   `http://127.0.0.1:5080`

## GitHub token

Gebruik een Personal Access Token dat hoort bij gebruiker `Tuerger`.

Aanbevolen rechten:

- `repo`
- `read:org`
- `workflow`

De token wordt alleen tijdelijk in het geheugen van de lokale app bewaard en niet in `config.json`.

## Configuratie

Basisinstellingen staan in [config.json](C:/museum2000/Github/config.json):

- GitHub organisatie
- standaard loginnaam
- aantal items per pagina
- hoeveel repos worden meegenomen voor workflow-overzichten

## Belangrijke bestanden

- [app.py](C:/museum2000/Github/app.py) - Flask backend en GitHub API-koppeling
- [templates/](C:/museum2000/Github/templates) - pagina's met hoofdmenu en submenu's
- [static/app.css](C:/museum2000/Github/static/app.css) - museumstijl
- [static/app.js](C:/museum2000/Github/static/app.js) - simpele frontendacties via fetch
- [config.json](C:/museum2000/Github/config.json) - lokale instellingen

## Opmerking

Deze eerste versie is bewust simpel gehouden, zodat we later makkelijk extra GitHub-functies kunnen toevoegen.
