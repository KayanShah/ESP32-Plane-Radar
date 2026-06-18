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
PRESETS_KM = [1.0, 5.0, 10.0, 25.0]
OUTER_KM   = [r * 4.0 / 3.0 for r in PRESETS_KM]

ADSB_API = "https://api.airplanes.live/v2/point/{lat:.6f}/{lon:.6f}/{nm_int}"

C_RUNWAY       = "#389632"
C_RUNWAY_LABEL = "#6ed2e6"

MAP_OUTER_KM = 5.0   # tight airport view: fills the whole window with ~5 km radius

# ---------------------------------------------------------------------------
# Aircraft silhouettes — PNG sprites, nose-up, transparent background.
# Loaded from simulator_assets/; PIL (Pillow) required.
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageTk
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulator_assets")


def _load_tinted_images():
    if not _PIL_OK:
        return {}
    r, g, b = int(C_AIRCRAFT[1:3], 16), int(C_AIRCRAFT[3:5], 16), int(C_AIRCRAFT[5:7], 16)
    result = {}
    for key, fname in (
        ('airliner_2', '2_Engine_Airliner.png'),
        ('airliner_4', '4_Engine.png'),
        ('private_jet', 'Private_Jet.png'),
        ('ga',          'General_Aviation.png'),
        ('helicopter',  'Helicopter.png'),
        ('military',    'Military.png'),
    ):
        path = os.path.join(_ASSETS_DIR, fname)
        if not os.path.exists(path):
            continue
        img = Image.open(path).convert("RGBA")
        _, _, _, alpha = img.split()
        colored = Image.new("RGBA", img.size, (r, g, b, 255))
        colored.putalpha(alpha)
        result[key] = colored
    return result


_TINTED_IMAGES = _load_tinted_images()
_RESIZED_CACHE: dict = {}  # (img_key, size) → PIL.Image


def _get_resized(img_key: str, size: int):
    key = (img_key, size)
    if key not in _RESIZED_CACHE:
        base = _TINTED_IMAGES.get(img_key)
        _RESIZED_CACHE[key] = base.resize((size, size), Image.LANCZOS) if base else None
    return _RESIZED_CACHE[key]


# category → (image_key, pixel_size on screen)
_CAT_IMAGE = {
    'narrow':      ('airliner_2', 32),
    'wide':        ('airliner_2', 40),
    'quad':        ('airliner_4', 44),
    'regional':    ('private_jet', 28),
    'private_jet': ('private_jet', 24),
    'turboprop':   ('ga', 26),
    'ga':          ('ga', 20),
    'helicopter':  ('helicopter', 22),
    'military':    ('military', 28),
}

_UNKNOWN_TYPES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unknown_types.txt")

def _load_known_unknowns() -> set:
    """Read type codes already logged in previous sessions."""
    if not os.path.exists(_UNKNOWN_TYPES_FILE):
        return set()
    with open(_UNKNOWN_TYPES_FILE) as f:
        return {line.strip().split()[0] for line in f if line.strip() and not line.startswith('#')}

_LOGGED_UNKNOWNS: set = _load_known_unknowns()

def _log_unknown_type(ac_type: str, callsign: str) -> None:
    """Append a newly seen unrecognised type code to unknown_types.txt."""
    code = ac_type.strip().upper()
    if not code or code in _LOGGED_UNKNOWNS:
        return
    _LOGGED_UNKNOWNS.add(code)
    with open(_UNKNOWN_TYPES_FILE, 'a') as f:
        label = f"  # {callsign}" if callsign else ""
        f.write(f"{code}{label}\n")


