#!/usr/bin/env python3
"""
ESP32 Plane Radar — Desktop Simulator
Fetches live ADS-B data from opendata.adsb.fi and renders a round radar
display that mirrors what the ESP32 / GC9A01 hardware shows.

Usage:
  python3 simulator.py [lat] [lon] [range_index]
  python3 simulator.py 51.5074 -0.1278   # London
  python3 simulator.py 40.7128 -74.0060  # New York
  python3 simulator.py                   # default: Amsterdam

Click the display to cycle range presets (5 / 10 / 15 / 25 km).
"""

import json
import math
import os
import re
import sys
import tkinter as tk
import urllib.request

# ---------------------------------------------------------------------------
# Layout — hardware is 240×240; we render at 2× for comfort on a laptop.
# ---------------------------------------------------------------------------
SCALE = 2

# Mirror of radar_theme.h
SIZE          = 240 * SCALE
CX            = SIZE // 2
CY            = SIZE // 2
GRID_R        = 107 * SCALE          # outer ring radius (px)
RING_COUNT    = 4
CENTER_DOT_R  = 2  * SCALE
NOSE_LEN      = 8  * SCALE
TAIL_LEN      = 3  * SCALE
TAIL_HALF     = 4  * SCALE
BEYOND_DOT_R  = 4  * SCALE
BEYOND_MARGIN = 2  * SCALE
INSIDE_INSET  = (NOSE_LEN + TAIL_HALF + 1)
LABEL_GAP     = 1  * SCALE
SCALE_GAP     = 6  * SCALE

TRACK_HORIZON_SEC    = 60.0
TRACK_REF_OUTER_KM   = 13.3
TRACK_LENGTH_SCALE   = 1.5 / 5.0
SPEED_LINE_MIN_PX    = 2 * SCALE

# Mirror of radar_theme.h color constants (RGB → hex)
C_BG       = "#040a1c"
C_GRID     = "#106420"
C_LABEL    = "#ffffff"
C_CENTER   = "#ffffff"
C_AIRCRAFT = "#ff0000"
C_TRACK    = "#ff00ff"
C_TAG_TYPE = "#ffc800"
C_TAG_ALT  = "#5ac8ff"

# Mirror of radar_range.h presets (ring3_km → outer_km = ring3 × 4/3)
PRESETS_KM = [5.0, 10.0, 15.0, 25.0]
OUTER_KM   = [r * 4.0 / 3.0 for r in PRESETS_KM]

ADSB_API = "https://opendata.adsb.fi/api/v3/lat/{lat:.6f}/lon/{lon:.6f}/dist/{nm:.1f}"

C_RUNWAY       = "#389632"  # teal-ish (56, 150, 170) → using a green that pops on dark bg
C_RUNWAY_LABEL = "#6ed2e6"  # lighter teal (110, 210, 230)

# ---------------------------------------------------------------------------
# Parse airport + runway data from the C++ source (no extra files needed)
# ---------------------------------------------------------------------------

def _load_airports_runways():
    data_file = os.path.join(os.path.dirname(__file__),
                             "src", "data", "large_airports_data.cpp")
    if not os.path.exists(data_file):
        return [], []
    with open(data_file) as f:
        content = f.read()
    airports = re.findall(r'\{"([A-Z0-9]+)",\s*(-?\d+),\s*(-?\d+)\}', content)
    airports = [(ident, int(lat_e7) * 1e-7, int(lon_e7) * 1e-7)
                for ident, lat_e7, lon_e7 in airports]
    runways = re.findall(
        r'\{(\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(\d+)\}', content)
    runways = [(int(ap_idx),
                int(le_lat) * 1e-7, int(le_lon) * 1e-7,
                int(he_lat) * 1e-7, int(he_lon) * 1e-7)
               for ap_idx, le_lat, le_lon, he_lat, he_lon, _ in runways]
    return airports, runways

AIRPORTS, RUNWAYS = _load_airports_runways()


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def dist_sq_from_center(x, y):
    return (x - CX) ** 2 + (y - CY) ** 2


def lat_lon_to_screen(lat, lon, center_lat, center_lon, outer_km):
    px_per_km = GRID_R / outer_km
    cos_lat = math.cos(math.radians((lat + center_lat) * 0.5))
    dx_km = (lon - center_lon) * 111.0 * cos_lat
    dy_km = (lat - center_lat) * 111.0
    return (int(CX + dx_km * px_per_km),
            int(CY - dy_km * px_per_km))


