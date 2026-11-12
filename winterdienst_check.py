#!/usr/bin/env python3
"""
Winterdienst-Check für Niclas.
Läuft 2x täglich über GitHub Actions: ca. 19:55 Uhr (Abend-Check für morgen)
und ca. 4:55 Uhr (Morgen-Check für heute, hier fällt die Entscheidung), jeweils deutsche Winterzeit (CET, UTC+1).

Prüft die 3 AGB-Kriterien für zwei Standorte (Rellingen, Ellerbek), je 10 km Umkreis:
  a) Neuschnee / Schneedecke >= 1 cm
  b) Glätte (Niederschlag/Nässe seit 20 Uhr Vortag, Temp irgendwann danach <= +1°C –
     nicht zwingend zeitgleich, deckt "überfrierende Nässe" ab)
  c) Offizielle DWD-Warnung (Schnee/Glätte/Glatteis) für den Standort

Zusätzlich eine Risikostufe 1-6 (siehe evaluate_location()), inkl. einer Vorwarnstufe
für Grenzfälle (≤3°C Luft statt ≤1°C), weil die Straßenoberfläche in klaren, windstillen
Nächten oft kälter ist als die gemessene Lufttemperatur.

Datenquelle: Bright Sky API (offene Schnittstelle auf DWD-Daten, kein Key nötig)
Versand: Gmail SMTP mit App-Passwort (Umgebungsvariablen GMAIL_USER / GMAIL_APP_PASSWORD)
"""

import json
import os
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

# ---------- Einstellungen ----------
LOCATIONS = [
    {"name": "Rellingen (Jakob-Ahrens-Str. 5)", "lat": 53.6500, "lon": 9.8170, "max_dist": 10000},
    {"name": "Ellerbek (Kellerstraße 5)", "lat": 53.6572, "lon": 9.8697, "max_dist": 10000},
]

# Deutschland ist während der Winterdienst-Saison (Okt-Mrz) auf CET = UTC+1.
CET = timezone(timedelta(hours=1))

# Ende des Prüffensters am Zieltag (Uhrzeit, bis wann "morgens" zählt).
# Dient auch zur Unterscheidung Abend-/Morgen-Check: vor dieser Uhrzeit = Morgen-Check
# für heute, ab dieser Uhrzeit = Abend-Check für morgen.
WINDOW_END_HOUR = 7

# Uhrzeit der Morgen-Mail, nur für Texte in der Mail (der eigentliche Start steht im Workflow/cron).
MORGEN_MAIL_UHRZEIT = "4:55"

# Sicherheitsmarge für die Vorwarnstufe (Grenzfall), zusätzlich zum harten
# AGB-Kriterium b) bei 1°C. Luft kann 2-4°C wärmer sein als die Fläche selbst.
GRENZFALL_TEMP = 3

# Reif-Regel (Glätte ohne Niederschlag): klare, windarme, feuchte Nacht bei wenig Plusgraden.
# Dann kühlt die Fläche unter die Lufttemperatur ab, Reif/überfrierende Feuchtigkeit ist möglich.
REIF_TEMP = 3           # °C Lufttemperatur oder darunter
REIF_MIN_FEUCHTE = 80   # % relative Luftfeuchte oder mehr
REIF_MAX_WIND = 12      # km/h Wind oder weniger
REIF_MAX_WOLKEN = 50    # % Bewölkung oder weniger

# Für Risikostufe 5 und 6: ab wie viel mm Niederschlag-als-Schnee (≈ cm Neuschnee)
STUFE5_SCHNEE_MM = 5
STUFE6_SCHNEE_MM = 10

# Umkreis für die aktuelle Messung (nächste DWD-Station, in Metern)
CURRENT_MAX_DIST = 30000

# Link zur amtlichen DWD-Warnlage
DWD_WARN_URL = "https://dwd.de/warnungen"

# Link, über den du den Check jederzeit manuell mit frischen Daten neu anstoßen kannst
# (GitHub-Login im Browser nötig). Anpassen, falls Benutzername/Repo-Name abweichen.
RERUN_URL = "https://github.com/22072204/winterdienst-check/actions/workflows/winterdienst.yml"