_TYPE_CAT = {
    # ── Quad widebody ──────────────────────────────────────────────────
    'A388':'quad', 'A389':'quad',
    'B741':'quad', 'B742':'quad', 'B743':'quad', 'B744':'quad', 'B748':'quad',
    'A342':'quad', 'A343':'quad', 'A345':'quad', 'A346':'quad',
    # ── Wide body twin ─────────────────────────────────────────────────
    'B762':'wide', 'B763':'wide', 'B764':'wide',
    'B752':'wide', 'B753':'wide', 'B703':'wide',
    'B772':'wide', 'B773':'wide', 'B77L':'wide', 'B77W':'wide',
    'B778':'wide', 'B779':'wide',
    'B788':'wide', 'B789':'wide', 'B78X':'wide',
    'A332':'wide', 'A333':'wide', 'A338':'wide', 'A339':'wide',
    'A359':'wide', 'A35K':'wide',
    # ── Narrow body ────────────────────────────────────────────────────
    'A318':'narrow', 'A319':'narrow', 'A320':'narrow', 'A321':'narrow',
    'A19N':'narrow', 'A20N':'narrow', 'A21N':'narrow',
    'B731':'narrow', 'B732':'narrow', 'B733':'narrow', 'B734':'narrow',
    'B735':'narrow', 'B736':'narrow', 'B737':'narrow', 'B738':'narrow', 'B739':'narrow',
    'B37M':'narrow', 'B38M':'narrow', 'B39M':'narrow',
    'E295':'narrow', 'BCS3':'narrow',
    # ── Regional jet ───────────────────────────────────────────────────
    'E170':'regional', 'E175':'regional', 'E190':'regional', 'E195':'regional',
    'E75L':'regional', 'E7W' :'regional',
    'B712':'regional',
    'CRJ2':'regional', 'CRJ7':'regional', 'CRJ9':'regional', 'CRJX':'regional',
    'E145':'regional',
    # ── Business / private jet ─────────────────────────────────────────
    'C525':'private_jet', 'C510':'private_jet', 'C56X':'private_jet',
    'C680':'private_jet', 'C68A':'private_jet', 'C700':'private_jet',
    'LJ45':'private_jet', 'LJ60':'private_jet', 'LJ75':'private_jet',
    'CL30':'private_jet', 'CL35':'private_jet', 'CL60':'private_jet',
    'GL5T':'private_jet', 'GLEX':'private_jet', 'G280':'private_jet',
    'GLF4':'private_jet', 'GLF5':'private_jet', 'GLF6':'private_jet',
    'F900':'private_jet', 'F2TH':'private_jet', 'FA7X':'private_jet', 'FA8X':'private_jet',
    'PC24':'private_jet', 'E55P':'private_jet', 'E50P':'private_jet', 'E35L':'private_jet',
    'BE40':'private_jet', 'BE4W':'private_jet', 'HA4T':'private_jet',
    'C25C':'private_jet', 'C550':'private_jet', 'PRM1':'private_jet', 'SF50':'private_jet',
    'E545':'private_jet', 'E550':'private_jet', 'GL7T':'private_jet', 'EA50':'private_jet',
    # ── Turboprop ──────────────────────────────────────────────────────
    'AT43':'turboprop', 'AT45':'turboprop', 'AT72':'turboprop',
    'AT75':'turboprop', 'AT76':'turboprop',
    'DH8A':'turboprop', 'DH8B':'turboprop', 'DH8C':'turboprop', 'DH8D':'turboprop',
    'SF34':'turboprop', 'BE20':'turboprop',
    'P180':'turboprop', 'C130':'turboprop', 'C160':'turboprop', 'A400':'turboprop',
    'B190':'turboprop', 'C30J':'turboprop',
    # ── GA / light piston ──────────────────────────────────────────────
    'C150':'ga', 'C152':'ga', 'C172':'ga', 'C182':'ga', 'C208':'ga',
    'PA28':'ga', 'PA31':'ga', 'PA32':'ga', 'PA34':'ga', 'PA38':'ga', 'PA44':'ga', 'PA46':'ga',
    'P32R':'ga',
    'M20P':'ga', 'C72R':'ga', 'C340':'ga', 'C414':'ga',
    'BE58':'ga', 'DHC2':'ga',
    'SR20':'ga', 'SR22':'ga', 'S22T':'ga', 'ECHO':'ga',
    'DA40':'ga', 'DA42':'ga',
    'P28A':'ga', 'P28B':'ga',
    'BE9L':'ga', 'BE36':'ga', 'GA7C':'ga',
    'TBM8':'ga', 'TBM9':'ga', 'PC12':'ga',
    'RV6':'ga', 'TWEN':'ga', 'EV97':'ga', 'SKRA':'ga', 'ULAC':'ga', 'G2CA':'ga',
    'MCR1':'ga', 'BT36':'ga', 'T6':'ga', 'BE76':'ga',
    # ── Helicopter ─────────────────────────────────────────────────────
    'EC35':'helicopter', 'EC45':'helicopter', 'H135':'helicopter', 'H145':'helicopter',
    'EC20':'helicopter', 'EC30':'helicopter',
    'B06' :'helicopter', 'B407':'helicopter', 'B412':'helicopter', 'B429':'helicopter',
    'S61' :'helicopter', 'S70' :'helicopter', 'S76' :'helicopter', 'S92' :'helicopter',
    'R22' :'helicopter', 'R44' :'helicopter', 'R66' :'helicopter',
    'MD52':'helicopter', 'MD53':'helicopter',
    'AW09':'helicopter', 'AW19':'helicopter', 'AW13':'helicopter', 'AW16':'helicopter',
    'AS32':'helicopter', 'AS50':'helicopter', 'AS55':'helicopter', 'AS65':'helicopter',
    'BK17':'helicopter', 'NH90':'helicopter',
    'H500':'helicopter', 'B505':'helicopter', 'H60':'helicopter', 'K100':'helicopter',
    'A109':'helicopter', 'A139':'helicopter',
    # ── Military ───────────────────────────────────────────────────────
    'F15' :'military', 'F16' :'military', 'F18' :'military',
    'F22' :'military', 'F35' :'military', 'F117':'military',
    'EUFI':'military', 'TYFN':'military',
    'A10' :'military', 'SU27':'military', 'SU30':'military',
    'MIG2':'military', 'MIG3':'military',
    'B1'  :'military', 'B2'  :'military', 'B52' :'military',
    'C17' :'military',
}


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