def dist_km(lat, lon, center_lat, center_lon):
    cos_lat = math.cos(math.radians((lat + center_lat) * 0.5))
    dx = (lon - center_lon) * 111.0 * cos_lat
    dy = (lat - center_lat) * 111.0
    return math.sqrt(dx * dx + dy * dy)


def nose_tip(cx, cy, heading_deg):
    r = math.radians(heading_deg)
    return (cx + int(math.sin(r) * NOSE_LEN),
            cy - int(math.cos(r) * NOSE_LEN))


def clip_to_ring(x0, y0, x1, y1):
    dx, dy = x1 - x0, y1 - y0
    t = 1.0
    for _ in range(20):
        px = x0 + int(dx * t)
        py = y0 + int(dy * t)
        if dist_sq_from_center(px, py) <= GRID_R ** 2:
            return px, py
        t -= 0.05
        if t <= 0:
            return x0, y0
    return x0, y0


def speed_line_px(gs_knots):
    if gs_knots <= 0:
        return 0
    km_per_knot = 1.852 * TRACK_HORIZON_SEC / 3600.0
    px = gs_knots * km_per_knot * GRID_R / TRACK_REF_OUTER_KM * TRACK_LENGTH_SCALE
    return max(int(px + 0.5), SPEED_LINE_MIN_PX)


def _f(val, fallback=0.0):
    """Return float(val) if val is not None, else fallback."""
    return float(val) if val is not None else fallback


# ---------------------------------------------------------------------------
# ADS-B fetch (uses stdlib urllib only)
# ---------------------------------------------------------------------------