# Bright Sky Schweregrade -> Rang (DWD: Wetterwarnung / markant / Unwetter / extremes Unwetter)
SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
# Standardmäßig schickt er sich die Mail selbst; optional eine andere Adresse
# (z.B. für Mitarbeiter) über die Secret-Variable NOTIFY_TO überschreiben.
NOTIFY_TO = os.environ.get("NOTIFY_TO", GMAIL_USER)


# ---------- Bright Sky API ----------
def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "winterdienst-check/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_weather(lat, lon, max_dist, from_iso, to_iso):
    url = (
        f"https://api.brightsky.dev/weather?lat={lat}&lon={lon}&max_dist={max_dist}"
        f"&date={from_iso}&last_date={to_iso}"
    )
    data = http_get_json(url)
    return data.get("weather", [])


def fetch_alerts(lat, lon):
    url = f"https://api.brightsky.dev/alerts?lat={lat}&lon={lon}"
    data = http_get_json(url)
    return data.get("alerts", [])


def fetch_current_temperature(lat, lon):
    """Aktuelle gemessene Temperatur (DWD-Station in der Nähe).

    Gibt (temperatur, messzeitpunkt) zurück oder None, falls nicht verfügbar.
    Ein Fehler hier darf den Check nicht abbrechen, die Mail geht trotzdem raus.
    """
    try:
        url = f"https://api.brightsky.dev/current_weather?lat={lat}&lon={lon}&max_dist={CURRENT_MAX_DIST}"
        weather = http_get_json(url).get("weather") or {}
        temp = weather.get("temperature")
        ts = weather.get("timestamp")
        if temp is None:
            return None
        return temp, (parse_dt(ts).astimezone(CET) if ts else None)
    except Exception:  # noqa: BLE001
        return None