def lat_lon_to_screen(lat, lon, center_lat, center_lon, outer_km, radius_px=GRID_R):
    px_per_km = radius_px / outer_km
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
    url = ADSB_API.format(lat=center_lat, lon=center_lon, nm_int=max(1, int(dist_nm + 0.5)))
    req = urllib.request.Request(url, headers={"User-Agent": "PlaneRadarSim/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    planes = []
    for p in data.get("ac", []):
        if p.get("lat") is None or p.get("lon") is None:
            continue
        on_ground = False
        if p.get("alt_baro") == "ground":
            continue

        nose  = _f(p.get("true_heading") or p.get("mag_heading") or
                   p.get("track") or p.get("dir"))
        track = _f(p.get("track") or p.get("true_heading") or
                   p.get("mag_heading") or p.get("dir"))
        gs    = _f(p.get("gs") or p.get("tas") or p.get("ias"))

        callsign = (p.get("flight") or p.get("hex") or "").strip()[:8]
        ac_type  = (p.get("t") or "").strip()[:4]

        if on_ground:
            alt = "GND"
        else:
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
            on_ground=on_ground,
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
        root.bind("<m>", self._toggle_map)
        root.bind("<M>", self._toggle_map)
        self._image_refs: list = []  # keep PhotoImage refs alive
        self.map_mode = False
        self._cur_outer_km  = self._outer_km()
        self._cur_radius_px = GRID_R

        self._draw()
        self._schedule_fetch()

    # --- event handlers ---

    def _on_click(self, _event):
        if self.map_mode:
            return
        self.range_idx = (self.range_idx + 1) % len(PRESETS_KM)
        self._draw()
        self._schedule_fetch()

    def _toggle_map(self, _event=None):
        self.map_mode = not self.map_mode
        self._draw()

    # --- range helpers ---

    def _outer_km(self):  return OUTER_KM[self.range_idx]
    def _ring3_km(self):  return PRESETS_KM[self.range_idx]

    def _inner_max_km(self):
        return self._outer_km() * (GRID_R - INSIDE_INSET) / GRID_R

    # --- drawing ---

    def _draw(self):
        self._image_refs = []
        c = self.canvas
        c.delete("all")

        if self.map_mode:
            self._cur_outer_km  = MAP_OUTER_KM
            self._cur_radius_px = CX
        else:
            self._cur_outer_km  = self._outer_km()
            self._cur_radius_px = GRID_R

        if self.map_mode:
            # Full-screen flat map: black background, no rings or compass
            c.create_rectangle(0, 0, SIZE, SIZE, fill=C_BG, outline="")
            self._draw_runways()
            for p in self.aircraft:
                x, y = lat_lon_to_screen(p["lat"], p["lon"], self.lat, self.lon,
                                         self._cur_outer_km, self._cur_radius_px)
                if -60 <= x <= SIZE + 60 and -60 <= y <= SIZE + 60:
                    self._draw_plane(p)
            r = CENTER_DOT_R
            c.create_oval(CX - r, CY - r, CX + r, CY + r, fill=C_CENTER, outline="")
            c.create_text(SIZE - 4, SIZE - 4, text="M=radar", fill="#333355",
                          font=("Arial", 8), anchor="se")
            return

        # --- radar mode ---
        disp_r = CX
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
                      text="M=map  click=range", fill="#333355",
                      font=("Arial", 8), anchor="se")

    def _draw_runways(self):
        if not AIRPORTS:
            return
        c = self.canvas
        outer_km   = self._cur_outer_km
        radius_px  = self._cur_radius_px
        fetch_r    = outer_km * 1.3

        labeled = set()
        for ap_idx, le_lat, le_lon, he_lat, he_lon in RUNWAYS:
            if ap_idx >= len(AIRPORTS):
                continue
            ident, ap_lat, ap_lon = AIRPORTS[ap_idx]
            if ident not in ("EGLL", "EGCC"):
                continue
            if dist_km(ap_lat, ap_lon, self.lat, self.lon) > fetch_r:
                continue

            x0, y0 = lat_lon_to_screen(le_lat, le_lon, self.lat, self.lon, outer_km, radius_px)
            x1, y1 = lat_lon_to_screen(he_lat, he_lon, self.lat, self.lon, outer_km, radius_px)

            if self.map_mode:
                c.create_line(x0, y0, x1, y1, fill=C_RUNWAY, width=4)
            else:
                # Skip if both endpoints are outside the ring
                if (dist_sq_from_center(x0, y0) > GRID_R ** 2 and
                        dist_sq_from_center(x1, y1) > GRID_R ** 2):
                    if not self._segment_crosses_ring(x0, y0, x1, y1):
                        continue
                x1c, y1c = clip_to_ring(x0, y0, x1, y1)
                x0c, y0c = clip_to_ring(x1, y1, x0, y0)
                c.create_line(x0c, y0c, x1c, y1c, fill=C_RUNWAY, width=3)

            if ap_idx not in labeled:
                labeled.add(ap_idx)
                ax, ay = lat_lon_to_screen(ap_lat, ap_lon, self.lat, self.lon, outer_km, radius_px)
                if not self.map_mode:
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
        x, y = lat_lon_to_screen(p["lat"], p["lon"], self.lat, self.lon,
                                  self._cur_outer_km, self._cur_radius_px)
        heading = p["nose_deg"]
        rad = math.radians(heading)
        sin_h, cos_h = math.sin(rad), math.cos(rad)
        on_ground = p.get("on_ground", False)
        plane_color = "#666666" if on_ground else C_AIRCRAFT

        # Resolve PNG sprite for this aircraft type
        cat = _TYPE_CAT.get((p["type"] or '').strip().upper())
        img_info = _CAT_IMAGE.get(cat) if cat else None
        sprite_img = None
        sprite_size = 0
        if img_info and _PIL_OK and not on_ground:
            img_key, sprite_size = img_info
            sprite_img = _get_resized(img_key, sprite_size)

        # Speed vector — only for airborne aircraft
        nose_r = (sprite_size // 2 - 2) if sprite_img else NOSE_LEN
        if not on_ground:
            ln = speed_line_px(p["gs_knots"])
            if ln > 0:
                tx = x + int(sin_h * nose_r)
                ty = y - int(cos_h * nose_r)
                r2 = math.radians(p["track_deg"])
                ex = tx + int(math.sin(r2) * ln)
                ey = ty - int(math.cos(r2) * ln)
                ex, ey = clip_to_ring(tx, ty, ex, ey)
                if (ex, ey) != (tx, ty):
                    c.create_line(tx, ty, ex, ey, fill=C_TRACK, width=2)

        if sprite_img is not None:
            rotated = sprite_img.rotate(-heading, resample=Image.BICUBIC, expand=False)
            tk_img = ImageTk.PhotoImage(rotated)
            self._image_refs.append(tk_img)
            c.create_image(x, y, image=tk_img, anchor='center')
        else:
            if not on_ground:
                _log_unknown_type(p["type"], p["callsign"])
            tip_x = int(x + sin_h * NOSE_LEN)
            tip_y = int(y - cos_h * NOSE_LEN)
            bx = int(x - sin_h * TAIL_LEN)
            by = int(y + cos_h * TAIL_LEN)
            wx, wy = int(cos_h * TAIL_HALF), int(sin_h * TAIL_HALF)
            c.create_polygon(tip_x, tip_y,
                             bx + wx, by + wy,
                             bx - wx, by - wy,
                             fill=plane_color, outline="")

        self._draw_tag(x, y, p["callsign"], p["type"], p["alt"], p["gs_knots"],
                       on_ground=on_ground)

    def _draw_tag(self, x, y, callsign, ac_type, alt, gs_knots, on_ground=False):
        c = self.canvas
        tf = ("Arial", 9, "bold")
        line_h = 11
        spd = f"{int(round(gs_knots))} kt" if gs_knots > 0 else ""
        if on_ground:
            lines = [(callsign, "#999999"), (ac_type, "#777777"), (alt, "#777777")]
        else:
            lines = [(callsign, C_LABEL), (ac_type, C_TAG_TYPE), (alt, C_TAG_ALT), (spd, C_LABEL)]
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
