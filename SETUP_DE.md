# Kindle einrichten (ca. 20 Minuten)

Jeder Hort bekommt seine **eigene** Installation und seine **eigene** Datenbank. Die Daten liegen bei Ihnen, nicht beim Entwickler.

## 1. Datenbank (Neon)
1. Konto auf neon.tech erstellen, neues Projekt anlegen (Region möglichst Frankfurt/EU).
2. Die **Connection string** (beginnt mit `postgresql://`) kopieren und sicher aufbewahren, z. B. im Passwortmanager.

## 2. Website (Render)
1. Konto auf render.com erstellen und mit GitHub verbinden.
2. **New → Blueprint** wählen und das Kindle-Repository angeben.
3. Bei `DATABASE_URL` die Neon-Adresse einfügen. `FEEDBACK_EMAIL` kann leer bleiben oder die Adresse für Rückmeldungen enthalten.
4. Deploy starten. Nach wenigen Minuten ist die Seite unter der angezeigten Adresse erreichbar.

## 3. Erster Start
1. Die Adresse öffnen. **Sofort** den Hort einrichten und das Leitungskonto erstellen (Passwort mind. 8 Zeichen). Wer die Seite zuerst öffnet, wird sonst Leitung.
2. Unter «Einstellungen» weitere Mitarbeitende anlegen.
3. Für den Test nur **erfundene** Kinder verwenden («Beispieldaten laden»).

## 4. Regelmässig
- Unter «Kinder» das **Backup herunterladen** (enthält Kinderdaten, sicher aufbewahren).
- Updates: Render aktualisiert automatisch, sobald eine neue Version veröffentlicht wird.

## Rückmeldungen
Probleme bitte per E-Mail melden: Was wurde gedrückt, welche Uhrzeit, was ist passiert (Screenshot hilft).