# ---------- Hilfsfunktionen ----------
def iso_cet(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00+01:00")


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dt(value) -> str:
    """Zeitangabe (String oder datetime) lesbar in deutscher Zeit, z.B. '13.11. 04:00 Uhr'."""
    if value is None:
        return "?"
    dt = parse_dt(value) if isinstance(value, str) else value
    return dt.astimezone(CET).strftime("%d.%m. %H:%M Uhr")


# ---------- Auswertung pro Standort ----------
def evaluate_location(loc, window_start_dt, window_end_dt):
    window_start = iso_cet(window_start_dt)
    window_end = iso_cet(window_end_dt)

    weather = fetch_weather(loc["lat"], loc["lon"], loc["max_dist"], window_start, window_end)
    alerts = fetch_alerts(loc["lat"], loc["lon"])
    current = fetch_current_temperature(loc["lat"], loc["lon"])

    # --- Kriterium a) Neuschnee / Schneedecke >= 1 cm ---
    # Faustregel: ca. 1 mm Niederschlag als Schnee ≈ 1 cm Neuschnee.
    snow_precip_sum = 0.0
    for h in weather:
        if h.get("condition") == "snow" and h.get("temperature") is not None and h["temperature"] <= 1:
            snow_precip_sum += h.get("precipitation") or 0
    kriterium_a = snow_precip_sum >= 1

    # --- Kriterium b) Glätte (inkl. "überfrierende Nässe") ---
    # Nicht nur zeitgleich prüfen: Niederschlag kann z.B. bei +4°C tagsüber fallen,
    # die Fläche bleibt nass, und erst Stunden später sinkt die Temperatur auf ≤1°C.
    # Das AGB-Kriterium ("Nässe ... bei Lufttemperaturen von +1°C oder darunter
    # vorlag") deckt genau diesen Fall ab -> wir prüfen sequenziell, nicht nur stundengleich.
    precip_times = [
        parse_dt(h["timestamp"]) for h in weather if (h.get("precipitation") or 0) > 0.05
    ]
    first_precip_time = min(precip_times) if precip_times else None

    glaette_stunde = None
    grenzfall_stunde = None
    if first_precip_time:
        for h in weather:
            if h.get("temperature") is None:
                continue
            t = parse_dt(h["timestamp"])
            if t < first_precip_time:
                continue
            if h["temperature"] <= 1 and glaette_stunde is None:
                glaette_stunde = h["timestamp"]
            if h["temperature"] <= GRENZFALL_TEMP and grenzfall_stunde is None:
                grenzfall_stunde = h["timestamp"]
    kriterium_b = glaette_stunde is not None
    # Grenzfall nur relevant, wenn das harte Kriterium (noch) nicht erfüllt ist
    grenzfall = grenzfall_stunde is not None and not kriterium_b

    # --- Reif-Risiko (auch ohne Niederschlag) ---
    # Die AGB zählen Reif ausdrücklich als Glätte. Fehlende Messwerte (Wind/Wolken) werden
    # bewusst vorsichtig behandelt (= Bedingung gilt als erfüllt), fehlende Feuchte nicht.
    reif_stunde = None
    for h in weather:
        temp = h.get("temperature")
        if temp is None or temp > REIF_TEMP:
            continue
        hum = h.get("relative_humidity")
        wind = h.get("wind_speed")
        cloud = h.get("cloud_cover")
        feucht = hum is not None and hum >= REIF_MIN_FEUCHTE
        windarm = wind is None or wind <= REIF_MAX_WIND
        klar = cloud is None or cloud <= REIF_MAX_WOLKEN
        if feucht and windarm and klar:
            reif_stunde = h["timestamp"]
            break
    reif_risiko = reif_stunde is not None and not kriterium_b

    # --- Kriterium c) Offizielle DWD-Warnung ---
    relevant_alerts = []
    for a in alerts:
        event = (a.get("event_de") or "").lower()
        if not any(k in event for k in ["schnee", "glätte", "glatteis", "eis"]):
            continue
        onset = parse_dt(a["onset"]) if a.get("onset") else None
        expires = parse_dt(a["expires"]) if a.get("expires") else None
        if onset and onset > window_end_dt.astimezone(timezone.utc):
            continue
        if expires and expires < window_start_dt.astimezone(timezone.utc):
            continue
        relevant_alerts.append(a)
    kriterium_c = len(relevant_alerts) > 0

    temps = [h["temperature"] for h in weather if h.get("temperature") is not None]
    min_temp = min(temps) if temps else None
    precip_sum = sum(h.get("precipitation") or 0 for h in weather)

    # Bis jetzt bereits eingetretene Werte (beim Morgen-Check überwiegend Messwerte der DWD-Stationen)
    jetzt = datetime.now(timezone.utc)
    bisher = [h for h in weather if parse_dt(h["timestamp"]) <= jetzt]
    bisher_temps = [h["temperature"] for h in bisher if h.get("temperature") is not None]
    bisher_min = min(bisher_temps) if bisher_temps else None
    bisher_precip = sum(h.get("precipitation") or 0 for h in bisher)

    einsatz_noetig = kriterium_a or kriterium_b or kriterium_c

    # --- Risikostufe 1-6 ---
    #  6: extremes Unwetter (DWD) oder sehr viel Schnee (>= STUFE6_SCHNEE_MM)
    #  5: Unwetter (DWD) oder deutlicher Schnee (>= STUFE5_SCHNEE_MM)
    #  4: mindestens ein AGB-Kriterium erfüllt -> Einsatz nötig
    #  3: Grenzfall (Nässe + ≤3°C) oder Reif-Risiko (klar, windarm, feucht, ≤3°C)
    #  2: leicht kühl (≤3°C irgendwann im Fenster)
    #  1: kein Risiko
    max_severity = max((SEVERITY_RANK.get(a.get("severity"), 0) for a in relevant_alerts), default=0)
    if snow_precip_sum >= STUFE6_SCHNEE_MM or max_severity >= 4:
        stufe = 6
    elif snow_precip_sum >= STUFE5_SCHNEE_MM or max_severity >= 3:
        stufe = 5
    elif einsatz_noetig:
        stufe = 4
    elif grenzfall or reif_risiko:
        stufe = 3
    elif min_temp is not None and min_temp <= GRENZFALL_TEMP:
        stufe = 2
    else:
        stufe = 1

    return {
        "loc": loc,
        "kriterium_a": kriterium_a,
        "kriterium_b": kriterium_b,
        "kriterium_c": kriterium_c,
        "grenzfall": grenzfall,
        "grenzfall_stunde": grenzfall_stunde,
        "reif_risiko": reif_risiko,
        "reif_stunde": reif_stunde,
        "snow_precip_sum": snow_precip_sum,
        "glaette_stunde": glaette_stunde,
        "first_precip_time": first_precip_time,
        "relevant_alerts": relevant_alerts,
        "min_temp": min_temp,
        "precip_sum": precip_sum,
        "bisher_min": bisher_min,
        "bisher_precip": bisher_precip,
        "bisher_vorhanden": bool(bisher),
        "current": current,
        "einsatz_noetig": einsatz_noetig,
        "stufe": stufe,
    }


# ---------- Hauptlogik ----------
STUFE_LABEL = {
    1: "🟢 Stufe 1/6 – kein Risiko",
    2: "🟡 Stufe 2/6 – leicht kühl, kein Einsatz",
    3: "🟠 Stufe 3/6 – GRENZFALL, im Zweifel AUSRÜCKEN",
    4: "🔴 Stufe 4/6 – EINSATZ NÖTIG",
    5: "🟣 Stufe 5/6 – EINSATZ DRINGEND (deutlicher Schnee / Unwetterwarnung)",
    6: "⚫❄️ Stufe 6/6 – EXTREM (sehr viel Schnee / extreme Unwetterwarnung)",
}


# Klare Handlungsanweisung pro Stufe und Mail-Art (auch für Mitarbeiter gedacht).
# Niemand soll extra zu den Objekten fahren, um zu "prüfen": Es gilt "im Zweifel ausrücken".
ACTION = {
    # Abend-Mail (19:55 Uhr): Vorwarnung, die Entscheidung fällt morgens
    "abend": {
        1: "Nichts zu tun.",
        2: "Nichts zu tun. Die Entscheidung kommt morgen früh mit der Mail um " + MORGEN_MAIL_UHRZEIT + " Uhr.",
        3: "Grenzfall. Morgen früh die Mail um " + MORGEN_MAIL_UHRZEIT + " Uhr abwarten. "
           "Im Zweifel wird ausgerückt, bitte darauf einstellen.",
        4: "Einsatz voraussichtlich nötig. Bitte morgen früh einsatzbereit sein. "
           "Die endgültige Mail kommt um " + MORGEN_MAIL_UHRZEIT + " Uhr.",
        5: "Einsatz dringend zu erwarten. Bitte morgen früh frühzeitig einsatzbereit sein. "
           "Die endgültige Mail kommt um " + MORGEN_MAIL_UHRZEIT + " Uhr.",
        6: "Extremer Einsatz zu erwarten. Bitte morgen früh frühzeitig einsatzbereit sein und den Chef informieren. "
           "Die endgültige Mail kommt um " + MORGEN_MAIL_UHRZEIT + " Uhr.",
    },
    # Morgen-Mail (4:55 Uhr): hier fällt die Entscheidung
    "morgen": {
        1: "KEIN EINSATZ. Nichts zu tun.",
        2: "KEIN EINSATZ. Nichts zu tun (nur leicht kühl).",
        3: "AUSRÜCKEN (Grenzfall, im Zweifel wird gestreut). Ausrücken und streuen. "
           "Danach Uhrzeit und Foto festhalten und diese Mail aufheben.",
        4: "AUSRÜCKEN. Ausrücken und streuen bzw. räumen. "
           "Danach Uhrzeit und Foto festhalten und diese Mail aufheben.",
        5: "AUSRÜCKEN, DRINGEND. Sofort los, räumen und streuen. Fläche später noch einmal kontrollieren. "
           "Uhrzeit und Foto festhalten und diese Mail aufheben.",
        6: "AUSRÜCKEN, EXTREM. Sofort los, Chef informieren, bei Bedarf zusätzliches Personal holen. "
           "Fläche mehrfach kontrollieren. Uhrzeit und Foto festhalten und diese Mail aufheben.",
    },
}


def determine_mode(now: datetime) -> str:
    """Vor 7 Uhr: Morgen-Check für heute. Ab 7 Uhr: Abend-Check für morgen.

    Unabhängig von der exakten Startzeit, damit auch ein verspäteter GitHub-Lauf
    (oder ein manueller Start) immer den richtigen Zieltag bekommt.
    """
    return "morgen" if now.hour < WINDOW_END_HOUR else "abend"


def run_check(mode: str, manual: bool = False):
    now = datetime.now(CET)

    # Zieltag: beim Abend-Check "morgen", beim Morgen-Check "heute"
    target_date = (now + timedelta(days=1)).date() if mode == "abend" else now.date()
    day_before = target_date - timedelta(days=1)

    window_start_dt = datetime(day_before.year, day_before.month, day_before.day, 20, 0, tzinfo=CET)
    window_end_dt = datetime(
        target_date.year, target_date.month, target_date.day, WINDOW_END_HOUR, 0, tzinfo=CET
    )

    results = [evaluate_location(loc, window_start_dt, window_end_dt) for loc in LOCATIONS]
    einsatz_noetig = any(r["einsatz_noetig"] for r in results)
    gesamt_stufe = max(r["stufe"] for r in results)

    tag_label = "morgen" if mode == "abend" else "heute"
    datum_label = target_date.strftime("%d.%m.%Y")

    subject = f"Winterdienst {tag_label} ({datum_label}): {STUFE_LABEL[gesamt_stufe]}"
    if manual:
        subject = "[Manueller Lauf] " + subject

    lines = [f"Gesamt-Risikostufe: {STUFE_LABEL[gesamt_stufe]}", ""]
    if mode == "morgen":
        einsatz_orte = [r["loc"]["name"].split(" (")[0] for r in results if r["stufe"] >= 3]
        if einsatz_orte:
            lines.append(f"➡️ ENTSCHEIDUNG: AUSRÜCKEN ({', '.join(einsatz_orte)})")
        else:
            lines.append("➡️ ENTSCHEIDUNG: KEIN EINSATZ NÖTIG")
        lines.append("")

    if einsatz_noetig:
        lines.append(f"Laut Prognose/Warnung ist {tag_label} ({datum_label}) voraussichtlich ein Einsatz nötig.")
    elif gesamt_stufe == 3:
        ende = (
            "Im Zweifel wird ausgerückt."
            if mode == "morgen"
            else f"Die Entscheidung kommt morgen früh um {MORGEN_MAIL_UHRZEIT} Uhr, im Zweifel wird ausgerückt."
        )
        lines.append(
            f"Formal ist noch kein AGB-Kriterium erfüllt, aber {tag_label} ({datum_label}) ist ein Grenzfall "
            f"(kühl und nass bzw. Reif-Gefahr) – die Fläche kann kälter sein als die Luft. {ende}"
        )
    elif gesamt_stufe == 2:
        lines.append(f"Für {tag_label} ({datum_label}) ist kein Einsatz nötig, es wird nur leicht kühl.")
    else:
        lines.append(
            f"Laut Prognose/Warnung ist für {tag_label} ({datum_label}) an beiden Standorten aktuell kein Einsatz nötig."
        )
    lines.append(f"Stand der Daten: {now.strftime('%d.%m.%Y, %H:%M')} Uhr" + (" (manueller Lauf)" if manual else ""))
    lines.append("")

    for r in results:
        lines.append(f"── {r['loc']['name']} – {STUFE_LABEL[r['stufe']]} ──")
        lines.append(f"→ ZU TUN: {ACTION[mode][r['stufe']]}")
        if r["current"]:
            temp, gemessen = r["current"]
            zeit = f" (gemessen {gemessen.strftime('%H:%M')} Uhr)" if gemessen else ""
            lines.append(f"Aktuelle Temperatur: {temp}°C{zeit}")
        else:
            lines.append("Aktuelle Temperatur: k.A. (Messwert gerade nicht abrufbar)")
        if mode == "morgen" and r["bisher_vorhanden"] and r["bisher_min"] is not None:
            lines.append(
                f"Seit gestern 20 Uhr bis jetzt: Tiefstwert {r['bisher_min']}°C, Niederschlag {r['bisher_precip']:.1f} mm"
            )
        if r["kriterium_a"]:
            lines.append(
                f"• Neuschnee/Schneedecke: ca. {r['snow_precip_sum']:.1f} mm Niederschlag als Schnee bei ≤1°C "
                f"(Faustregel ≈ cm Schnee)"
            )
        if r["kriterium_b"]:
            lines.append(
                f"• Glätte-Risiko (AGB, ≤1°C): Nässe seit {fmt_dt(r['first_precip_time'])}, "
                f"Temperatur ≤1°C ab {fmt_dt(r['glaette_stunde'])}"
            )
        if r["kriterium_c"]:
            for a in r["relevant_alerts"]:
                bis = f", bis {fmt_dt(a.get('expires'))}" if a.get("expires") else ""
                lines.append(
                    f"• DWD-Warnung: {a.get('event_de')} – {a.get('headline_de')} (ab {fmt_dt(a.get('onset'))}{bis})"
                )
        if r["grenzfall"]:
            lines.append(
                f"• Grenzfall-Vorwarnung: Nässe seit {fmt_dt(r['first_precip_time'])}, Temperatur ≤{GRENZFALL_TEMP}°C "
                f"ab {fmt_dt(r['grenzfall_stunde'])} (AGB-Schwelle 1°C noch nicht erreicht)"
            )
        if r["reif_risiko"]:
            lines.append(
                f"• Reif-Risiko: ab {fmt_dt(r['reif_stunde'])} klar, windarm und feucht bei ≤{REIF_TEMP}°C – "
                "die Fläche kann trotz Plusgraden überfrieren bzw. Reif bilden"
            )
        if r["stufe"] == 1:
            lines.append("Keines der Kriterien bzw. Vorwarnstufen erfüllt.")
        min_temp_str = f"{r['min_temp']}°C" if r["min_temp"] is not None else "k.A."
        lines.append(f"Prognose Min. Temp: {min_temp_str}, Niederschlagssumme: {r['precip_sum']:.1f} mm")
        lines.append("")

    lines.append(f"Aktuelle DWD-Warnlage ansehen: {DWD_WARN_URL}")
    lines.append(f"Check manuell mit frischen Daten neu laufen lassen: {RERUN_URL}")
    lines.append("")
    lines.append(
        "⚠️ Automatische Einschätzung auf Basis von DWD-Daten (Bright Sky API), geprüft für die beiden "
        "Standorte Rellingen und Ellerbek im 10-km-Umkreis. Die Fläche selbst kann kälter sein als die Luft. "
        "Deshalb gilt in Grenzfällen: im Zweifel ausrücken."
    )

    send_email(subject, "\n".join(lines))


def send_email(subject: str, body: str):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD fehlen (als GitHub Secrets setzen).")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_TO

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [NOTIFY_TO], msg.as_string())


def main():
    now_cet = datetime.now(CET)
    # Bei "Run workflow" in GitHub ist das Event "workflow_dispatch" -> Mail wird als manuell markiert.
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    try:
        run_check(determine_mode(now_cet), manual=manual)
    except Exception as err:  # noqa: BLE001
        try:
            send_email(
                "⚠️ Winterdienst-Check fehlgeschlagen",
                f"Der automatische Check ist mit einem Fehler abgebrochen:\n\n{err}",
            )
        except Exception:
            # Wenn selbst der Fehler-Versand scheitert, zumindest im GitHub-Actions-Log sichtbar machen.
            print(f"FEHLER (auch Mail-Versand fehlgeschlagen): {err}")
            raise


if __name__ == "__main__":
    main()
