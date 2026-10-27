# Kindle – Datenschutz auf einen Blick (Entwurf)

*Kein Rechtsrat. Vor dem Einsatz mit echten Daten prüfen lassen und Angaben in [Klammern] ergänzen.*

- **Zweck:** Anwesenheit, Mahlzeiten, Termine und Übergaben von Kindern im Hort organisieren.
- **Gespeicherte Daten:** Name, Gruppe, frei wählbare Infos (z. B. Allergien, Notfallkontakt), Notizen, Anwesenheits- und Übergabeprotokoll, Mitarbeitenden-Konten.
- **Wo:** Eigene Datenbank des Horts bei Neon [Region eintragen] und eigener Render-Dienst des Horts. Der Hort hält die Zugangsdaten.
- **Zugriff:** Nur Konten des Horts. Leitung sieht zusätzlich Backup und Änderungsprotokoll. Der Entwickler hat keinen Zugriff auf die Datenbank des Horts. Er liefert nur den Programmcode (Updates).
- **Schutz:** Verschlüsselte Verbindung, Passwörter gehashed, Sperre nach 5 Fehlversuchen, Änderungsprotokoll (90 Tage).
- **Löschung:** Kind löschen entfernt Stammdaten, Protokolle und Übergaben. Mahlzeiten-Einträge werden nach 30 Tagen automatisch gelöscht.
- **Backup:** Nur durch die Leitung, enthält alle Kinderdaten und ist sicher aufzubewahren.
- **Kontakt:** wird in "einstellungen" angezeigt
