# Halb-manueller Integrationstest: Ableton MCP 2.0

Für Branch `v2.0` (PR #3, Remote Script 2.0.0+dunk.1, auf Basis von Upstream 1.5.0), inklusive der Tools aus PR #2 (Returns, Sends, Clip-Automation).

Seit dem Upstream-Sync (28.11.2026) kommen Mixer, Transport-Status, Song-Einstellungen und Spur/Szenen-Struktur aus Upstream (`set_track_mixer`, `get_transport`, `set_song_settings`, `edit_structure`, `stop_all_clips`). Diese Tools prüfen Ziel-IDs: Claude liest vorher z. B. mit `get_mixer_state` oder `get_structure` die `target_id`.
Gedacht für einen Durchlauf mit Claude und Ableton Live 12 nebeneinander: Du gibst Claude den Prompt, Claude ruft das Tool auf, du prüfst in Live das erwartete Ergebnis und hakst ab.

**Dauer:** etwa 60–90 Minuten für alles. Die Blöcke sind unabhängig, du kannst nach jedem Block aufhören.

**Legende:** `[ ]` offen · Bei Abweichung: Prompt, Tool-Antwort und was Live zeigt notieren (Abschnitt „Befunde“ ganz unten).

---

## 0. Vorbereitung

### 0.1 Sicherheit
- [ ] Ableton Live ist **Version 12.x** (für `insert_device` 12.3+). Version notieren: `______`
- [ ] Alle offenen Sets gespeichert. **Für den Test ein neues, leeres Set anlegen** (Datei → Neues Live-Set) und als `MCP-2.0-Test.als` speichern. Niemals in einem echten Projekt testen.

### 0.2 Remote Script 2.0.0+dunk.1 installieren
Die alte Version wird vom Installer automatisch als `__init__.py.bak` gesichert.

```bash
uvx --refresh --from git+https://github.com/dungeonkueper/dunk-ableton-mcp@v2.0 ableton-mcp-install-script --list-targets
uvx --refresh --from git+https://github.com/dungeonkueper/dunk-ableton-mcp@v2.0 ableton-mcp-install-script
```

- [ ] Installer meldet `script_version=2.0.0+dunk.1` und den Zielordner.
- [ ] Löschen ist in Upstreams Sicherheitsrichtlinie standardmäßig aus. Für die Lösch-Tests (3.5, 4.5, 8.7, 9.6) im Testset `~/.config/ableton-mcp/safety.json` mit `{"allow": {"delete": true}, "saved_set_allow": {"delete": true}}` anlegen und die Datei nach dem Test wieder löschen.
- [ ] Live **neu starten** (oder in Einstellungen → Link, Tempo & MIDI die Control Surface „AbletonMCP“ auf „None“ und wieder zurück stellen).

### 0.3 MCP-Server aus dem Branch starten
In der MCP-Konfiguration (Claude Desktop: `claude_desktop_config.json`) den Eintrag für AbletonMCP vorübergehend so ändern:

```json
"AbletonMCP": {
  "command": "uvx",
  "args": ["--refresh", "--from", "git+https://github.com/dungeonkueper/dunk-ableton-mcp@v2.0", "ableton-mcp"]
}
```

- [ ] Claude neu starten; in der Tool-Liste tauchen u. a. `set_track_mixer` (Upstream), `create_arrangement_clip`, `insert_device`, `read_scale` auf, jedes Tool genau einmal.

### 0.4 Handshake
> Prompt: *„Zeig mir die Remote-Script-Info.“* (`get_remote_script_info`)

- [ ] `script_version` = `2.0.0+dunk.1`, `expected_version` = `2.0.0+dunk.1`
- [ ] `capabilities` enthält `set_track_mixer`, `get_clip_automation`, `undo`

### 0.5 Ausgangsset bauen
> Prompt: *„Lege zwei MIDI-Spuren und eine Audio-Spur an. Nenne sie Bass, Lead und Vox. Lege auf Bass in Slot 1 einen 4-Takt-Clip (16 Beats) mit vier Viertelnoten C2 an.“*

- [ ] Drei Spuren mit den Namen, auf Bass ein Clip mit 4 Noten.
- [ ] Merke dir: Bass = Spur 0, Lead = Spur 1, Vox = Spur 2 (Claude rechnet ab 0).

---

## 1. Undo-Gruppierung

| # | Prompt / Aktion | Erwartet in Live | OK |
|---|---|---|---|
| 1.1 | *„Setze Bass auf Volume 0.5, Pan -0.5 und Solo an.“* (Upstream `set_track_mixer`) | Alle drei Änderungen sichtbar | [ ] |
| 1.2 | In Live einmal **Cmd/Strg+Z** | **Alle drei** Änderungen sind auf einmal zurück (ein Undo-Schritt) | [ ] |
| 1.3 | Live: Bearbeiten-Menü ansehen | Kein Eintrag pro Einzelwert, sondern einer für den Aufruf | [ ] |
| 1.4 | *„Mach das rückgängig.“* nach einer weiteren Änderung (`undo`) | Änderung zurück, Antwort zeigt `can_redo=True` | [ ] |
| 1.5 | *„Wiederhole das.“* (`redo`) | Änderung wieder da | [ ] |
| 1.6 | Auch alte Tools: *„Benenne Spur Lead in Synth um“*, dann Cmd+Z | Name wieder „Lead“ | [ ] |

## 2. Mixer (Upstream `set_track_mixer`)

Nur kurz gegenprüfen, dass das Upstream-Tool im Fork läuft.

| # | Prompt | Erwartet in Live | OK |
|---|---|---|---|
| 2.1 | *„Bass auf 0 dB.“* | Fader zeigt 0.0 dB | [ ] |
| 2.2 | *„Lead ganz links und gemutet.“* | Pan 50L, Spur stumm | [ ] |
| 2.3 | Fehlerfall: *„Setze Bass auf Volume 5.“* | Fehlermeldung, **nichts** hat sich geändert | [ ] |

## 3. Tracks und Returns

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 3.1 | *„Dupliziere die Bass-Spur.“* (Upstream `edit_structure`) | Kopie direkt unter Bass, mit Clip | [ ] |
| 3.3 | *„Lege einen Return-Track an.“* (PR #2) | Neuer Return; jede Spur hat einen neuen Send | [ ] |
| 3.4 | *„Send von Bass auf diesen Return auf 0.6.“* (PR #2, `set_send`) | Send-Regler bewegt sich; Antwort zeigt alten und neuen Wert | [ ] |
| 3.5 | *„Lösche den letzten Return-Track.“* (braucht safety.json) | Return und zugehörige Sends weg | [ ] |

## 4. Szenen

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 4.1 | *„Lege am Ende eine Szene ‚Drop‘ an.“* (Upstream `edit_structure`) | Neue Szene „Drop“ | [ ] |
| 4.2 | *„Szene Drop: Tempo 128 aktiv, Taktart 7/8.“* | Szenen-Tempo 128 und 7/8 sichtbar | [ ] |
| 4.3 | *„Dupliziere Szene 1.“* (Upstream `edit_structure`) | Kopie direkt darunter, mit Clips | [ ] |
| 4.4 | *„Starte Szene 1.“* (Transport läuft) | Szene startet, Clip spielt | [ ] |
| 4.5 | *„Lösche die duplizierte Szene.“* (Upstream `edit_structure`, braucht safety.json) | Szene weg | [ ] |

## 5. Clip-Eigenschaften und Loop

Auf dem Bass-Clip (Spur 0, Slot 0).

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 5.1 | *„Loop des Bass-Clips auf Beat 4 bis 12.“* | Loop-Klammer 2.1.1 bis 4.1.1 | [ ] |
| 5.2 | *„Loop auf Beat 16 bis 24“* (hinter dem alten Ende) | Klappt in einem Aufruf ohne Fehler | [ ] |
| 5.3 | *„Launch-Mode Gate, Quantisierung 1 Bar, Legato an.“* | Clip-Ansicht zeigt Gate / 1 Bar / Legato | [ ] |
| 5.4 | *„Mute den Clip.“* | Clip-Aktivator aus (grau) | [ ] |
| 5.5 | Audio-Clip: eine kurze Audiodatei auf Vox legen (`create_audio_clip` mit absolutem Pfad), dann *„Gain 0.3, Pitch -12, Warp-Modus Complex Pro.“* | Werte in der Clip-Ansicht | [ ] |
| 5.6 | Fehlerfall: *„Gain 0.3 auf dem Bass-Clip.“* | Fehlermeldung „only apply to audio clips“ | [ ] |
| 5.7 | Fehlerfall: *„Loop von 8 bis 4.“* | Fehlermeldung, Loop unverändert | [ ] |

## 6. Clip-Edits (`edit_clip`)

Vorher: *„Schreib in den Bass-Clip ein paar leicht verschobene Noten (Starts 0.07, 1.04, 2.1).“*

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 6.1 | *„Quantisiere den Clip auf 1/16 mit 100 %.“* | Noten sitzen auf dem Raster | [ ] |
| 6.2 | *„Quantisiere auf 1/8 mit 50 %.“* | Noten rücken halb Richtung Raster | [ ] |
| 6.3 | *„Verdopple den Loop.“* (`duplicate_loop`) | Clip doppelt so lang, Inhalt wiederholt | [ ] |
| 6.4 | *„Kopiere die Noten von Beat 0–2 nach Beat 8, eine Oktave höher.“* (`duplicate_region`) | Kopie bei Beat 8, +12 | [ ] |
| 6.5 | *„Crop den Clip.“* | Alles außerhalb des Loops entfernt | [ ] |
| 6.6 | *„Dupliziere den Clip in den nächsten Slot.“* (`duplicate_slot`) | Kopie in Slot 2; Antwort nennt `new_clip_index` | [ ] |

## 7. Transport

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 7.1 | *„Wie ist der Transport-Status?“* (Upstream `get_transport`) | JSON mit Tempo und Song-Einstellungen | [ ] |
| 7.2 | *„Arrangement-Loop an, Beat 0 bis 16, Metronom an.“* | Claude nutzt `set_transport` für den Loop und `set_song_settings` fürs Metronom; beides sichtbar | [ ] |
| 7.3 | *„Loop von Beat 200 bis 232.“* (leeres Arrangement) | Antwort enthält **Warning** (Song-Ende), statt still falsch | [ ] |
| 7.4 | *„Taktart 3/4, globale Launch-Quantisierung 1 Bar (=4).“* | Taktart (via `set_song_settings`) und Quantisierungs-Menü (via `set_transport`) | [ ] |
| 7.5 | *„Punch In und Punch Out an.“* | Beide Knöpfe aktiv | [ ] |
| 7.6 | Setze zwei Locator (Beat 8, Beat 16), Playhead auf 0, dann *„Spring zum nächsten Locator.“* | Playhead auf 8 | [ ] |
| 7.7 | *„Spring 4 Beats zurück.“* (`jump_by -4`) | Playhead 4 Beats früher | [ ] |
| 7.8 | *„Stoppe alle Clips.“* (Upstream `stop_all_clips`) | Session-Clips stoppen | [ ] |
| 7.9 | Spiel etwas auf einem MIDI-Keyboard auf der gearmten Spur (ohne Aufnahme), dann *„Capture MIDI.“* | Neuer Clip mit dem Gespielten | [ ] |
| 7.10 | Fehlerfall: *„Capture MIDI“* ohne vorher gespielte Noten | Klare Fehlermeldung | [ ] |

## 8. Arrangement-Clips direkt anlegen

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 8.1 | *„Lege auf Lead bei Takt 9 (Beat 32) einen 8-Beat-MIDI-Clip ‚Hook‘ mit einer kleinen Melodie an.“* | Clip im Arrangement bei 9.1.1, mit Noten, ohne Session-Clip | [ ] |
| 8.2 | *„Zeig mir die Arrangement-Clips von Lead.“* (`get_arrangement_clips`) | Hook mit Start 32 und richtigem Index | [ ] |
| 8.3 | Audio: *„Lege die Datei /pfad/zu/sample.wav bei Beat 0 auf Vox.“* | Audio-Clip im Arrangement | [ ] |
| 8.4 | Fehlerfall: MIDI-Clip auf Vox (Audio-Spur) | Fehler „need a MIDI track“ | [ ] |
| 8.5 | *„Setz einen Locator ‚Hook‘ bei Beat 32.“* (jetzt gibt es dort Material) | Locator erscheint | [ ] |
| 8.6 | Fehlerfall: *„Locator bei Beat 1000.“* | Fehlermeldung (Upstreams Locator-Code; die frühere Song-Ende-Erklärung aus 2.0 ist entfallen) | [ ] |
| 8.7 | *„Lösche den Hook-Clip.“* (`delete_arrangement_clip`, braucht safety.json) | Clip weg; Cmd+Z holt ihn zurück | [ ] |

## 9. Clip-Automation schreiben und zurücklesen

PR #2 schreibt, 2.0 liest. Vorher auf Bass ein Gerät laden (siehe Block 10, z. B. Auto Filter).

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 9.1 | *„Schreib im Bass-Clip einen Filter-Sweep der Auto-Filter-Frequenz von min nach max über den ganzen Clip.“* (`set_clip_automation`) | Hüllkurve im Clip sichtbar (Rampe aus Stufen) | [ ] |
| 9.2 | *„Welche Automation ist im Bass-Clip?“* (`get_clip_automation`) | Envelope „Auto Filter: Frequency“ mit `device_index`/`parameter_index` und Punkten, die die Rampe abbilden | [ ] |
| 9.3 | *„Lies sie zusätzlich jede Viertel ab.“* (`sample_step` 1.0) | `samples` mit steigenden Werten | [ ] |
| 9.4 | Volume-Automation dazuschreiben, dann wieder alles lesen | Zwei Envelopes in der Antwort | [ ] |
| 9.5 | In Live von Hand einen Punkt verschieben, dann neu lesen | Geänderter Wert taucht auf | [ ] |
| 9.6 | *„Lösche nur die Volume-Automation.“* (`clear_clip_automation`, braucht safety.json) | Nur Filter-Envelope übrig | [ ] |
| 9.7 | *„Kopiere den Bass-Clip bei Beat 64 ins Arrangement.“* (`duplicate_to_arrangement`) | Entweder Automation ist mit dabei, **oder** die Antwort enthält die Warnung „automation lost“. Ergebnis notieren: `______` | [ ] |
| 9.8 | *„Lies die Automation des Arrangement-Clips.“* (`clip_location=arrangement`) | Entspricht dem Ergebnis aus 9.7 | [ ] |
| 9.9 | Fehlerfall: Automation für einen neuen Parameter auf einem Arrangement-Clip schreiben | Fehlermeldung erklärt, dass Live dort keine neuen Hüllkurven anlegt (oder es klappt: dann bitte notieren!) | [ ] |

## 10. Devices ohne Browser (`insert_device`, Live 12.3+)

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 10.1 | *„Füge auf Bass ‚Operator‘ ein.“* | Operator lädt sofort, kein Browser-Suchen | [ ] |
| 10.2 | *„Füge dahinter ‚Auto Filter‘ und ‚EQ Eight‘ ein.“* | Reihenfolge Operator → Auto Filter → EQ Eight | [ ] |
| 10.3 | *„Setze ‚Utility‘ an Position 0.“* (`device_index` 0) | Utility ganz vorne | [ ] |
| 10.4 | *„Lege ‚Reverb‘ auf den Return-Track.“* (`track_type="return"`) | Reverb auf dem Return | [ ] |
| 10.5 | Weitere Namen probieren: Drift, Wavetable, Simpler, Drum Rack, Compressor, Saturator, Delay. Was ging nicht? `______` | Möglichst alle laden | [ ] |
| 10.6 | Fehlerfall: *„Füge Serum 2 ein.“* | Fehler mit Hinweis auf `load_instrument_or_effect` | [ ] |
| 10.7 | Cmd+Z nach 10.2 | Beide Devices auf einmal weg | [ ] |

## 11. Skala (`read_scale` / `set_scale`)

| # | Prompt | Erwartet | OK |
|---|---|---|---|
| 11.1 | *„Welche Tonart hat das Set? Liste auch alle Skalen.“* | Grundton, Skala, Notennamen, `available_scales` (Liste plausibel?) | [ ] |
| 11.2 | *„Stell das Set auf D Dorian und schalte Scale Mode an.“* | Live zeigt D Dorian, Scale-Knopf an | [ ] |
| 11.3 | *„Jetzt F# Moll.“* / *„Bb Major.“* | Grundton und Skala korrekt (Kreuz und b) | [ ] |
| 11.4 | *„Schreib eine Basslinie in den Bass-Clip, die in der Skala bleibt.“* | Im MIDI-Editor nur hervorgehobene Tasten | [ ] |
| 11.5 | Fehlerfall: *„Skala Klingonisch.“* | Fehler mit Liste der bekannten Skalen, Skala unverändert | [ ] |

## 12. Abschluss

- [ ] Set **nicht** speichern (oder als Testset verwerfen).
- [ ] `~/.config/ableton-mcp/safety.json` wieder löschen, falls angelegt.
- [ ] Alten Zustand wiederherstellen, falls gewünscht: MCP-Konfiguration zurück, und in `User Library/Remote Scripts/AbletonMCP/` die `__init__.py.bak` zurück nach `__init__.py` kopieren, Live neu starten.

---

## Befunde

| Test-# | Was passiert ist (Tool-Antwort, Live-Zustand) | Schwere (blockiert / stört / kosmetisch) |
|---|---|---|
| | | |
| | | |
| | | |

Die Befunde kannst du einfach als Kommentar in PR #3 posten; offen geprüft werden sollten vor allem 9.7–9.9, 10.5 und 11.1, weil sie nur gegen nachgebaute Live-Objekte getestet sind.
