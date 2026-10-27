# Hort Manager

Kostenloses Tool für Horte / Tagesstätten: Anwesenheit, Termine, Tagesprotokoll, **Wochenplan & Mittagessen**.

Free tool for after-school care (Hort): attendance, appointments, daily log, **weekly schedule & lunches**.

---

## Was drin ist

- Kinder-Roster mit Status (anwesend, abwesend, unterwegs …)
- Termine mit Erinnerung (Physio, Musik, etc.)
- Tagesprotokoll / Notizen
- **Wochenplan (Stundenplan)** + **Mittagessen pro Wochentag**
- Mehrere Konten (Manager + Mitarbeitende)
- Themes, Logo, Hort-Name, Deutsch / English
- Einfacher Modus (grosse Schrift, weniger Ablenkung)

---

## Lokal testen (auf dem PC)

```bash
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Browser öffnen: http://127.0.0.1:8000  
Beim ersten Start: Hort einrichten (Name, Theme, Manager-Konto). Optional «Beispieldaten laden» anklicken.

Daten liegen in der Datei `hort.db` (SQLite) neben `main.py`.

---

## Online hosten (damit Horte zugreifen können – ohne deinen PC)

Empfohlen: **Render.com** (kostenloser Tarif).

### Schritt-für-Schritt auf Render

1. Konto auf [https://render.com](https://render.com) anlegen (mit GitHub).
2. Neues **GitHub-Repository** anlegen und diese Dateien hochladen:
   - `main.py`
   - `index.html`
   - `requirements.txt`
   - (optional) `README.md`
3. Auf Render: **New → Web Service** → Repository verbinden.
4. Einstellungen:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Deploy starten. Nach ein paar Minuten gibt es eine URL wie  
   `https://hort-manager-xxxx.onrender.com`
6. Diese URL an den Hort weitergeben. Beim ersten Besuch richten sie den Hort selbst ein.

**Hinweis Free-Tier:** Nach ~15 Min. Inaktivität schläft der Service ein; der nächste Besuch dauert 30–60 Sekunden länger. Für den Alltag eines Horts ist das meist ok. Persistent disk ist im Free-Tier eingeschränkt – bei Bedarf später auf einen günstigen Paid-Plan wechseln oder die `hort.db` regelmässig sichern.

### Alternativen

| Anbieter        | Aufwand | Kosten   | Bemerkung                          |
|-----------------|---------|----------|------------------------------------|
| Render.com      | einfach | gratis   | Empfehlung oben                    |
| Railway.app     | einfach | gratis*  | Credit-Limit, dann bezahlt         |
| Fly.io          | mittel  | gratis*  | etwas mehr Technik                 |
| PythonAnywhere  | einfach | gratis   | gut für Python, etwas eingeschränkt|

\* Free-Tarife ändern sich – aktuelle Limits auf der jeweiligen Seite prüfen.

---

## Für den Hort (kurze Anleitung)

1. Link öffnen (von dir erhalten).
2. Beim ersten Mal: **Hort einrichten** → Name, Theme, Manager-Konto.
3. Unter **Einstellungen** (Manager): weitere Mitarbeiter-Konten anlegen, Status und Terminarten anpassen.
4. **Stundenplan**: Wochenprogramm und Mittagsmenüs eintragen.
5. Täglich: Status der Kinder tippen, Termine sehen, Notizen eintragen.

Deutsch und English umschaltbar. «Einfacher Modus» für grosse Schrift.

---

## Dateien

| Datei              | Zweck                          |
|--------------------|--------------------------------|
| `main.py`          | Backend (FastAPI + SQLite)     |
| `index.html`       | Oberfläche (Browser)           |
| `requirements.txt` | Python-Pakete                  |
| `README.md`        | Diese Anleitung                |

Keine anderen Abhängigkeiten. Keine Cloud-Pflicht – läuft auch nur lokal.

---

Viel Erfolg – und danke, dass du Horten ein freies Werkzeug zur Verfügung stellst.
