#!/usr/bin/env python3

import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import matplotlib.pyplot as plt

REPO_PATH = "/home/juan/pi-daily-status"

# Scheduling behavior
SKIP_WEEKDAYS = {6}       # Sunday only; Monday=0 ... Sunday=6
SKIP_CHANCE = 0.0         # 0.0 = do not randomly skip eligible days
MIN_DELAY = 0
MAX_DELAY = 8 * 60 * 60   # up to 8 hours after cron starts

PING_TARGET = "1.1.1.1"
CITY_CONFIG = {
    "Madrid": {
	"municipio_id": "28079",
	"warning_zone": "Metropolitana y Henares",
	"warnings_page": "https://www.aemet.es/es/eltiempo/prediccion/avisos?k=mad",
    },
    "Barcelona": {
	"municipio_id": "08019",
	"warning_zone": "Litoral de Barcelona",
	"warnings_page": "https://www.aemet.es/es/eltiempo/prediccion/avisos?k=cat",
    },
}


COMMIT_MESSAGES = [
    "Add daily system and weather report",
    "Update Pi daily status",
    "Record daily machine check",
    "Add daily health and weather snapshot",
]


def run(cmd, cwd=REPO_PATH, check=True):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True
    )
    if check and result.returncode != 0:
        print(f"Command failed: {' '.join(cmd)}")
        print(result.stdout)
        print(result.stderr)
        sys.exit(result.returncode)
    return result


def command_output(cmd):
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_uptime():
    out = command_output(["uptime", "-p"])
    return out if out else "Unavailable"


def get_load_average():
    try:
        load1, load5, load15 = os.getloadavg()
        return {
            "load1": load1,
            "load5": load5,
            "load15": load15,
            "text": f"{load1:.2f}, {load5:.2f}, {load15:.2f}"
        }
    except OSError:
        return {
            "load1": None,
            "load5": None,
            "load15": None,
            "text": "Unavailable"
        }


def get_ram_info():
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            lines = f.readlines()

        mem_total_kb = None
        mem_available_kb = None

        for line in lines:
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available_kb = int(line.split()[1])

        if mem_total_kb is None or mem_available_kb is None:
            return {
                "used_mb": None,
                "total_mb": None,
                "pct": None,
                "text": "Unavailable"
            }

        used_kb = mem_total_kb - mem_available_kb
        total_mb = mem_total_kb / 1024
        used_mb = used_kb / 1024
        pct = (used_kb / mem_total_kb) * 100

        return {
            "used_mb": used_mb,
            "total_mb": total_mb,
            "pct": pct,
            "text": f"{used_mb:.0f} MB / {total_mb:.0f} MB ({pct:.1f}%)"
        }
    except Exception:
        return {
            "used_mb": None,
            "total_mb": None,
            "pct": None,
            "text": "Unavailable"
        }


def get_disk_info():
    try:
        usage = shutil.disk_usage("/")
        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)
        pct = (usage.used / usage.total) * 100

        return {
            "used_gb": used_gb,
            "total_gb": total_gb,
            "free_gb": free_gb,
            "pct": pct,
            "text": f"{used_gb:.1f} GB / {total_gb:.1f} GB ({pct:.1f}%)"
        }
    except Exception:
        return {
            "used_gb": None,
            "total_gb": None,
            "free_gb": None,
            "pct": None,
            "text": "Unavailable"
        }


