import app
import math
import random
import json

from events.input import Buttons, BUTTON_TYPES
from tildagonos import tildagonos
from system.eventbus import eventbus
from system.patterndisplay.events import PatternDisable, PatternEnable

# ----------------------------------------------------------------------------
# Tildagon Lander - a lunar-lander style game for the EMF Tildagon badge.
# Fly the lander down through the rocks and touch down gently on a flat pad.
# Land slow, level, and on the pad to score. Crash and you lose a life.
#
# Controls:  LEFT / RIGHT = rotate,  UP (or CONFIRM) = main thruster,
#            CANCEL = back to title / exit.
# ----------------------------------------------------------------------------

# Display ------------------------------------------------------------------
R_SCREEN = 120.0          # the round display has radius ~120 from centre
SAFE_R   = 118.0          # keep gameplay objects inside this radius

# Physics (pixels, milliseconds) ------------------------------------------
GRAVITY_BASE = 0.0000085  # downward accel, px/ms^2
THRUST       = 0.000070   # main engine accel, px/ms^2 (~8x gravity)
ROT_SPEED    = 0.0022     # rotation rate, rad/ms (~0.7s for 90 degrees)
MAX_VY       = 0.13       # terminal-ish clamp so nothing tunnels

# Fuel ---------------------------------------------------------------------
FUEL_MAX    = 333.0       # tank cut by ~1/3 (was 500)
THRUST_BURN = 0.44        # fuel per ms while thrusting (doubled)
ROT_BURN    = 0.10        # fuel per ms while rotating (doubled)

# Safe-landing thresholds --------------------------------------------------
VY_SAFE  = 0.045          # max descent speed
VX_SAFE  = 0.032          # max sideways speed
ANG_SAFE = 0.20           # max tilt from vertical (radians, ~11 deg)

START_LIVES = 3
SPEED_DISP  = 1000.0      # multiplier for on-screen speed numbers

TWO_PI = 6.28318


