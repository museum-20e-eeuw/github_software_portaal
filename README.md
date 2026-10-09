# GitHub Software Portaal

Een lokale webapp voor medewerkers van Museum van de 20ste Eeuw om softwareprojecten van de GitHub-organisatie `museum-20e-eeuw` te bekijken en te beheren.

## Functies

- Dashboard met repositories, recente activiteit en lopend werk.
- Meldingenwidget met recente acties in de huidige browsersessie.
- Repositories, pull requests, issues, branches en GitHub Actions-workflows bekijken.
- Issues aanmaken en sluiten; pull requests mergen; workflows opnieuw starten of annuleren.
- Projecten lokaal klonen, synchroniseren en bestanden openen in Visual Studio Code, Arduino IDE of de standaardapp.
- Lokale wijzigingen committen en pushen, met optioneel een GitHub-release.
- Nieuwe GitHub-projecten aanmaken met een Python- of Arduino-startproject, of als kopie van een bestaand project.
- Installatie van Git, Visual Studio Code en Arduino IDE controleren.

> **Let op:** de actie om een lokale repository bij te werken voert een harde reset uit naar de standaardbranch op GitHub. Niet-gecommitte lokale wijzigingen kunnen daarbij verloren gaan.

## Techniek

- Python 3.13
- Flask (zie [requirements.txt](./requirements.txt))
- GitHub REST API
- HTML, CSS en JavaScript

## Installeren en starten

Voer deze stappen uit vanuit de hoofdmap van deze repository.

1. Maak een virtuele omgeving en installeer de dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python -m pip install -r requirements.txt
   ```

2. Maak in de hoofdmap een lokaal bestand `config.json`. Dit bestand wordt niet door Git bijgehouden:

   ```json
   {
     "app_name": "GitHub Software Portaal",
     "organization": "museum-20e-eeuw",
     "default_username": "Tuerger",
     "page_size": 20,
     "workflow_repo_limit": 6,
     "repo_preview_limit": 5,
     "log_level": "DEBUG",
     "workspace_root": "C:\\museum2000\\Workspace",
     "personal_access_token": ""
   }
   ```

   Pas `workspace_root` zo nodig aan naar de map waarin lokale projectklonen en logbestanden moeten worden bewaard.

3. Start de app:

   Dubbelklik op `start_portaal.bat`, of start de app vanuit PowerShell:

   ```powershell
   .\.venv\Scripts\python app.py
   ```

4. De app opent het dashboard in je browser. Standaard luistert de lokale server op `http://127.0.0.1:5080`. Stel eventueel de omgevingsvariabele `PORT` in om een andere poort te gebruiken.

## GitHub-aanmelding en token

Meld je aan met het GitHub-account dat toegang heeft tot de organisatie. De app verwacht standaard de gebruiker `Tuerger`; wijzig `default_username` in `config.json` als dat voor jouw installatie anders is.

Gebruik een Personal Access Token met de rechten die passen bij de gewenste acties, zoals repositories lezen en beheren, issues en pull requests beheren, workflows bekijken of bedienen en repositories in de organisatie aanmaken. De benodigde rechten hangen af van het type token en het beleid van de organisatie.

> **Beveiliging:** wanneer je een token invult bij aanmelden of instellingen, slaat de app het lokaal op in `config.json` voor automatisch aanmelden. Dat bestand wordt door `.gitignore` uitgesloten, maar behandel het als een geheim en deel of commit het niet.

## Instellingen en logbestanden

De belangrijkste instellingen staan in `config.json`:

- `organization`: GitHub-organisatie.
- `default_username`: verwachte GitHub-gebruikersnaam voor aanmelden.
- `page_size`: aantal items per pagina.
- `workflow_repo_limit`: maximaal aantal repositories voor het workflow-overzicht.
- `repo_preview_limit`: aantal items in repositoryvoorbeelden.
- `log_level`: minimaal logniveau (`DEBUG`, `INFO`, `WARNING`, `ERROR` of `CRITICAL`).
- `workspace_root`: map voor lokale projectklonen en logging.
- `personal_access_token`: optioneel token voor automatisch aanmelden.

De werkmap en het logniveau kunnen ook via **Instellingen** in de app worden gewijzigd. De app maakt onder `workspace_root` de submap `logging` aan en schrijft daar `app.log` en `app_new.log`. Het gekozen logniveau geldt voor beide bestanden. De logbestanden roteren bij 5 MB; maximaal vijf oude bestanden per log blijven bewaard.

De meldingenwidget op het dashboard toont maximaal 50 recente gebruikersacties, zoals het openen van een project, het opslaan van instellingen en het pushen van wijzigingen. Deze actielijst staat alleen tijdelijk in het geheugen en wordt gewist wanneer de app opnieuw wordt gestart.

## Belangrijke bestanden

- [app.py](./app.py) — Flask-hoofdapp, authenticatie, GitHub API, projecten en overige routes.
- [app_new.py](./app_new.py) — routes voor de projectaanmaakwizard.
- [templates/](./templates/) — HTML-pagina's.
- [static/](./static/) — CSS, JavaScript en app-pictogram.
- `config.json` — lokale instellingen en eventueel token; wordt niet door Git bijgehouden.
- [requirements.txt](./requirements.txt) — Python-dependencies.