def get_cpu_temp():
    thermal_path = "/sys/class/thermal/thermal_zone0/temp"

    if os.path.exists(thermal_path):
        try:
            with open(thermal_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            temp_c = int(raw) / 1000
            return {
                "value": temp_c,
                "text": f"{temp_c:.1f}°C"
            }
        except Exception:
            pass

    out = command_output(["vcgencmd", "measure_temp"])
    if out and "temp=" in out:
        match = re.search(r"temp=([0-9.]+)", out)
        if match:
            temp_c = float(match.group(1))
            return {
                "value": temp_c,
                "text": f"{temp_c:.1f}°C"
            }

    return {
        "value": None,
        "text": "Unavailable"
    }


def decode_throttled_flags(value):
    issues = []

    flags = {
        0: "Undervoltage currently detected",
        1: "ARM frequency currently capped",
        2: "Currently throttled",
        3: "Soft temperature limit currently active",
        16: "Undervoltage has occurred",
        17: "ARM frequency capping has occurred",
        18: "Throttling has occurred",
        19: "Soft temperature limit has occurred",
    }

    for bit, label in flags.items():
        if value & (1 << bit):
            issues.append(label)

    return issues


def get_voltage_status():
    out = command_output(["vcgencmd", "get_throttled"])
    if not out or "throttled=" not in out:
        return {
            "raw": None,
            "issues": [],
            "text": "Unavailable"
        }

    raw_value = out.split("=", 1)[1].strip()

    try:
        numeric_value = int(raw_value, 16) if raw_value.startswith("0x") else int(raw_value)
    except ValueError:
        return {
            "raw": raw_value,
            "issues": [],
            "text": f"Unparsed value: {raw_value}"
        }

    issues = decode_throttled_flags(numeric_value)

    return {
        "raw": raw_value,
        "issues": issues,
        "text": "OK" if not issues else "; ".join(issues)
    }


def get_reachability():
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "2", PING_TARGET],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        return {
            "reachable": False,
            "latency_ms": None,
            "text": "Unreachable"
        }

    match = re.search(r"time=([0-9.]+)\s*ms", result.stdout)
    latency_ms = float(match.group(1)) if match else None

    return {
        "reachable": True,
        "latency_ms": latency_ms,
        "text": f"Reachable ({latency_ms:.1f} ms)" if latency_ms is not None else "Reachable"
    }