class TildaLander(app.App):
    def __init__(self):
        self.button_states = Buttons(self)
        self.state = 'title'
        self.t = 0.0
        self.high_score = self._load_high_score()
        self.last_score = 0
        self.score = 0
        # decorative space (generated once, persists across levels)
        self.stars = []
        self.planets = []
        self._gen_space()
        # title animation
        self.led_phase = 0.0
        # previous-frame states for confirm/cancel rising-edge detection
        self._pc = False
        self._cc = False
        eventbus.emit(PatternDisable())

    # -- persistence -------------------------------------------------------
    def _load_high_score(self):
        try:
            with open('tildalander_save.json', 'r') as f:
                return json.load(f).get('high_score', 0)
        except Exception:
            return 0

    def _save_high_score(self, score):
        try:
            with open('tildalander_save.json', 'w') as f:
                json.dump({'high_score': score}, f)
        except Exception:
            pass

    # -- helpers -----------------------------------------------------------
    def _halfwidth(self, y):
        # horizontal half-width of the visible circle at height y
        d = SAFE_R * SAFE_R - y * y
        if d <= 0:
            return 0.0
        return math.sqrt(d)

    def _norm_angle(self):
        a = (self.angle + math.pi) % TWO_PI - math.pi
        return a

    # -- world generation --------------------------------------------------
    def _gen_space(self):
        self.stars = []
        for _ in range(34):
            # rejection-sample a point inside the circle
            for _try in range(6):
                x = random.uniform(-112.0, 112.0)
                y = random.uniform(-115.0, 60.0)
                if x * x + y * y < 110.0 * 110.0:
                    break
            self.stars.append({
                'x': x, 'y': y,
                'b': random.uniform(0.3, 0.9),
                'tw': random.random() < 0.35,
                'ph': random.uniform(0.0, TWO_PI),
            })
        # one banded gas giant + a small moon, up and to the sides
        self.planets = [
            {'x': -52.0, 'y': -60.0, 'r': 30.0,
             'col': (0.80, 0.45, 0.18), 'dark': (0.55, 0.28, 0.10),
             'bands': [-14.0, -4.0, 7.0, 16.0], 'ring': True},
            {'x': 64.0, 'y': -78.0, 'r': 11.0,
             'col': (0.62, 0.64, 0.70), 'dark': (0.40, 0.42, 0.48),
             'bands': [], 'ring': False},
        ]

    def _gen_level(self, level):
        self.level = level
        self.gravity = GRAVITY_BASE * (1.0 + 0.06 * (level - 1))
        self.level_fuel = max(187.0, FUEL_MAX - 20.0 * (level - 1))
        self.drift = min(0.05, 0.012 + 0.006 * (level - 1))

        step = 10.0
        # --- base rocky height map on a uniform grid ---
        gx = []
        x = -130.0
        while x <= 130.0:
            gx.append(x)
            x += step
        n = len(gx)
        rough = min(22.0, 9.0 + 2.0 * (level - 1))
        gh = [0.0] * n
        h = random.uniform(55.0, 80.0)
        for i in range(n):
            h += random.uniform(-rough, rough)
            if h < 38.0:
                h = 38.0 + random.uniform(0.0, 8.0)
            if h > 95.0:
                h = 95.0 - random.uniform(0.0, 8.0)
            top = (-self._halfwidth(gx[i]) + 4.0) if abs(gx[i]) < SAFE_R else 95.0
            if h < top:
                h = top
            gh[i] = h

        def base_height_at(px):
            if px <= gx[0]:
                return gh[0]
            if px >= gx[-1]:
                return gh[-1]
            i = int((px - gx[0]) / step)
            f = (px - gx[i]) / step
            return gh[i] + (gh[i + 1] - gh[i]) * f

        # --- choose landing pads (wider pads are easier, worth less) ---
        # half-widths chosen so even the hardest pad clears the lander stance
        widths = [28.0, 23.0, 20.0]          # half-widths for mult 1, 2, 3
        mults = [1, 2, 3]
        shrink = min(2.0, 0.5 * (level - 1))  # only a touch narrower per level
        candidates = [-50.0, -25.0, 0.0, 25.0, 50.0]
        # MicroPython's random has no shuffle(); do Fisher-Yates by hand
        for i in range(len(candidates) - 1, 0, -1):
            j = random.randint(0, i)
            candidates[i], candidates[j] = candidates[j], candidates[i]
        n_pads = 3 if level <= 2 else (2 if level <= 5 else 1)

        pads = []
        for cx in candidates:
            if len(pads) >= n_pads:
                break
            idx = len(pads)                   # placement order -> mult 1,2,3
            mult = mults[idx]
            half = max(19.0, widths[idx] - shrink)
            x0 = cx - half
            x1 = cx + half
            pad_y = base_height_at(cx)
            if pad_y < 50.0:
                pad_y = 50.0
            if pad_y > 84.0:
                pad_y = 84.0
            # the whole pad must be inside the round screen
            vis = self._halfwidth(pad_y) - 4.0
            if abs(x0) > vis or abs(x1) > vis:
                continue
            # must not overlap or crowd a pad already placed (8px gap)
            crowd = False
            for p in pads:
                if not (x1 < p['x0'] - 8.0 or x0 > p['x1'] + 8.0):
                    crowd = True
                    break
            if crowd:
                continue
            pads.append({'x0': x0, 'x1': x1, 'y': pad_y, 'mult': mult,
                         'cx': cx, 'half': half})

        # guarantee at least one pad
        if not pads:
            cx = 0.0
            pad_y = min(82.0, max(52.0, base_height_at(cx)))
            pads.append({'x0': cx - 26.0, 'x1': cx + 26.0, 'y': pad_y,
                         'mult': 1, 'cx': cx, 'half': 26.0})

        # --- build the final (non-uniform) terrain polyline ---
        # start from rocky grid points, flatten pad spans, insert exact edges
        terrain = []
        for i in range(n):
            terrain.append([gx[i], gh[i]])
        for p in pads:
            for pt in terrain:
                if p['x0'] <= pt[0] <= p['x1']:
                    pt[1] = p['y']
            terrain.append([p['x0'], p['y']])
            terrain.append([p['x1'], p['y']])
        terrain.sort(key=lambda pt: pt[0])
        # drop points that fall just inside a pad span but off its flat height
        cleaned = []
        for pt in terrain:
            pad = self._pad_for(pads, pt[0])
            if pad is not None and abs(pt[1] - pad['y']) > 0.01 \
                    and pad['x0'] + 0.01 < pt[0] < pad['x1'] - 0.01:
                continue
            cleaned.append(pt)
        self.terrain = cleaned
        self.pads = pads

    @staticmethod
    def _pad_for(pads, x):
        for p in pads:
            if p['x0'] <= x <= p['x1']:
                return p
        return None

    def _spawn_ship(self):
        self.x = random.uniform(-25.0, 25.0)
        self.y = -100.0
        self.vx = random.uniform(-self.drift, self.drift)
        self.vy = 0.0
        self.angle = 0.0
        self.fuel = self.level_fuel
        self.thrusting = False
        self.rot_dir = 0
        self.particles = []
        self.shake = 0.0

    def _start_game(self):
        self.score = 0
        self.lives = START_LIVES
        self._gen_space()
        self._gen_level(1)
        self._spawn_ship()
        self.state = 'playing'

    # -- terrain queries ---------------------------------------------------
    def _terrain_y(self, x):
        t = self.terrain
        n = len(t)
        if n == 0:
            return 90.0
        if x <= t[0][0]:
            return t[0][1]
        if x >= t[n - 1][0]:
            return t[n - 1][1]
        # scan for the segment containing x
        for i in range(n - 1):
            x0 = t[i][0]
            x1 = t[i + 1][0]
            if x0 <= x <= x1:
                span = x1 - x0
                if span <= 0.0001:
                    return t[i][1]
                frac = (x - x0) / span
                return t[i][1] + (t[i + 1][1] - t[i][1]) * frac
        return t[n - 1][1]

    def _pad_at(self, x):
        for p in self.pads:
            if p['x0'] <= x <= p['x1']:
                return p
        return None

    # -- main loop ---------------------------------------------------------
    def update(self, delta):
        if delta > 100:
            delta = 100
        self.t += delta

        b = self.button_states
        left    = b.get(BUTTON_TYPES["LEFT"])
        right   = b.get(BUTTON_TYPES["RIGHT"])
        up      = b.get(BUTTON_TYPES["UP"])
        cancel  = b.get(BUTTON_TYPES["CANCEL"])
        confirm = b.get(BUTTON_TYPES["CONFIRM"])
        # We do NOT clear() every frame: clearing wipes the held state, which
        # would stop UP from thrusting unless tapped. Instead we track rising
        # edges for the one-shot menu actions (confirm / cancel).
        confirm_edge = confirm and not self._pc
        cancel_edge  = cancel and not self._cc
        self._pc = confirm
        self._cc = cancel

        if self.state == 'title':
            if cancel_edge:
                b.clear()   # clear only when leaving, so we don't re-open instantly
                eventbus.emit(PatternEnable())
                self.minimise()
            elif confirm_edge:
                self._start_game()
            self._update_leds(delta)
            return

        if self.state == 'playing':
            self._update_play(delta, left, right, up, cancel_edge)
            self._update_particles(delta)
            self._update_leds(delta)
            return

        # landed / crashed / gameover share simple "press to continue" logic
        self._update_particles(delta)
        if self.shake > 0:
            self.shake -= delta

        if self.state == 'landed':
            if cancel_edge:
                self.state = 'title'
            elif confirm_edge and self.t - self.state_t > 350:
                self._gen_level(self.level + 1)
                self._spawn_ship()
                self.state = 'playing'

        elif self.state == 'crashed':
            if cancel_edge:
                self._enter_gameover()
            elif confirm_edge and self.t - self.state_t > 500:
                if self.lives > 0:
                    self._spawn_ship()
                    self.state = 'playing'
                else:
                    self._enter_gameover()

        elif self.state == 'gameover':
            if confirm_edge or cancel_edge:
                self.state = 'title'

        self._update_leds(delta)

    def _update_play(self, delta, left, right, up, cancel):
        if cancel:
            self.state = 'title'
            return

        thrusting = up and self.fuel > 0
        self.thrusting = thrusting
        self.rot_dir = 0

        # rotation
        if left and not right:
            self.angle -= ROT_SPEED * delta
            self.rot_dir = -1
            self.fuel -= ROT_BURN * delta / 16.0
        elif right and not left:
            self.angle += ROT_SPEED * delta
            self.rot_dir = 1
            self.fuel -= ROT_BURN * delta / 16.0

        # thrust along the nose direction
        if thrusting:
            sa = math.sin(self.angle)
            ca = math.cos(self.angle)
            self.vx += THRUST * sa * delta
            self.vy += -THRUST * ca * delta
            self.fuel -= THRUST_BURN * delta / 16.0

        if self.fuel < 0:
            self.fuel = 0

        # gravity + integrate
        self.vy += self.gravity * delta
        if self.vy > MAX_VY:
            self.vy = MAX_VY
        if self.vy < -MAX_VY:
            self.vy = -MAX_VY
        self.x += self.vx * delta
        self.y += self.vy * delta

        # ceiling
        if self.y < -112.0:
            self.y = -112.0
            if self.vy < 0:
                self.vy = 0.0

        # invisible side walls (stay inside the round screen)
        maxx = self._halfwidth(self.y) - 16.0
        if maxx < 6.0:
            maxx = 6.0
        if self.x > maxx:
            self.x = maxx
            self.vx = 0.0
        elif self.x < -maxx:
            self.x = -maxx
            self.vx = 0.0

        self._check_contact()

    def _ship_point(self, lx, ly):
        ca = math.cos(self.angle)
        sa = math.sin(self.angle)
        return (self.x + lx * ca - ly * sa,
                self.y + lx * sa + ly * ca)

    def _check_contact(self):
        footL = self._ship_point(-15.0, 13.0)
        footR = self._ship_point(15.0, 13.0)
        belly = self._ship_point(0.0, 12.0)
        nose  = self._ship_point(0.0, -14.0)
        bodyL = self._ship_point(-11.0, -1.0)
        bodyR = self._ship_point(11.0, -1.0)

        # flying the body into a rock is always a crash
        for p in (nose, bodyL, bodyR):
            if p[1] >= self._terrain_y(p[0]):
                self._crash()
                return

        contact = (footL[1] >= self._terrain_y(footL[0]) or
                   footR[1] >= self._terrain_y(footR[0]) or
                   belly[1] >= self._terrain_y(belly[0]))
        if not contact:
            return

        pad = self._pad_at(self.x)
        feet_on_pad = (pad is not None and
                       footL[0] >= pad['x0'] - 3.0 and
                       footR[0] <= pad['x1'] + 3.0 and
                       footL[0] <= pad['x1'] and
                       footR[0] >= pad['x0'])
        speed_ok = (abs(self.vx) < VX_SAFE and
                    self.vy < VY_SAFE and self.vy > -0.02)
        angle_ok = abs(self._norm_angle()) < ANG_SAFE

        if feet_on_pad and speed_ok and angle_ok:
            self._land(pad)
        else:
            self._crash()

    def _land(self, pad):
        # settle the lander onto the pad
        self.y = pad['y'] - 13.0
        self.angle = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.thrusting = False
        base = 100 * pad['mult']
        fuel_bonus = int((self.fuel / self.level_fuel) * 100.0)
        soft = int(max(0.0, (VY_SAFE - self.vy) / VY_SAFE) * 60.0)
        cen = int(max(0.0, 1.0 - abs(self.x - pad['cx']) / pad['half']) * 40.0)
        gained = base + fuel_bonus + soft + cen
        self.score += gained
        self.last_gain = gained
        self.last_mult = pad['mult']
        self.last_fuelbonus = fuel_bonus
        self.state = 'landed'
        self.state_t = self.t

    def _crash(self):
        self.lives -= 1
        self.thrusting = False
        self.shake = 420.0
        # debris + fireball
        self.particles = []
        for _ in range(26):
            ang = random.uniform(0, TWO_PI)
            spd = random.uniform(0.02, 0.12)
            kind = random.random()
            if kind < 0.6:
                col = (1.0, random.uniform(0.4, 0.8), 0.1)   # fire
                sz = random.uniform(2.0, 4.0)
            else:
                g = random.uniform(0.4, 0.7)
                col = (g, g, g * 0.95)                        # metal debris
                sz = random.uniform(1.5, 3.0)
            self.particles.append({
                'x': self.x, 'y': self.y,
                'vx': math.cos(ang) * spd,
                'vy': math.sin(ang) * spd - 0.02,
                'life': random.uniform(500.0, 1100.0),
                'max': 1100.0, 'col': col, 'sz': sz,
            })
        self.state = 'crashed'
        self.state_t = self.t

    def _enter_gameover(self):
        self.last_score = self.score
        if self.score > self.high_score:
            self.high_score = self.score
            self._save_high_score(self.score)
        self.state = 'gameover'
        self.state_t = self.t

    def _update_particles(self, delta):
        if not self.particles:
            return
        alive = []
        for pt in self.particles:
            pt['life'] -= delta
            if pt['life'] <= 0:
                continue
            pt['vy'] += 0.00004 * delta
            pt['x'] += pt['vx'] * delta
            pt['y'] += pt['vy'] * delta
            alive.append(pt)
        self.particles = alive

    # -- LEDs --------------------------------------------------------------
    def _update_leds(self, delta):
        try:
            if self.state == 'title':
                # slow, dim cyan breathe (~2.4s cycle)
                b = int(8 + 12 * (0.5 + 0.5 * math.sin(self.t * 0.0026)))
                for i in range(1, 13):
                    tildagonos.leds[i] = (0, b, b + 4)
                tildagonos.leds.write()
                return

            if self.state == 'landed':
                # gentle green breathe (~1.8s cycle)
                p = int(22 + 55 * (0.5 + 0.5 * math.sin(self.t * 0.0035)))
                for i in range(1, 13):
                    tildagonos.leds[i] = (0, p, 0)
                tildagonos.leds.write()
                return

            if self.state == 'crashed':
                # smooth red breathe (~1.4s) instead of a fast flash
                v = int(18 + 72 * (0.5 + 0.5 * math.sin(self.t * 0.0045)))
                for i in range(1, 13):
                    tildagonos.leds[i] = (v, 0, 0)
                tildagonos.leds.write()
                return

            if self.state == 'gameover':
                # steady dim red, no flashing at all
                for i in range(1, 13):
                    tildagonos.leds[i] = (26, 0, 0)
                tildagonos.leds.write()
                return

            # playing: ring acts as a fuel gauge (dimmed a little)
            frac = self.fuel / self.level_fuel if self.level_fuel else 0.0
            if frac < 0:
                frac = 0.0
            lit = int(round(frac * 12))
            if frac > 0.5:
                col = (0, 70, 8)
            elif frac > 0.2:
                col = (70, 55, 0)
            else:
                col = (90, 0, 0)
            for i in range(1, 13):
                tildagonos.leds[i] = col if i <= lit else (2, 2, 2)
            # engine glow at the bottom LEDs when thrusting:
            # a smooth shimmer (no per-frame random => no strobing)
            if self.thrusting:
                eg = int(90 + 22 * (0.5 + 0.5 * math.sin(self.t * 0.007)))
                tildagonos.leds[6] = (eg, int(eg * 0.45), 0)
                tildagonos.leds[7] = (eg, int(eg * 0.45), 0)
            tildagonos.leds.write()
        except Exception:
            pass

    # -- drawing -----------------------------------------------------------
    def _ctext(self, ctx, s, y, size, col):
        ctx.font_size = size
        ctx.rgb(*col)
        w = ctx.text_width(s)
        ctx.move_to(-w / 2, y).text(s)

    def draw(self, ctx):
        ctx.save()

        if self.state == 'title':
            self._draw_title(ctx)
            ctx.restore()
            return

        # screen shake
        if self.shake > 0:
            mag = self.shake / 90.0
            ctx.translate(random.uniform(-mag, mag), random.uniform(-mag, mag))

        self._draw_space(ctx)
        self._draw_terrain(ctx)
        self._draw_pads(ctx)

        if self.state in ('playing', 'landed'):
            self._draw_ship(ctx)
        if self.particles:
            self._draw_particles(ctx)

        self._draw_hud(ctx)

        if self.state == 'landed':
            self._draw_landed(ctx)
        elif self.state == 'crashed':
            self._draw_crashed(ctx)
        elif self.state == 'gameover':
            self._draw_gameover(ctx)

        ctx.restore()

    def _draw_space(self, ctx):
        ctx.rgb(0.02, 0.02, 0.06).rectangle(-130, -130, 260, 260).fill()
        # stars
        for s in self.stars:
            b = s['b']
            if s['tw']:
                b = b * (0.5 + 0.5 * math.sin(self.t * 0.004 + s['ph']))
            ctx.rgba(1.0, 1.0, 1.0, b)
            ctx.rectangle(s['x'], s['y'], 1.4, 1.4).fill()
        # planets
        for pl in self.planets:
            self._draw_planet(ctx, pl)

    def _draw_planet(self, ctx, pl):
        cx, cy, r = pl['x'], pl['y'], pl['r']
        cr, cg, cb = pl['col']
        dr, dg, db = pl['dark']
        if pl['ring']:
            ctx.save()
            ctx.translate(cx, cy)
            ctx.rotate(-0.5)
            ctx.scale(1.0, 0.30)
            ctx.rgba(dr, dg, db, 0.8)
            ctx.begin_path()
            ctx.move_to(r * 1.7, 0)
            ctx.arc(0, 0, r * 1.7, 0, TWO_PI, 0)
            ctx.fill()
            ctx.rgb(0.02, 0.02, 0.06)
            ctx.begin_path()
            ctx.move_to(r * 1.25, 0)
            ctx.arc(0, 0, r * 1.25, 0, TWO_PI, 0)
            ctx.fill()
            ctx.restore()
        # body
        ctx.rgb(cr, cg, cb)
        ctx.begin_path()
        ctx.move_to(cx + r, cy)
        ctx.arc(cx, cy, r, 0, TWO_PI, 0)
        ctx.fill()
        # bands (clipped to the disc)
        if pl['bands']:
            ctx.save()
            ctx.begin_path()
            ctx.move_to(cx + r, cy)
            ctx.arc(cx, cy, r, 0, TWO_PI, 0)
            ctx.clip()
            for dy in pl['bands']:
                ctx.rgba(dr, dg, db, 0.7)
                ctx.rectangle(cx - r, cy + dy, r * 2, 3.0).fill()
            ctx.restore()

    def _draw_terrain(self, ctx):
        t = self.terrain
        n = len(t)
        ctx.rgb(0.20, 0.18, 0.24)
        ctx.begin_path()
        ctx.move_to(t[0][0], t[0][1])
        for i in range(1, n):
            ctx.line_to(t[i][0], t[i][1])
        ctx.line_to(t[n - 1][0], 135.0)
        ctx.line_to(t[0][0], 135.0)
        ctx.close_path()
        ctx.fill()
        # rim light along the surface
        ctx.rgb(0.45, 0.42, 0.55)
        ctx.line_width = 2.0
        ctx.begin_path()
        ctx.move_to(t[0][0], t[0][1])
        for i in range(1, n):
            ctx.line_to(t[i][0], t[i][1])
        ctx.stroke()

    def _draw_pads(self, ctx):
        blink = int(self.t * 0.006) % 2 == 0
        for p in self.pads:
            if p['mult'] >= 3:
                col = (1.0, 0.2, 0.5)
            elif p['mult'] == 2:
                col = (0.2, 0.9, 1.0)
            else:
                col = (0.2, 1.0, 0.4)
            ctx.rgb(*col)
            ctx.rectangle(p['x0'], p['y'] - 1.0, p['x1'] - p['x0'], 3.0).fill()
            # end posts with lights
            for px in (p['x0'], p['x1']):
                ctx.rgb(0.5, 0.5, 0.55)
                ctx.rectangle(px - 1.0, p['y'] - 8.0, 2.0, 8.0).fill()
                if blink:
                    ctx.rgb(*col)
                    ctx.begin_path()
                    ctx.move_to(px + 1.6, p['y'] - 8.0)
                    ctx.arc(px, p['y'] - 8.0, 1.6, 0, TWO_PI, 0)
                    ctx.fill()
            # multiplier label
            label = "x" + str(p['mult'])
            ctx.font_size = 11
            ctx.rgb(*col)
            ctx.move_to(p['cx'] - ctx.text_width(label) / 2, p['y'] - 11.0).text(label)

    def _draw_ship(self, ctx):
        ctx.save()
        ctx.translate(self.x, self.y)
        ctx.rotate(self.angle)

        # exhaust flame (smooth pulse, not a per-frame random flicker)
        if self.state == 'playing' and self.thrusting:
            fl = 14.0 + 4.0 * (0.5 + 0.5 * math.sin(self.t * 0.012))
            ctx.rgba(1.0, 0.45, 0.0, 0.9)
            ctx.begin_path()
            ctx.move_to(-4.0, 9.0)
            ctx.line_to(4.0, 9.0)
            ctx.line_to(0.0, 9.0 + fl)
            ctx.close_path()
            ctx.fill()
            ctx.rgba(1.0, 0.85, 0.2, 0.95)
            ctx.begin_path()
            ctx.move_to(-2.5, 9.0)
            ctx.line_to(2.5, 9.0)
            ctx.line_to(0.0, 9.0 + fl * 0.65)
            ctx.close_path()
            ctx.fill()
            ctx.rgb(1.0, 1.0, 0.9)
            ctx.begin_path()
            ctx.move_to(-1.2, 9.0)
            ctx.line_to(1.2, 9.0)
            ctx.line_to(0.0, 9.0 + fl * 0.35)
            ctx.close_path()
            ctx.fill()

        # landing legs + feet
        ctx.rgb(0.62, 0.64, 0.70)
        ctx.line_width = 2.0
        ctx.begin_path()
        ctx.move_to(-9.0, 3.0)
        ctx.line_to(-15.0, 13.0)
        ctx.move_to(9.0, 3.0)
        ctx.line_to(15.0, 13.0)
        ctx.move_to(-18.0, 13.0)
        ctx.line_to(-12.0, 13.0)
        ctx.move_to(12.0, 13.0)
        ctx.line_to(18.0, 13.0)
        ctx.stroke()

        # descent stage (gold)
        ctx.rgb(0.86, 0.68, 0.18)
        ctx.begin_path()
        ctx.move_to(-11.0, -3.0)
        ctx.line_to(11.0, -3.0)
        ctx.line_to(9.0, 6.0)
        ctx.line_to(-9.0, 6.0)
        ctx.close_path()
        ctx.fill()
        ctx.rgb(0.70, 0.54, 0.12)
        ctx.rectangle(-9.0, 2.0, 18.0, 2.0).fill()

        # engine nozzle
        ctx.rgb(0.30, 0.30, 0.34)
        ctx.begin_path()
        ctx.move_to(-3.0, 6.0)
        ctx.line_to(3.0, 6.0)
        ctx.line_to(2.0, 10.0)
        ctx.line_to(-2.0, 10.0)
        ctx.close_path()
        ctx.fill()

        # ascent module (grey capsule)
        ctx.rgb(0.80, 0.82, 0.87)
        ctx.begin_path()
        ctx.move_to(-7.0, -3.0)
        ctx.line_to(-7.0, -10.0)
        ctx.line_to(-4.0, -14.0)
        ctx.line_to(4.0, -14.0)
        ctx.line_to(7.0, -10.0)
        ctx.line_to(7.0, -3.0)
        ctx.close_path()
        ctx.fill()

        # cockpit window
        ctx.rgb(0.15, 0.85, 1.0)
        ctx.begin_path()
        ctx.move_to(2.6, -8.0)
        ctx.arc(0.0, -8.0, 2.6, 0, TWO_PI, 0)
        ctx.fill()
        ctx.rgb(0.8, 1.0, 1.0)
        ctx.begin_path()
        ctx.move_to(0.3, -8.7)
        ctx.arc(-0.7, -8.7, 1.0, 0, TWO_PI, 0)
        ctx.fill()

        # antenna
        ctx.rgb(0.62, 0.64, 0.70)
        ctx.line_width = 1.0
        ctx.begin_path()
        ctx.move_to(0.0, -14.0)
        ctx.line_to(0.0, -18.0)
        ctx.stroke()
        ctx.rgb(1.0, 0.3, 0.3)
        ctx.begin_path()
        ctx.move_to(1.3, -18.0)
        ctx.arc(0.0, -18.0, 1.3, 0, TWO_PI, 0)
        ctx.fill()

        # RCS puff while rotating
        if self.state == 'playing' and self.rot_dir != 0:
            px = 9.0 * self.rot_dir
            ctx.rgba(0.9, 0.9, 1.0, 0.5)
            rr = random.uniform(1.5, 3.0)
            ctx.begin_path()
            ctx.move_to(-px + rr, -10.0)
            ctx.arc(-px, -10.0, rr, 0, TWO_PI, 0)
            ctx.fill()

        ctx.restore()

    def _draw_particles(self, ctx):
        for pt in self.particles:
            a = pt['life'] / pt['max']
            if a > 1.0:
                a = 1.0
            r, g, b = pt['col']
            ctx.rgba(r, g, b, a)
            ctx.rectangle(pt['x'] - pt['sz'] / 2, pt['y'] - pt['sz'] / 2,
                          pt['sz'], pt['sz']).fill()

    def _draw_hud(self, ctx):
        # score
        self._ctext(ctx, str(self.score), -99.0, 15, (1.0, 1.0, 1.0))
        # level (left) and lives (right)
        ctx.font_size = 12
        ctx.rgb(0.7, 0.75, 0.85)
        ctx.move_to(-80.0, -82.0).text("L" + str(self.level))
        lx = 78.0
        for _ in range(self.lives):
            ctx.rgb(0.2, 0.9, 0.5)
            ctx.begin_path()
            ctx.move_to(lx + 3.0, -86.0)
            ctx.arc(lx, -86.0, 3.0, 0, TWO_PI, 0)
            ctx.fill()
            lx -= 9.0

        if self.state != 'playing':
            return

        # speed readout
        vy_d = int(abs(self.vy) * SPEED_DISP)
        vx_d = int(abs(self.vx) * SPEED_DISP)
        v_ok = self.vy < VY_SAFE
        h_ok = abs(self.vx) < VX_SAFE
        vstr = "V " + str(vy_d)
        hstr = "H " + str(vx_d)
        ctx.font_size = 14
        vw = ctx.text_width(vstr)
        hw = ctx.text_width(hstr)
        gap = 14.0
        total = vw + gap + hw
        sx = -total / 2.0
        ctx.rgb(*((0.3, 1.0, 0.4) if v_ok else (1.0, 0.35, 0.35)))
        ctx.move_to(sx, -66.0).text(vstr)
        ctx.rgb(*((0.3, 1.0, 0.4) if h_ok else (1.0, 0.35, 0.35)))
        ctx.move_to(sx + vw + gap, -66.0).text(hstr)

        # tilt / level indicator
        ang = self._norm_angle()
        a_ok = abs(ang) < ANG_SAFE
        ctx.save()
        ctx.translate(0.0, -50.0)
        ctx.rotate(ang)
        ctx.rgb(*((0.3, 1.0, 0.4) if a_ok else (1.0, 0.35, 0.35)))
        ctx.line_width = 2.0
        ctx.begin_path()
        ctx.move_to(-12.0, 0.0)
        ctx.line_to(12.0, 0.0)
        ctx.stroke()
        ctx.restore()

        # fuel bar
        fw = 80.0
        frac = self.fuel / self.level_fuel if self.level_fuel else 0.0
        if frac < 0:
            frac = 0.0
        ctx.rgb(0.25, 0.25, 0.3)
        ctx.rectangle(-fw / 2, 98.0, fw, 6.0).fill()
        if frac > 0.5:
            fc = (0.2, 0.9, 0.3)
        elif frac > 0.2:
            fc = (0.9, 0.8, 0.1)
        else:
            fc = (1.0, 0.2, 0.2)
        ctx.rgb(*fc)
        ctx.rectangle(-fw / 2, 98.0, fw * frac, 6.0).fill()
        ctx.font_size = 9
        ctx.rgb(0.7, 0.72, 0.8)
        ctx.move_to(-fw / 2 - 18.0, 104.0).text("FUEL")

    def _panel(self, ctx, h):
        ctx.rgba(0.0, 0.0, 0.05, 0.78)
        ctx.rectangle(-95.0, -h / 2, 190.0, h).fill()

    def _draw_landed(self, ctx):
        self._panel(ctx, 92.0)
        self._ctext(ctx, "TOUCHDOWN!", -28.0, 20, (0.3, 1.0, 0.5))
        self._ctext(ctx, "x" + str(self.last_mult) + " pad  +" + str(self.last_gain),
                    -4.0, 14, (1.0, 1.0, 1.0))
        self._ctext(ctx, "fuel bonus +" + str(self.last_fuelbonus),
                    16.0, 12, (0.7, 0.85, 1.0))
        self._ctext(ctx, "CONFIRM: next level", 36.0, 12, (0.8, 0.8, 0.5))

    def _draw_crashed(self, ctx):
        self._panel(ctx, 80.0)
        self._ctext(ctx, "CRASHED", -22.0, 22, (1.0, 0.35, 0.35))
        if self.lives > 0:
            self._ctext(ctx, str(self.lives) + " landers left", 4.0, 13,
                        (1.0, 1.0, 1.0))
            self._ctext(ctx, "CONFIRM: retry", 26.0, 12, (0.8, 0.8, 0.5))
        else:
            self._ctext(ctx, "no landers left", 4.0, 13, (1.0, 1.0, 1.0))
            self._ctext(ctx, "CONFIRM: continue", 26.0, 12, (0.8, 0.8, 0.5))

    def _draw_gameover(self, ctx):
        self._panel(ctx, 104.0)
        self._ctext(ctx, "GAME OVER", -34.0, 22, (1.0, 0.5, 0.2))
        self._ctext(ctx, "Score: " + str(self.score), -8.0, 15, (1.0, 1.0, 1.0))
        self._ctext(ctx, "Best: " + str(self.high_score), 14.0, 14,
                    (1.0, 0.85, 0.2))
        self._ctext(ctx, "CONFIRM: menu", 38.0, 12, (0.8, 0.8, 0.5))

    def _draw_title(self, ctx):
        self._draw_space(ctx)
        # a little lander hovering on the title
        ctx.save()
        self.x = 0.0
        self.y = 0.0
        self.angle = 0.0
        self.state = 'title'
        self.thrusting = False
        self.rot_dir = 0
        ctx.translate(0.0, -16.0)
        ctx.scale(1.15, 1.15)
        # reuse ship drawing without flame/contact
        self._draw_ship_static(ctx)
        ctx.restore()

        self._ctext(ctx, "TILDA", -86.0, 30, (0.85, 0.85, 0.95))
        self._ctext(ctx, "LANDER", -58.0, 30, (0.3, 0.8, 1.0))

        ctx.font_size = 12
        self._ctext(ctx, "L / R : rotate", 34.0, 12, (0.85, 0.88, 0.95))
        self._ctext(ctx, "UP : fire thruster", 50.0, 12, (0.85, 0.88, 0.95))
        self._ctext(ctx, "land slow & level on a pad", 66.0, 11,
                    (0.6, 0.65, 0.75))

        if self.high_score > 0:
            self._ctext(ctx, "Best: " + str(self.high_score), 86.0, 13,
                        (1.0, 0.85, 0.2))
        self._ctext(ctx, "CONFIRM to play", 104.0, 13, (0.3, 0.9, 0.5))

    def _draw_ship_static(self, ctx):
        # legs
        ctx.rgb(0.62, 0.64, 0.70)
        ctx.line_width = 2.0
        ctx.begin_path()
        ctx.move_to(-9.0, 3.0)
        ctx.line_to(-15.0, 13.0)
        ctx.move_to(9.0, 3.0)
        ctx.line_to(15.0, 13.0)
        ctx.move_to(-18.0, 13.0)
        ctx.line_to(-12.0, 13.0)
        ctx.move_to(12.0, 13.0)
        ctx.line_to(18.0, 13.0)
        ctx.stroke()
        ctx.rgb(0.86, 0.68, 0.18)
        ctx.begin_path()
        ctx.move_to(-11.0, -3.0)
        ctx.line_to(11.0, -3.0)
        ctx.line_to(9.0, 6.0)
        ctx.line_to(-9.0, 6.0)
        ctx.close_path()
        ctx.fill()
        ctx.rgb(0.30, 0.30, 0.34)
        ctx.begin_path()
        ctx.move_to(-3.0, 6.0)
        ctx.line_to(3.0, 6.0)
        ctx.line_to(2.0, 10.0)
        ctx.line_to(-2.0, 10.0)
        ctx.close_path()
        ctx.fill()
        ctx.rgb(0.80, 0.82, 0.87)
        ctx.begin_path()
        ctx.move_to(-7.0, -3.0)
        ctx.line_to(-7.0, -10.0)
        ctx.line_to(-4.0, -14.0)
        ctx.line_to(4.0, -14.0)
        ctx.line_to(7.0, -10.0)
        ctx.line_to(7.0, -3.0)
        ctx.close_path()
        ctx.fill()
        ctx.rgb(0.15, 0.85, 1.0)
        ctx.begin_path()
        ctx.move_to(2.6, -8.0)
        ctx.arc(0.0, -8.0, 2.6, 0, TWO_PI, 0)
        ctx.fill()


__app_export__ = TildaLander
