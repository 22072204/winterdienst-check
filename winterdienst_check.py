#!/usr/bin/env python3
"""
Winterdienst-Check für Niclas.
Läuft 2x täglich über GitHub Actions: 20:00 Uhr (Abend-Check für morgen)
und 3:00 Uhr (Morgen-Check für heute), jeweils deutsche Winterzeit (CET, UTC+1).

Prüft die 3 AGB-Kriterien für zwei Standorte (Rellingen, Ellerbek), je 10 km Umkreis:
  a) Neuschnee / Schneedecke >= 1 cm
  b) Glätte (Niederschlag/Nässe seit 20 Uhr Vortag, Temp irgendwann danach <= +1°C –
     nicht zwingend zeitgleich, deckt "überfrierende Nässe" ab)
  c) Offizielle DWD-Warnung (Schnee/Glätte/Glatteis) für den Standort

Zusätzlich eine Risikostufe 1-5 (siehe risk_level()), inkl. einer Vorwarnstufe für
Grenzfälle (≤3°C Luft statt ≤1°C), weil die Straßenoberfläche in klaren, windstillen
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

# Ende des Prüffensters am Zieltag (Uhrzeit, bis wann "morgens" zählt)
WINDOW_END_HOUR = 7

# Sicherheitsmarge für die Vorwarnstufe (Grenzfall), zusätzlich zum harten
# AGB-Kriterium b) bei 1°C. Luft kann 2-4°C wärmer sein als die Fläche selbst.
GRENZFALL_TEMP = 3

# Für Risikostufe 5: ab wie viel mm Niederschlag-als-Schnee (≈ cm Neuschnee)
STUFE5_SCHNEE_MM = 5

# Link, über den du den Check jederzeit manuell mit frischen Daten neu anstoßen kannst
# (GitHub-Login im Browser nötig). Anpassen, falls Benutzername/Repo-Name abweichen.
RERUN_URL = "https://github.com/22072204/winterdienst-check/actions/workflows/winterdienst.yml"

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


# ---------- Hilfsfunktionen ----------
def iso_cet(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00+01:00")


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- Auswertung pro Standort ----------
def evaluate_location(loc, window_start_dt, window_end_dt):
    window_start = iso_cet(window_start_dt)
    window_end = iso_cet(window_end_dt)

    weather = fetch_weather(loc["lat"], loc["lon"], loc["max_dist"], window_start, window_end)
    alerts = fetch_alerts(loc["lat"], loc["lon"])

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

    einsatz_noetig = kriterium_a or kriterium_b or kriterium_c

    # --- Risikostufe 1-5 ---
    max_severity = max((SEVERITY_RANK.get(a.get("severity"), 0) for a in relevant_alerts), default=0)
    if snow_precip_sum >= STUFE5_SCHNEE_MM or max_severity >= 3:
        stufe = 5
    elif einsatz_noetig:
        stufe = 4
    elif grenzfall:
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
        "snow_precip_sum": snow_precip_sum,
        "glaette_stunde": glaette_stunde,
        "first_precip_time": first_precip_time,
        "relevant_alerts": relevant_alerts,
        "min_temp": min_temp,
        "precip_sum": precip_sum,
        "einsatz_noetig": einsatz_noetig,
        "stufe": stufe,
    }


# ---------- Hauptlogik ----------
def run_check(mode: str):
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

    STUFE_LABEL = {
        1: "🟢 Stufe 1/5 – kein Risiko",
        2: "🟡 Stufe 2/5 – leicht kühl, kein akutes Risiko",
        3: "🟠 Stufe 3/5 – GRENZFALL, bitte im Auge behalten",
        4: "🔴 Stufe 4/5 – EINSATZ NÖTIG",
        5: "⚫❄️ Stufe 5/5 – EINSATZ DRINGEND (deutlicher Schnee / schwere Warnung)",
    }

    subject = f"Winterdienst {tag_label} ({datum_label}): {STUFE_LABEL[gesamt_stufe]}"

    lines = [f"Gesamt-Risikostufe: {STUFE_LABEL[gesamt_stufe]}", ""]
    if einsatz_noetig:
        lines.append(f"Laut Prognose/Warnung musst du {tag_label} ({datum_label}) voraussichtlich ausrücken.")
    elif gesamt_stufe == 3:
        lines.append(
            f"Formal ist noch keines deiner AGB-Kriterien erfüllt, aber {tag_label} ({datum_label}) ist ein "
            "Grenzfall (kühl + Nässe) – die Straße kann kälter sein als die Luft. Lieber kurz selbst checken."
        )
    else:
        lines.append(
            f"Laut Prognose/Warnung ist für {tag_label} ({datum_label}) an beiden Standorten aktuell kein Einsatz nötig."
        )
    lines.append("")

    for r in results:
        lines.append(f"── {r['loc']['name']} – {STUFE_LABEL[r['stufe']]} ──")
        if r["kriterium_a"]:
            lines.append(
                f"• Neuschnee/Schneedecke: ca. {r['snow_precip_sum']:.1f} mm Niederschlag als Schnee bei ≤1°C "
                f"(Faustregel ≈ cm Schnee)"
            )
        if r["kriterium_b"]:
            lines.append(
                f"• Glätte-Risiko (AGB, ≤1°C): Nässe seit {r['first_precip_time']}, "
                f"Temperatur ≤1°C ab {r['glaette_stunde']}"
            )
        if r["kriterium_c"]:
            for a in r["relevant_alerts"]:
                bis = f", bis {a.get('expires')}" if a.get("expires") else ""
                lines.append(f"• DWD-Warnung: {a.get('event_de')} – {a.get('headline_de')} (ab {a.get('onset')}{bis})")
        if r["grenzfall"]:
            lines.append(
                f"• Grenzfall-Vorwarnung: Nässe seit {r['first_precip_time']}, Temperatur ≤{GRENZFALL_TEMP}°C "
                f"ab {r['grenzfall_stunde']} (AGB-Schwelle 1°C noch nicht erreicht)"
            )
        if r["stufe"] == 1:
            lines.append("Keines der Kriterien bzw. Vorwarnstufen erfüllt.")
        min_temp_str = f"{r['min_temp']}°C" if r["min_temp"] is not None else "k.A."
        lines.append(f"Min. Temp: {min_temp_str}, Niederschlagssumme: {r['precip_sum']:.1f} mm")
        lines.append("")

    lines.append(f"Zeitfenster: {iso_cet(window_start_dt)} – {iso_cet(window_end_dt)}")
    lines.append("")
    lines.append(f"Check manuell mit frischen Daten neu laufen lassen: {RERUN_URL}")
    lines.append("")
    lines.append(
        "⚠️ Automatische Einschätzung auf Basis der DWD-Vorhersage (Bright Sky API), geprüft für die "
        "beiden Standorte Rellingen und Ellerbek im 10-km-Umkreis. Bei Stufe 2-3 oder kurz vor knapp: "
        "bitte kurz selbst aus dem Fenster schauen bzw. die DWD-Warnlage prüfen, bevor du dich "
        "verbindlich festlegst."
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
    h = now_cet.hour

    try:
        if h == 20:
            run_check("abend")
        elif h == 3:
            run_check("morgen")
        else:
            # Manueller Testlauf (z.B. über "Run workflow" in GitHub) außerhalb der Cron-Zeiten:
            # einfach als Abend-Check behandeln, damit man sofort eine Mail zum Testen bekommt.
            run_check("abend")
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