def fetch_aircraft(center_lat, center_lon, outer_km_val):
    dist_nm = outer_km_val / 1.852
    url = ADSB_API.format(lat=center_lat, lon=center_lon, nm=dist_nm)
    req = urllib.request.Request(url, headers={"User-Agent": "PlaneRadarSim/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    planes = []
    for p in data.get("ac", []):
        if p.get("lat") is None or p.get("lon") is None:
            continue
        if p.get("alt_baro") == "ground":
            continue

        nose  = _f(p.get("true_heading") or p.get("mag_heading") or
                   p.get("track") or p.get("dir"))
        track = _f(p.get("track") or p.get("true_heading") or
                   p.get("mag_heading") or p.get("dir"))
        gs    = _f(p.get("gs") or p.get("tas") or p.get("ias"))

        callsign = (p.get("flight") or p.get("hex") or "").strip()[:8]
        ac_type  = (p.get("t") or "").strip()[:4]

        alt = ""
        ab = p.get("alt_baro")
        ag = p.get("alt_geom")
        if isinstance(ab, (int, float)):
            alt = f"{int(round(ab))} ft"
        elif isinstance(ag, (int, float)):
            alt = f"{int(round(ag))} ft"

        planes.append(dict(
            lat=float(p["lat"]), lon=float(p["lon"]),
            nose_deg=nose, track_deg=track, gs_knots=gs,
            callsign=callsign, type=ac_type, alt=alt,
        ))
    return planes


# ---------------------------------------------------------------------------
# Radar display (tkinter Canvas)
# ---------------------------------------------------------------------------

class RadarSim:
    def __init__(self, root, center_lat, center_lon, range_idx=1):
        self.root       = root
        self.lat        = center_lat
        self.lon        = center_lon
        self.range_idx  = range_idx % len(PRESETS_KM)
        self.aircraft   = []

        root.title("Plane Radar Simulator")
        root.configure(bg="black")
        root.resizable(False, False)

        self.canvas = tk.Canvas(root, width=SIZE, height=SIZE,
                                bg="black", highlightthickness=0)
        self.canvas.pack()

        self.status_var = tk.StringVar(value="Connecting to adsb.fi …")
        tk.Label(root, textvariable=self.status_var,
                 fg="#555577", bg="black",
                 font=("monospace", 10)).pack(pady=2)

        self.canvas.bind("<Button-1>", self._on_click)

        self._draw()
        self._schedule_fetch()

    # --- event handlers ---

    def _on_click(self, _event):
        self.range_idx = (self.range_idx + 1) % len(PRESETS_KM)
        self._draw()
        self._schedule_fetch()

    # --- range helpers ---

    def _outer_km(self):  return OUTER_KM[self.range_idx]
    def _ring3_km(self):  return PRESETS_KM[self.range_idx]

    def _inner_max_km(self):
        return self._outer_km() * (GRID_R - INSIDE_INSET) / GRID_R

    # --- drawing ---

    def _draw(self):
        c = self.canvas
        c.delete("all")

        # Background: full black, then navy circle (the display face)
        disp_r = CX  # half of SIZE
        c.create_oval(CX - disp_r, CY - disp_r, CX + disp_r, CY + disp_r,
                      fill=C_BG, outline="")

        # Grid rings
        for i in range(1, RING_COUNT + 1):
            r = (GRID_R * i) // RING_COUNT
            c.create_oval(CX - r, CY - r, CX + r, CY + r,
                          outline=C_GRID, width=2)

        # Crosshairs
        c.create_line(CX, CY - GRID_R, CX, CY + GRID_R, fill=C_GRID, width=2)
        c.create_line(CX - GRID_R, CY, CX + GRID_R, CY, fill=C_GRID, width=2)

        # Cardinal labels
        lf = ("Arial", 14, "bold")
        c.create_text(CX, 4,        text="N", fill=C_LABEL, font=lf, anchor="n")
        c.create_text(CX, SIZE - 4, text="S", fill=C_LABEL, font=lf, anchor="s")
        c.create_text(4,  CY,       text="W", fill=C_LABEL, font=lf, anchor="w")
        c.create_text(SIZE - 4, CY, text="E", fill=C_LABEL, font=lf, anchor="e")

        # Scale label (east side of the outermost ring)
        ring3 = self._ring3_km()
        scale_txt = f"{int(ring3)} km"
        scale_x = CX + GRID_R - SCALE_GAP
        c.create_text(scale_x, CY, text=scale_txt,
                      fill=C_GRID, font=("Arial", 10, "bold"), anchor="e")

        # Runways (drawn before aircraft, same as firmware)
        self._draw_runways()

        # Aircraft
        inner_km = self._inner_max_km()
        for p in self.aircraft:
            d = dist_km(p["lat"], p["lon"], self.lat, self.lon)
            if d <= inner_km:
                self._draw_plane(p)
            else:
                self._draw_beyond_dot(p)

        # Center dot (on top of everything)
        r = CENTER_DOT_R
        c.create_oval(CX - r, CY - r, CX + r, CY + r,
                      fill=C_CENTER, outline="")

        # Hint
        c.create_text(SIZE - 4, SIZE - 4,
                      text="click = range", fill="#333355",
                      font=("Arial", 8), anchor="se")

    def _draw_runways(self):
        if not AIRPORTS:
            return
        c = self.canvas
        outer_km = self._outer_km()
        fetch_r = outer_km * 1.3  # match firmware: fetchRadiusKm() > outer_km

        labeled = set()
        for ap_idx, le_lat, le_lon, he_lat, he_lon in RUNWAYS:
            if ap_idx >= len(AIRPORTS):
                continue
            ident, ap_lat, ap_lon = AIRPORTS[ap_idx]
            if dist_km(ap_lat, ap_lon, self.lat, self.lon) > fetch_r:
                continue

            x0, y0 = lat_lon_to_screen(le_lat, le_lon, self.lat, self.lon, outer_km)
            x1, y1 = lat_lon_to_screen(he_lat, he_lon, self.lat, self.lon, outer_km)

            # Skip if both endpoints are outside the ring
            if (dist_sq_from_center(x0, y0) > GRID_R ** 2 and
                    dist_sq_from_center(x1, y1) > GRID_R ** 2):
                # Quick check: might still cross the ring
                if not self._segment_crosses_ring(x0, y0, x1, y1):
                    continue

            # Clip endpoints to ring
            x1c, y1c = clip_to_ring(x0, y0, x1, y1)
            x0c, y0c = clip_to_ring(x1, y1, x0, y0)
            c.create_line(x0c, y0c, x1c, y1c, fill=C_RUNWAY, width=3)

            if ap_idx not in labeled:
                labeled.add(ap_idx)
                ax, ay = lat_lon_to_screen(ap_lat, ap_lon, self.lat, self.lon, outer_km)
                ax, ay = self._clamp_to_ring(ax, ay)
                dx, dy = ax - CX, ay - CY
                ln = math.sqrt(dx*dx + dy*dy) or 1
                gap = 6 * SCALE
                lx = int(ax + dx / ln * gap)
                ly = int(ay + dy / ln * gap)
                c.create_text(lx, ly, text=ident, fill=C_RUNWAY_LABEL,
                              font=("Arial", 8, "bold"), anchor="center")

    def _clamp_to_ring(self, x, y):
        dx, dy = x - CX, y - CY
        d = math.sqrt(dx*dx + dy*dy) or 1
        if d <= GRID_R:
            return x, y
        return int(CX + dx / d * GRID_R), int(CY + dy / d * GRID_R)

    def _segment_crosses_ring(self, x0, y0, x1, y1):
        """Return True if the line segment passes through the outer ring disc."""
        r = GRID_R
        dx, dy = x1 - x0, y1 - y0
        fx, fy = x0 - CX, y0 - CY
        a = dx*dx + dy*dy
        if a == 0:
            return False
        b = 2 * (fx*dx + fy*dy)
        c_val = fx*fx + fy*fy - r*r
        disc = b*b - 4*a*c_val
        if disc < 0:
            return False
        sq = math.sqrt(disc)
        t0 = (-b - sq) / (2*a)
        t1 = (-b + sq) / (2*a)
        return (0 <= t0 <= 1) or (0 <= t1 <= 1)

    def _draw_plane(self, p):
        c = self.canvas
        outer_km = self._outer_km()
        x, y = lat_lon_to_screen(p["lat"], p["lon"], self.lat, self.lon, outer_km)

        # Speed vector (track direction, magenta)
        ln = speed_line_px(p["gs_knots"])
        if ln > 0:
            tx, ty = nose_tip(x, y, p["nose_deg"])
            r = math.radians(p["track_deg"])
            ex = tx + int(math.sin(r) * ln)
            ey = ty - int(math.cos(r) * ln)
            ex, ey = clip_to_ring(tx, ty, ex, ey)
            if (ex, ey) != (tx, ty):
                c.create_line(tx, ty, ex, ey, fill=C_TRACK, width=2)

        # Aircraft triangle (heading, red)
        heading = p["nose_deg"]
        rad = math.radians(heading)
        s, co = math.sin(rad), math.cos(rad)
        tip_x, tip_y = int(x + s * NOSE_LEN), int(y - co * NOSE_LEN)
        bx = int(x - s * TAIL_LEN)
        by = int(y + co * TAIL_LEN)
        wx, wy = int(co * TAIL_HALF), int(s * TAIL_HALF)
        c.create_polygon(tip_x, tip_y,
                         bx + wx, by + wy,
                         bx - wx, by - wy,
                         fill=C_AIRCRAFT, outline="")

        # Tag (callsign / type / altitude / speed)
        self._draw_tag(x, y, p["callsign"], p["type"], p["alt"], p["gs_knots"])

    def _draw_tag(self, x, y, callsign, ac_type, alt, gs_knots):
        c = self.canvas
        tf = ("Arial", 9, "bold")
        line_h = 11
        spd = f"{int(round(gs_knots))} kt" if gs_knots > 0 else ""
        lines  = [(callsign, C_LABEL), (ac_type, C_TAG_TYPE), (alt, C_TAG_ALT), (spd, C_LABEL)]
        lines  = [(t, col) for t, col in lines if t]

        block_h = line_h * len(lines)
        ly = y - block_h // 2
        symbol_half = NOSE_LEN + TAIL_HALF

        for i, (text, color) in enumerate(lines):
            if x < CX:
                ax = min(x + symbol_half + LABEL_GAP, SIZE - 2)
                c.create_text(ax, ly + i * line_h, text=text,
                              fill=color, font=tf, anchor="nw")
            else:
                ax = max(x - symbol_half - LABEL_GAP, 2)
                c.create_text(ax, ly + i * line_h, text=text,
                              fill=color, font=tf, anchor="ne")

    def _draw_beyond_dot(self, p):
        dx_km = (p["lon"] - self.lon) * 111.0
        dy_km = (p["lat"] - self.lat) * 111.0
        angle = math.atan2(dx_km, dy_km)
        rim   = CX - BEYOND_MARGIN
        dot_x = int(CX + math.sin(angle) * rim)
        dot_y = int(CY - math.cos(angle) * rim)
        r = BEYOND_DOT_R
        self.canvas.create_oval(dot_x - r, dot_y - r, dot_x + r, dot_y + r,
                                fill=C_AIRCRAFT, outline="")

    # --- data refresh ---

    def _schedule_fetch(self):
        self.root.after(0, self._do_fetch)

    def _do_fetch(self):
        try:
            planes = fetch_aircraft(self.lat, self.lon, self._outer_km())
            self.aircraft = planes
            n = len(planes)
            self.status_var.set(
                f"{n} aircraft  |  lat={self.lat:.4f}  lon={self.lon:.4f}"
                f"  range={int(self._ring3_km())} km"
            )
        except Exception as e:
            self.status_var.set(f"Fetch error: {e}")
        self._draw()
        self.root.after(3000, self._do_fetch)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 52.3676  # Amsterdam
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else 4.9041
    idx = int(sys.argv[3])   if len(sys.argv) > 3 else 1        # default 10 km

    root = tk.Tk()
    RadarSim(root, lat, lon, idx)
    root.mainloop()


if __name__ == "__main__":
    main()