def fetch_json(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()

    for encoding in ("utf-8", "iso-8859-15", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    return raw.decode("latin-1", errors="replace")


def aemet_daily_xml_url(municipio_id):
    return f"https://www.aemet.es/xml/municipios/localidad_{municipio_id}.xml"


def aemet_hourly_xml_url(municipio_id):
    # AEMET hourly municipality XML follows this pattern for municipality IDs.
    return f"https://www.aemet.es/xml/municipios_h/localidad_h_{municipio_id}.xml"


def clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def to_int(value):
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def strip_html_tags(text):
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_today_dia_node(root):
    today_str = datetime.date.today().isoformat()

    for dia in root.findall(".//dia"):
        fecha = dia.attrib.get("fecha", "")
        if fecha.startswith(today_str):
            return dia

    dias = root.findall(".//dia")
    return dias[0] if dias else None


def parse_condition_from_dia(dia):
    descriptions = []

    for node in dia.findall(".//estado_cielo"):
        desc = clean_text(node.attrib.get("descripcion"))
        if desc and desc not in descriptions:
            descriptions.append(desc)

    if descriptions:
        return descriptions[0]

    return "Unavailable"


def parse_max_min_from_dia(dia):
    maxima = clean_text(dia.findtext(".//temperatura/maxima")) or "N/A"
    minima = clean_text(dia.findtext(".//temperatura/minima")) or "N/A"
    return maxima, minima


def parse_rain_chance_from_dia(dia):
    values = []

    for node in dia.findall(".//prob_precipitacion"):
        value = to_int(node.text)
        if value is not None:
            values.append(value)

    return max(values) if values else "N/A"


def parse_feels_like_from_hourly_xml(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "N/A"

    today_str = datetime.date.today().isoformat()
    current_hour = datetime.datetime.now().hour

    best_value = None
    best_distance = None

    for dia in root.findall(".//dia"):
        fecha = dia.attrib.get("fecha", "")
        if not fecha.startswith(today_str):
            continue

        for node in dia.findall(".//sens_termica/dato"):
            hour_attr = node.attrib.get("hora")
            value = clean_text(node.text)

            if not hour_attr or not value:
                continue

            try:
                hour = int(hour_attr)
            except ValueError:
                continue

            distance = abs(hour - current_hour)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_value = value

    return best_value if best_value else "N/A"


def get_warning_summary(page_url, zone_name):
    try:
        html_text = fetch_text(page_url, timeout=20)
        text = strip_html_tags(html_text)

        if "sin avisos" in text.lower() or "no hay avisos" in text.lower():
            return "None reported"

        idx = text.lower().find(zone_name.lower())
        if idx == -1:
            return "None reported"

        start = max(0, idx - 160)
        end = min(len(text), idx + 220)
        snippet = text[start:end].strip(" -:;,.")
        snippet = re.sub(r"\s+", " ", snippet)

        return snippet if snippet else "Warning present"
    except Exception as e:
        return f"Unavailable ({e.__class__.__name__})"


def get_weather_for_city(city):
    config = CITY_CONFIG[city]

    try:
        daily_xml = fetch_text(aemet_daily_xml_url(config["municipio_id"]), timeout=20)
        root = ET.fromstring(daily_xml)
        dia = get_today_dia_node(root)

        if dia is None:
            raise ValueError("No forecast day found")

        condition = parse_condition_from_dia(dia)
        max_temp, min_temp = parse_max_min_from_dia(dia)
        chance_of_rain = parse_rain_chance_from_dia(dia)

        try:
            hourly_xml = fetch_text(aemet_hourly_xml_url(config["municipio_id"]), timeout=20)
            feels_like = parse_feels_like_from_hourly_xml(hourly_xml)
        except Exception:
            feels_like = "N/A"

        warnings = get_warning_summary(
            config["warnings_page"],
            config["warning_zone"]
        )

        return {
            "city": city,
            "condition": condition,
            "max_temp": max_temp,
            "min_temp": min_temp,
            "feels_like": feels_like,
            "chance_of_rain": chance_of_rain,
            "warnings": warnings,
        }

    except Exception as e:
        return {
            "city": city,
            "condition": "Unavailable",
            "max_temp": "N/A",
            "min_temp": "N/A",
            "feels_like": "N/A",
            "chance_of_rain": "N/A",
            "warnings": f"Unavailable ({e.__class__.__name__})",
        }

def build_health_summary(disk, cpu_temp, voltage, reachability, weather_reports=None):
    warnings = []

    if disk["pct"] is not None:
        if disk["pct"] >= 90:
            warnings.append(f"Disk usage critical ({disk['pct']:.1f}%)")
        elif disk["pct"] >= 80:
            warnings.append(f"Disk usage high ({disk['pct']:.1f}%)")

    if cpu_temp["value"] is not None:
        if cpu_temp["value"] >= 80:
            warnings.append(f"CPU temperature critical ({cpu_temp['value']:.1f}°C)")
        elif cpu_temp["value"] >= 70:
            warnings.append(f"CPU temperature high ({cpu_temp['value']:.1f}°C)")

    if voltage["issues"]:
        warnings.append(f"Power issue detected: {voltage['text']}")

    if not reachability["reachable"]:
        warnings.append("Internet unreachable")

    if weather_reports:
        for report in weather_reports:
            if report["condition"] == "Unavailable":
                warnings.append(f"Weather data unavailable for {report['city']}")

    return {
        "overall": "Warning" if warnings else "Good",
        "warnings": warnings
    }


def ensure_month_file():
    today = datetime.date.today()
    filename = f"{today.year}-{today.month:02d}.md"
    filepath = os.path.join(REPO_PATH, filename)

    if not os.path.exists(filepath):
        month_name = today.strftime("%B %Y")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# {month_name}\n\n")

    return filepath, filename


def entry_for_today_exists(filepath):
    today_str = datetime.date.today().isoformat()

    if not os.path.exists(filepath):
        return False

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    return f"## {today_str} " in content or f"## {today_str}\n" in content


def build_health_summary(disk, cpu_temp, voltage, reachability):
    warnings = []

    if disk["pct"] is not None:
        if disk["pct"] >= 90:
            warnings.append(f"Disk usage critical ({disk['pct']:.1f}%)")
        elif disk["pct"] >= 80:
            warnings.append(f"Disk usage high ({disk['pct']:.1f}%)")

    if cpu_temp["value"] is not None:
        if cpu_temp["value"] >= 80:
            warnings.append(f"CPU temperature critical ({cpu_temp['value']:.1f}°C)")
        elif cpu_temp["value"] >= 70:
            warnings.append(f"CPU temperature high ({cpu_temp['value']:.1f}°C)")

    if voltage["issues"]:
        warnings.append(f"Power issue detected: {voltage['text']}")

    if not reachability["reachable"]:
        warnings.append("Internet unreachable")

    if weather_reports:
        for report in weather_reports:
            if report["condition"] == "Unavailable":
                warnings.append(f"Weather data unavailable for {report['city']}")

    return {
        "overall": "Warning" if warnings else "Good",
        "warnings": warnings
    }


def update_history_and_graphs(cpu_temp_value, disk_pct):
    history_file = os.path.join(REPO_PATH, "history.json")
    today = datetime.date.today().isoformat()

    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    else:
        history = []

    history = [entry for entry in history if entry.get("date") != today]

    history.append({
        "date": today,
        "cpu_temp": cpu_temp_value,
        "disk": disk_pct
    })

    history = history[-60:]

    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    dates = [x["date"] for x in history]
    temps = [x["cpu_temp"] for x in history]
    disks = [x["disk"] for x in history]

    plt.figure(figsize=(8, 4))
    plt.plot(dates, temps, marker="o")
    plt.xticks(rotation=45, ha="right")
    plt.title("CPU Temperature (°C)")
    plt.tight_layout()
    plt.savefig(os.path.join(REPO_PATH, "cpu_temp.png"))
    plt.close()

    plt.figure(figsize=(8, 4))
    plt.plot(dates, disks, marker="o")
    plt.xticks(rotation=45, ha="right")
    plt.title("Disk Usage (%)")
    plt.tight_layout()
    plt.savefig(os.path.join(REPO_PATH, "disk_usage.png"))
    plt.close()


def build_entry():
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    uptime = get_uptime()
    load = get_load_average()
    ram = get_ram_info()
    disk = get_disk_info()
    cpu_temp = get_cpu_temp()
    voltage = get_voltage_status()
    reachability = get_reachability()

    weather_reports = [get_weather_for_city(city) for city in CITY_CONFIG]
    health = build_health_summary(disk, cpu_temp, voltage, reachability, weather_reports)

    if cpu_temp["value"] is not None and disk["pct"] is not None:
        update_history_and_graphs(cpu_temp["value"], disk["pct"])

    lines = [
        f"## {timestamp}",
        "",
        "### Machine",
        f"- Uptime: {uptime}",
        f"- CPU load (1m, 5m, 15m): {load['text']}",
        f"- RAM used: {ram['text']}",
        f"- Disk used (/): {disk['text']}",
        f"- Free disk space (/): {disk['free_gb']:.1f} GB" if disk["free_gb"] is not None else "- Free disk space (/): Unavailable",
        f"- CPU temp: {cpu_temp['text']}",
        f"- Voltage / throttling: {voltage['text']}",
        "",
        "### Reachability",
        f"- Internet check ({PING_TARGET}): {reachability['text']}",
        "",
        "### Weather",
    ]

    for report in weather_reports:
        lines.extend([
            f"- {report['city']}:",
            f"  - Condition: {report['condition']}",
            f"  - Max temp: {report['max_temp']}°C",
            f"  - Min temp: {report['min_temp']}°C",
            f"  - Feels like: {report['feels_like']}°C",
            f"  - Chance of rain: {report['chance_of_rain']}%",
            f"  - Warnings: {report['warnings']}",
        ])

    lines.extend([
        "",
        "### Health",
        f"- Overall: {health['overall']}",
    ])

    if health["warnings"]:
        for warning in health["warnings"]:
            lines.append(f"- Warning: {warning}")
    else:
        lines.append("- Warning: None")

    lines.append("")
    lines.append("")

    return "\n".join(lines)


def append_entry(filepath):
    entry = build_entry()
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(entry)


def main():
    force_run = "--force" in sys.argv

    now = datetime.datetime.now()

    if not force_run and now.weekday() in SKIP_WEEKDAYS:
        print("Skipping today because of weekday rule.")
        return

    if not force_run and random.random() < SKIP_CHANCE:
        print("Skipping today due to random skip chance.")
        return

    delay = 0 if force_run else random.randint(MIN_DELAY, MAX_DELAY)
    print(f"Sleeping for {delay} seconds...")
    time.sleep(delay)

    run(["git", "pull", "--rebase"])

    filepath, filename = ensure_month_file()

    if entry_for_today_exists(filepath):
        print("Today's entry already exists. Nothing to do.")
        return

    append_entry(filepath)

    run(["git", "add", filename])
    run(["git", "add", "history.json"], check=False)
    run(["git", "add", "cpu_temp.png"], check=False)
    run(["git", "add", "disk_usage.png"], check=False)

    diff = run(["git", "diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        print("No staged changes.")
        return

    commit_message = random.choice(COMMIT_MESSAGES)
    run(["git", "commit", "-m", commit_message])
    run(["git", "push"])

    print("Done.")


if __name__ == "__main__":
    main()
