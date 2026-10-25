#!/usr/bin/env python3
"""weather_advice.py â€” turn a raw forecast into decisions the owner can act on.

THE OWNER'S ASK
---------------
    "in morning it should say there is _% of raining chances so you should
     wash/not wash your clothes, wear warm clothes when going out"

So this module does not summarise weather. It answers three questions with a
threshold and an action attached: may I wash, do I carry an umbrella, do I
wear a jacket. Silence is a first-class answer; see DEGRADATION below.

DETERMINISM â€” READ THIS BEFORE EDITING
--------------------------------------
This module is PURE. It must never call an LLM and must never touch the
network: no socket, no HTTP client, no shell-out, no shelling out to a model
to do arithmetic. Every number in every output string is computed here by
ordinary comparison, and every threshold comes out of the profile. An agent
upstream may *choose to read these strings*; it may never author one. That is
the whole safety property: the owner acts on a verdict, so the verdict has to
be reproducible, auditable and free of a model's mood.

This module is also decoupled from open-meteo's response shape. It only ever
sees the normalized schema (see NORMALIZED INPUT below). A field-name change
in the upstream API is the fetch adapter's problem (a different unit), never
this file's. That is deliberate: the rule table must be reviewable without
reading anybody's HTTP client.

NORMALIZED INPUT
----------------
    {
      "fetched_at_npt": "2026-09-28 06:12",     # optional when a bare list is passed
      "place": {"city": ..., "latitude": ..., "longitude": ..., "timezone": ...},
      "days": [
        {"date": "2026-09-28", "tmax_c": 22.0, "tmin_c": 14.0,
         "precip_sum_mm": 17.7, "weathercode": 80,
         "hours": [
           {"hour": 7,  "pop_pct": 30, "precip_mm": 0.0, "apparent_c": 19.4,
            "wind_kmh": 6.0, "visibility_m": 15000.0},
           ...
         ]}
      ]
    }

`visibility_m` is OPTIONAL. The VS rule reads it and simply never fires when
it is absent, which is the point of a closed rule set: a missing optional
input degrades one rule, not the whole digest.

`precip_mm` is OPTIONAL on the same terms, and it is REQUIRED BY THE RAIN
RULES for the same reason. W1 and U threshold `precip_mm` against the
`precip_wet_mm` wetting floor in the profile; an hour with no `precip_mm` is
an hour of unknown amount, and an unknown amount is not evidence of rain.
Read the AMOUNT, not just the chance: a 88 % chance of 0.2 mm is drizzle the
laundry survives, and advising against washing on it is a wrong statement of
consequence, not a cautious one. See TWO NECESSARY CONDITIONS below.

Only `days[0]` produces advice â€” the rules are phrased in the present tense
("today", "when you go out") and a 7-day PoP is not a thing to act on. Days
beyond the first are still checked for structural completeness (see
DEGRADATION), because a truncated forecast means we do not actually know what
today looks like.

DEGRADATION â€” SILENCE, NOT A WARNING
------------------------------------
weather_advice() returns [] when ANY of these hold:

  * the input is absent, or `days` is empty
  * `hours` is missing (or empty) on ANY day
  * `pop_pct` is absent on ANY hour
  * the payload is older than `max_age_hours`

No "couldn't fetch weather" line is ever emitted. A warning the owner cannot
act on is precisely the noise he complained about when he asked for this
engine. The anti-nag property is worth more than the completeness of the
line, so a missing input costs him nothing.

THE SERIOUS GUARD
-----------------
`blockers` is a list of (start_hour, end_hour, label) â€” a commitment he
already has. Two failure paths are closed here, both regression-tested:

  1. "Don't wash clothes" spanning an exam. If the W1 qualifying window
     intersects a blocker, the verdict is DOWNGRADED, never prohibited:
     "Wash after 15:00-17:59 - 70% chance of rain before then". Prohibiting
     a wash because rain is forecast is a small loss. Prohibiting it across a
     deadline is a schedule disaster, and the module has no business making
     that trade for him. Postpone, never forbid.
  2. Weather language implying waiting. Every string here is purely
     descriptive of weather plus a carry/wear action. Nothing says "wait",
     "stay", "leave early" or "you have time". The nearest real failure is him
     reading advice as permission to leave ten minutes late and missing class,
     so the wording is constrained, not just the thresholds.

WINDOW RENDERING (interpretation, stated so it is testable)
-----------------------------------------------------------
`{win}` is derived from the first and last hour of the contiguous qualifying
run as `HH:00-HH:59`. Two deliberate choices:

  * END AT :59, not at the next hour's :00. An hourly sample at 17:00 means
    the condition holds *during* hour 17, so 17:00-17:59 is the honest span
    and 17:00-18:00 would overstate it.
  * WHEN SEVERAL RUNS QUALIFY, the run reported is the one CONTAINING THE
    EXTREME VALUE that tripped the rule (the lowest visibility, the highest
PoP, the coldest apparent temperature). The rule table is phrased in
    terms of min/max, so the window that makes the min/max true is the
    window the string must name. Ties resolve to the earliest run, which
    keeps the output stable across runs.
  * A RUN IS CALENDAR-CONTIGUOUS. Two qualifying hours are one run only when
    the second is the hour immediately after the first. Judging contiguity on
    the observed hours alone fuses two separate spells across a gap and then
    renders the pair as first-hour..last-hour, which names an hour nobody
    sampled. That happened: outdoor hours 7-11 and 17-19 are not one spell,
    and 12:00 is not in either of them, so a fused run printed `11:00-17:59`
    and claimed a six-hour block that includes five unsampled hours.

TWO NECESSARY CONDITIONS FOR THE RAIN RULES
-------------------------------------------
W1 (don't wash) and U (umbrella) each need TWO things in the SAME hour, not
one:

    pop_pct >= wash_no / umbrella      AND      precip_mm >= precip_wet_mm

Probability alone was a defect, not a simplification. open-meteo pairs a high
`precipitation_probability` with a trivially small `precipitation` all the
time, because the two are answers to different questions: "how likely is
precipitation in this hour" and "how much water falls in it". Example City on
2026-10-03 peaked at 88 % with 0.2 mm and the digest published "Don't wash
clothes today - 88% chance of rain" for three consecutive days over 1.7 mm of
total drizzle. A prohibition is a claim about what the water DOES, and the
consequence of 0.2 mm is that the laundry gets slightly damp.

The floor is `precip_wet_mm` in the profile, calibrated against 27 days of
real Example City data rather than guessed: across 12 WMO drizzle days (codes
51/53) the largest hourly amount in ANY hour the rain rules can read was
0.8 mm, and the largest in an outdoor window was 0.6 mm. A floor below that
reproduces the original defect on a different day. 1.0 mm sits two 0.1 mm
grid steps clear of the measured drizzle ceiling.

This is an ADDITIONAL necessary condition, not a change to either
probability threshold. `wash_no` and `umbrella` are untouched, and so is the
deliberate 40-49 % silent band: the amount conjunct narrows what the rules
say without moving where they speak.

THRESHOLDS LIVE IN THE PROFILE, ONCE
------------------------------------
Every rule threshold is read from `weather_advice.thresholds` in the
profile. This file contains no rule threshold as a literal â€” grep for 50, 40,
10, 15, 30 or 1000 in the rule bodies and you will not find them, because a
number that appears in both the code and the config is a defect: the two
copies drift, and the config quietly stops meaning what the code means.
A missing threshold is a validation ERROR, not a default, for the same
reason (see PROFILE VALIDATION).

Stdlib only. Windows-friendly (ASCII-only stdout).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Rule table: ids, severity order, and the exact output strings.
#
# The strings are FROZEN. A test asserts each one character for character,
# because the owner reads these and adjusts his day on them; an accidental
# rewording is a behaviour change, not a copy fix.
# ---------------------------------------------------------------------------

T_VS = "Low visibility {vis} m {win} - take care on the road"
T_V = "Strong wind {wind} km/h {win} - hold onto an umbrella"
T_W1 = "Don't wash clothes today - {pop}% chance of rain {win}"
T_W1_BLOCKED = "Wash after {win} - {pop}% chance of rain before then"
T_U = "Take an umbrella - {pop}% chance of rain {win}"
T_J2 = "Wear a warm jacket - feels like {temp}C when you go out {win}"
T_J1 = "Take a jacket - feels like {temp}C {win}"
T_C = "Nothing to plan for - dry and mild today ({tmax}C)"

#: Severity order, most severe first. The index IS the priority, and callers
#: may sort on it. Output is emitted in this order regardless of which rules
#: fired, and `max_lines` truncates the tail, so the cap sheds the least
#: consequential line rather than an arbitrary one.
RULE_IDS = ("VS", "V", "W1", "U", "J2", "J1", "C")
PRIORITY = {rule_id: i for i, rule_id in enumerate(RULE_IDS)}
PRIORITY["W1-blocked"] = PRIORITY["W1"]

#: Documented policy defaults. These are NOT rule thresholds: they are the
#: two knobs the spec explicitly gives a default, and they are not duplicated
#: in the rule bodies. Rule thresholds have no default at all, by design.
DEFAULT_MAX_AGE_HOURS = 3
DEFAULT_MAX_LINES = 3

#: Legal bands for a threshold. These are validity bounds (what could
#: physically be a PoP, a temperature, a wind speed), not policy. Policy lives
#: in the profile; sanity lives here.
_THRESHOLD_BANDS = {
    "wash_no": (0.0, 100.0, "a rain probability percentage"),
    "umbrella": (0.0, 100.0, "a rain probability percentage"),
    "precip_wet_mm": (0.0, 1000.0, "an hourly precipitation amount in mm"),
    "warm_strong_c": (-90.0, 60.0, "an apparent temperature in Celsius"),
    "warm_mild_c": (-90.0, 60.0, "an apparent temperature in Celsius"),
    "wind_kmh": (0.0, 400.0, "a wind speed in km/h"),
    "visibility_m": (0.0, 100000.0, "a visibility in metres"),
}

THRESHOLD_KEYS = tuple(_THRESHOLD_BANDS)

# ---------------------------------------------------------------------------
# Profile shape. Unknown keys are rejected under the three sections this unit
# owns; `location` is deliberately exempt because it carries legacy keys that
# the LLM prompt and other units already read.
# ---------------------------------------------------------------------------

KNOWN_KEYS = {
    "schedule": frozenset({"outdoor_windows", "wash_window", "unavailable_windows"}),
    "weather_advice": frozenset({"enabled", "max_age_hours", "max_lines", "thresholds"}),
    "for_you": frozenset({
        "enabled", "min_length", "max_length", "min_personal_relevance",
        "max_stories", "categories", "reliability_tiers",
        # The two word lists validate_stories.py screens with. They are named
        # here so `--check` accepts the profile that carries them; their
        # CONTENTS are deliberately not validated by this unit (no word list
        # belongs to the weather engine) beyond being non-empty strings, which
        # is exactly what the screen needs to be able to run at all.
        "banned_openers", "hedge_markers",
    }),
}

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class ProfileError(ValueError):
    """Raised when a profile is missing, unparseable, or fails validation.

    A loud failure here means NO advice today, which is the safe outcome: see
    the note in validate_profile().
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A half-open local-time window [start_hour, end_hour).

    Half-open because 07:00-12:00 means "the hours 7 through 11", not "the
    hour 12 as well". Windows are whole hours on purpose: config is written
    in HH:MM and a half-hour boundary would silently mis-window the data.
    """

    start_hour: int
    end_hour: int

    def hours(self) -> range:
        return range(self.start_hour, self.end_hour)

    def contains(self, hour: int) -> bool:
        return self.start_hour <= hour < self.end_hour

    def intersects(self, other: "Window") -> bool:
        return (self.start_hour < other.end_hour
                and other.start_hour < self.end_hour)

    def intersect_hours(self, other: "Window") -> list:
        lo = max(self.start_hour, other.start_hour)
        hi = min(self.end_hour, other.end_hour)
        return list(range(lo, hi))


@dataclass(frozen=True)
class Thresholds:
    """The seven rule thresholds. Every value is read from the profile."""

    wash_no: float
    umbrella: float
    precip_wet_mm: float
    warm_strong_c: float
    warm_mild_c: float
    wind_kmh: float
    visibility_m: float


@dataclass(frozen=True)
class Profile:
    """A validated profile. Construct only through load_profile()."""

    path: Optional[str]
    location: dict
    outdoor_windows: tuple
    wash_window: Window
    unavailable_windows: tuple
    weather_enabled: bool
    max_age_hours: float
    max_lines: int
    thresholds: Thresholds
    for_you: dict
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------


def _fmt_num(value) -> str:
    """Render a number the way the owner reads it.

    70.0 -> "70", 9.5 -> "9.5", 22.0 -> "22". A trailing ".0" on every
    temperature would be noise in a line he reads at 06:00.
    """
    number = float(value)
    if number == int(number):
        return str(int(number))
    return "%.1f" % number


def _fmt_window(run: Sequence[int]) -> str:
    """`HH:00-HH:59` from the first and last hour of a run.

    See the module docstring: the run covers from the start of the first
    qualifying hour to the END of the last one, and the end of hour H is
    H:59. Empty run renders as "00:00-00:00" rather than raising, so a
    malformed input degrades to a visibly wrong line instead of a traceback.
    """
    if not run:
        return "00:00-00:00"
    return "%02d:00-%02d:59" % (run[0], run[-1])


# ---------------------------------------------------------------------------
# Run finding
# ---------------------------------------------------------------------------


def _runs(hours: Sequence[int], qualifies) -> list:
    """Split `hours` into maximal contiguous runs where qualifies(hour) is true.

    CONTIGUITY IS CALENDAR CONTIGUITY, not contiguity of the list. A run
    extends only when the hour is the one immediately after the hour already
    in it, so two qualifying hours separated by an hour that does not qualify
    -- or by an hour nobody sampled -- are TWO runs.

    Both readings sound defensible in the abstract and only one of them is
    true. The caller has already narrowed `hours` to the configured windows,
    so a gap between two qualifying hours is a real gap in the weather, not a
    gap in the data: outdoor hours 7-11 and 17-19 are separate spells by the
    owner's own schedule. Fusing them across the unsampled 12:00-16:59 and then
    rendering the pair by its endpoints printed `11:00-17:59`, a six-hour block
    that names five hours the engine holds no sample for and that lie outside
    every window it is allowed to speak about.

    A missing sample is handled by the same test and deliberately so: if 09:00
    is absent then 08:00 and 10:00 are not one run, because a block we never
    sampled is not a block we may describe.
    """
    runs = []
    current = []
    for hour in hours:
        if not qualifies(hour):
            if current:
                runs.append(current)
                current = []
            continue
        if current and hour != current[-1] + 1:
            runs.append(current)
            current = []
        current.append(hour)
    if current:
        runs.append(current)
    return runs


def _run_holding(runs, target_hour) -> list:
    """The run containing `target_hour`, else the first run (total function)."""
    for run in runs:
        if target_hour in run:
            return run
    return runs[0] if runs else []


#: Comparison predicates for _best(). Explicit directions rather than
#: min/max selectors: `max(a, b) < 0` is never true, and that bug shipped
#: once already -- it silently pinned every statistic to the FIRST hour it
#: looked at, so a second, worse fog bank or a colder afternoon hour was
#: reported as if it were the day's extreme.
_HIGHER = lambda a, b: a > b
_LOWER = lambda a, b: a < b


def _best(hours: Sequence[int], value_of, better):
    """(extremal value, its hour) over `hours`, or (None, None) if empty.

    `better(candidate, incumbent)` decides whether to replace. Ties resolve to
    the earliest hour so the rendered window is stable across runs.
    """
    best_value = None
    best_hour = None
    for hour in hours:
        value = value_of(hour)
        if value is None:
            continue
        if best_value is None or better(value, best_value):
            best_value = value
            best_hour = hour
    return best_value, best_hour


# ---------------------------------------------------------------------------
# The normalized forecast, read defensively
# ---------------------------------------------------------------------------


def _unpack(days):
    """Return (day_list, fetched_at_npt) from either accepted input shape.

    `days` may be the full normalized payload (a mapping) or just the list of
    days. Accepting both costs three lines and means the fetch adapter can
    hand over whatever it naturally holds. Only the mapping carries
    `fetched_at_npt`, so only the mapping can be staleness-checked; callers
    that want the age gate enforced should pass the payload. See
    DEPENDENCIES in the build report.
    """
    if days is None:
        return [], None
    if isinstance(days, dict):
        return list(days.get("days") or []), days.get("fetched_at_npt")
    if isinstance(days, (list, tuple)):
        return list(days), None
    return [], None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_fetched_at(raw):
    """Parse "YYYY-MM-DD HH:MM" (naive, local NPT). None on anything else."""
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


def _hour_index(day):
    """{hour: record} for one day, or None if structurally incomplete.

    A missing `hours` list, an empty one, or a single hour without `pop_pct`
    all mean we do not have a trustworthy today, and the contract is silence.
    """
    hours = day.get("hours")
    if not isinstance(hours, list) or not hours:
        return None
    index = {}
    for record in hours:
        if not isinstance(record, dict):
            return None
        hour = record.get("hour")
        if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
            return None
        if not _is_number(record.get("pop_pct")):
            return None
        index[hour] = record
    if not index:
        return None
    return index


def _hours_in_windows(index, windows):
    """Sorted observed hours that fall inside any of `windows`.

    Accepts a single Window or an iterable of them, because the wash window
    is one span and the outdoor windows are two, and forcing every caller to
    wrap the single case in a tuple is how one of them ends up unwrapped.
    """
    if isinstance(windows, Window):
        windows = (windows,)
    hours = sorted(index)
    return [h for h in hours if any(w.contains(h) for w in windows)]


def _wetting(record, floor) -> bool:
    """Does this hour carry enough water to matter, per the profile floor?

    The SECOND necessary condition for the rain rules, and the one that was
    missing. `pop_pct` says precipitation is likely; only `precip_mm` says how
    much of it lands. Thresholding the first alone is what produced "Don't wash
    clothes today - 88% chance of rain" over 0.2 mm of drizzle.

    A MISSING amount is not a wetting hour. The fetch adapter marks
    `precipitation` optional, so `precip_mm` can arrive as None, and an unknown
    quantity is not evidence of rain -- the same reasoning that keeps rule VS
    from firing on an absent `visibility_m`. It degrades W1 and U and nothing
    else: no other rule reads this field, and the digest still says its calm
    line, so the owner is never left with an empty header.

    The floor comes from the profile. This function holds no number, for the
    same reason every other rule body holds no number: two copies drift.
    """
    amount = record.get("precip_mm")
    return _is_number(amount) and amount >= floor


def _hours_in_window(index, window):
    return [h for h in sorted(index) if window.contains(h)]


def _build_windows(windows, index, value_of, better, qualifiers):
    """The contiguous qualifying run that contains the extremal value.

    Returns (None, None) when nothing qualifies. The extremal value is taken
    over EVERY qualifying hour, not just the first run: the rule table is
    phrased in terms of a min/max over the window, and truncating the
    candidate runs before taking the extreme would report a milder hour than
    the one the rule actually fired on.
    """
    observed = _hours_in_windows(index, windows)
    runs = _runs(observed, qualifiers)
    if not runs:
        return None, None
    value, hour = _best([h for run in runs for h in run], value_of, better)
    if hour is None:
        return None, None
    return _run_holding(runs, hour), value


def _eval_vis(index, thresholds, outdoor, _wash, _daily):
    """VS: sub-kilometre visibility on the road. Definition, not preference."""
    runs = _runs(
        _hours_in_windows(index, outdoor),
        lambda h: _is_number(index[h].get("visibility_m"))
        and index[h]["visibility_m"] < thresholds.visibility_m,
    )
    if not runs:
        return None
    value, hour = _best(
        [h for run in runs for h in run],
        lambda h: index[h].get("visibility_m"), _LOWER)
    return _emit("VS", T_VS, {"vis": _fmt_num(value),
                              "win": _fmt_window(_run_holding(runs, hour))})


def _eval_wind(index, thresholds, outdoor, _wash, _daily):
    """V: strong-breeze band, where an umbrella inverts and stops helping."""
    run, value = _build_windows(
        outdoor, index,
        lambda h: index[h].get("wind_kmh") if _is_number(index[h].get("wind_kmh")) else None,
        _HIGHER,
        lambda h: _is_number(index[h].get("wind_kmh"))
        and index[h]["wind_kmh"] >= thresholds.wind_kmh,
    )
    if run is None:
        return None
    return _emit("V", T_V, {"wind": _fmt_num(value), "win": _fmt_window(run)})


def _eval_wash(index, thresholds, _outdoor, wash, _daily, blockers):
    """W1 / W1-blocked: the washing verdict, with the serious guard applied.

    The statistic is max PoP over the wash (drying) window, never the daily
    rain total: washing is a decision about multi-hour outdoor exposure, and
    a daily total says nothing about whether the afternoon stays dry.

    TWO conditions, both required, in the SAME hour: PoP >= wash_no AND
    `precip_mm` >= precip_wet_mm. Probability alone was the confirmed defect
    that made this engine prohibit washing over drizzle; see TWO NECESSARY
    CONDITIONS in the module docstring. The reported PoP is the maximum over
    the qualifying hours, so the number in the string belongs to an hour that
    was also wet -- the string and the window describe the same hour.

    The 40-49 band deliberately produces NOTHING here, and still does. W1
    needs >= wash_no and U fires at >= umbrella; with wash_no=50 and
    umbrella=40, a 45% day yields an umbrella line and no wash verdict at all.
    That gap is the design, not an oversight: a hedge ("maybe don't wash") in
    the band where rain is genuinely uncertain is the wording that destroys
    trust in weather advice, and the owner was explicit that accuracy must not
    be traded for more advice. Silence is the correct output in the middle.
    The amount conjunct is a second necessary condition inside the rule; it
    does not move either probability threshold and it does not touch the band.
    """
    run, value = _build_windows(
        wash, index,
        lambda h: index[h].get("pop_pct"),
        _HIGHER,
        lambda h: index[h]["pop_pct"] >= thresholds.wash_no
        and _wetting(index[h], thresholds.precip_wet_mm),
    )
    if run is None:
        return None
    slots = {"pop": _fmt_num(value), "win": _fmt_window(run)}

    if _blocked(run, blockers):
        # THE SERIOUS GUARD. A commitment intersects the rain window, so the
        # verdict is postponed, never prohibited. See module docstring.
        return _emit("W1-blocked", T_W1_BLOCKED, slots)
    return _emit("W1", T_W1, slots)


def _eval_umbrella(index, thresholds, outdoor, _wash, _daily):
    """U: carry an umbrella. Costs seconds to carry, more to be soaked.

    Same two conditions as W1, at the umbrella probability: an hour needs both
    a likely-enough PoP and a real amount of water. The floor is shared, not a
    second number, because "worth carrying something for" and "worth not
    washing" are the same question about the same hour.
    """
    run, value = _build_windows(
        outdoor, index,
        lambda h: index[h].get("pop_pct"),
        _HIGHER,
        lambda h: index[h]["pop_pct"] >= thresholds.umbrella
        and _wetting(index[h], thresholds.precip_wet_mm),
    )
    if run is None:
        return None
    return _emit("U", T_U, {"pop": _fmt_num(value), "win": _fmt_window(run)})


def _eval_jackets(index, thresholds, outdoor, _wash, _daily):
    """J2 then J1: apparent temperature, not air temperature.

    Apparent temperature already folds in wind chill and humidity, which is
    why people actually feel cold. The mild band is derived from the two
    profile numbers rather than written as 11..14, so moving a threshold in
    the profile moves the silence band with it and the two can never drift.
    """
    observed = _hours_in_windows(index, outdoor)

    strong, cold_hour = _best(
        observed,
        lambda h: index[h].get("apparent_c") if _is_number(index[h].get("apparent_c")) else None,
        _LOWER)
    if cold_hour is not None and strong <= thresholds.warm_strong_c:
        # The reported run is the run of hours that QUALIFY (at or below the
        # strong threshold), not the run of hours exactly equal to the
        # minimum. Every other rule reports its qualifying run -- three cold
        # hours in a row are one cold morning, and the owner needs to see the
        # span he has to dress for, not just the single coldest sample.
        run = _run_holding(
            _runs(observed, lambda h: _is_number(index[h].get("apparent_c"))
                  and index[h]["apparent_c"] <= thresholds.warm_strong_c),
            cold_hour)
        return _emit("J2", T_J2, {"temp": _fmt_num(strong), "win": _fmt_window(run)})

    mild_runs = _runs(
        observed,
        lambda h: _is_number(index[h].get("apparent_c"))
        and thresholds.warm_strong_c < index[h]["apparent_c"] < thresholds.warm_mild_c)
    if not mild_runs:
        return None
    value, hour = _best(
        [h for run in mild_runs for h in run],
        lambda h: index[h].get("apparent_c"), _LOWER)
    return _emit("J1", T_J1, {"temp": _fmt_num(value),
                              "win": _fmt_window(_run_holding(mild_runs, hour))})


def _eval_calm(index, _thresholds, _outdoor, _wash, daily):
    """C: the anti-nag mechanism, made visible.

    Silence is ambiguous â€” did the rules fail, or did the module not run? The
    header must never be empty, so "nothing to plan for" is stated out loud.
    It is the LAST rule, so it is also the first thing `max_lines` sheds, and
    it only fires when nothing else did.
    """
    tmax = daily.get("tmax_c")
    if not _is_number(tmax):
        return None
    return _emit("C", T_C, {"tmax": _fmt_num(tmax)})


def _emit(rule_id, template, slots):
    return {"id": rule_id, "text": template.format(**slots),
            "priority": PRIORITY[rule_id]}


def _blocked(run, blockers) -> bool:
    """Does a commitment overlap any hour of `run`?

    Malformed blockers are skipped rather than raised: the caller assembles
    them at runtime, and a bad entry must not cost him the advice he is
    entitled to. A blocker that cannot be understood is not a commitment we
    can protect, and pretending otherwise would silently enable a prohibition
    we meant to block.
    """
    for blocker in blockers or ():
        if not isinstance(blocker, (list, tuple)) or len(blocker) < 2:
            continue
        start, end = blocker[0], blocker[1]
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        if not (isinstance(start, int) and isinstance(end, int)):
            if not (_is_number(start) and _is_number(end)):
                continue
            start, end = int(start), int(end)
        if start >= end:
            continue
        if any(start <= h < end for h in run):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def weather_advice(days, profile, blockers=None, *, now=None):
    """Actionable weather lines for today, ordered most severe first.

    `days` is the normalized payload (see _unpack) or a bare list of days.
    `blockers` is an iterable of (start_hour, end_hour, label).
    `now` is the staleness injection point for tests; it defaults to the real
    clock and is keyword-only so the call shape stays `f(days, profile,
    blockers)`.

    Returns a list of {"id", "text", "priority"} â€” exactly those keys â€” or
    [] when the module has nothing trustworthy to say. Every returned string
    is a completed verdict, never a template.

    Raises ProfileError if the profile is unusable, because advice derived
    from a profile that failed validation is advice for the wrong place. The
    caller is expected to have loaded the profile through load_profile(),
    which validates already; this re-check is cheap and closes the door on a
    hand-built dict.
    """
    if not isinstance(profile, Profile):
        problems = validate_profile(profile)
        raise ProfileError("invalid profile: " + "; ".join(problems or ["not a Profile"]))

    if not profile.weather_enabled:
        return []

    problems = validate_profile(profile)
    if problems:
        raise ProfileError("invalid profile: " + "; ".join(problems))

    day_list, fetched_raw = _unpack(days)
    if not day_list:
        return []

    # Age gate. Only enforced when the payload carries a fetch time; a bare
    # list has nothing to compare against and is trusted as given.
    if fetched_raw is not None:
        fetched = _parse_fetched_at(fetched_raw)
        if fetched is None:
            return []
        reference = _coerce_now(now)
        if reference is None:
            return []
        age = reference - fetched
        if age > timedelta(hours=profile.max_age_hours):
            return []

    # Structural completeness across EVERY day, not just day 0. A forecast
    # with a hole in it is not a forecast we should advise from, and the
    # only honest response to an incomplete one is to say nothing.
    indexes = []
    for day in day_list:
        if not isinstance(day, dict):
            return []
        index = _hour_index(day)
        if index is None:
            return []
        indexes.append(index)

    today = day_list[0]
    index = indexes[0]
    thresholds = profile.thresholds
    outdoor = profile.outdoor_windows
    wash = profile.wash_window

    lines = []
    for rule in (_eval_vis, _eval_wind):
        found = rule(index, thresholds, outdoor, wash, today)
        if found:
            lines.append(found)
    found = _eval_wash(index, thresholds, outdoor, wash, today, blockers)
    if found:
        lines.append(found)
    for rule in (_eval_umbrella, _eval_jackets):
        found = rule(index, thresholds, outdoor, wash, today)
        if found:
            lines.append(found)
    if not lines:
        found = _eval_calm(index, thresholds, outdoor, wash, today)
        if found:
            lines.append(found)

    lines.sort(key=lambda line: line["priority"])
    return lines[:max(0, profile.max_lines)]


def _coerce_now(now):
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    return _parse_fetched_at(now)


# ---------------------------------------------------------------------------
# Profile loading and validation
# ---------------------------------------------------------------------------


def _parse_window(raw, label, problems):
    """Parse ["HH:00", "HH:00"] into a Window, collecting problems."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        problems.append("%s must be a [start, end] pair, got %r" % (label, raw))
        return None
    start_raw, end_raw = raw
    hours = []
    for value in (start_raw, end_raw):
        match = _HHMM_RE.match(str(value).strip()) if value is not None else None
        if not match:
            problems.append("%s: %r is not an HH:MM time" % (label, value))
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if minute != 0:
            # Whole hours only. A half-hour boundary would silently widen or
            # narrow a window against data that is sampled on the hour, and
            # that is exactly the kind of quiet wrongness this validator is
            # for. Say so instead of guessing.
            problems.append(
                "%s: %r is not on a whole hour; windows are HH:00 to HH:00"
                % (label, value))
            return None
        if not 0 <= hour <= 24:
            problems.append("%s: %r is outside 00-24" % (label, value))
            return None
        hours.append(hour)
    if hours[0] >= hours[1]:
        problems.append("%s: start %02d:00 must be earlier than end %02d:00"
                        % (label, hours[0], hours[1]))
        return None
    return Window(hours[0], hours[1])


def _parse_windows(raw, label, problems):
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        problems.append("%s must be a list of [start, end] pairs" % label)
        return ()
    out = []
    for index, item in enumerate(raw):
        window = _parse_window(item, "%s[%d]" % (label, index), problems)
        if window is not None:
            out.append(window)
    return tuple(out)


def _check_unknown(section, value, known, problems):
    if not isinstance(value, dict):
        problems.append("%s must be a mapping" % section)
        return
    for key in value:
        if key not in known:
            problems.append(
                "unknown key %r under %s (a typo here would silently disable "
                "the feature; expected one of %s)"
                % (key, section, ", ".join(sorted(known))))


def _number(value, label, problems, band=None):
    if not _is_number(value):
        problems.append("%s must be a number, got %r" % (label, value))
        return None
    if band is not None:
        lo, hi, what = band
        if not lo <= float(value) <= hi:
            problems.append("%s must be %s in [%g, %g], got %g"
                            % (label, what, lo, hi, float(value)))
            return None
    return float(value)


def _parse_thresholds(raw, problems):
    _check_unknown("weather_advice.thresholds", raw, THRESHOLD_KEYS, problems)
    values = {}
    for key in THRESHOLD_KEYS:
        if not isinstance(raw, dict) or key not in raw:
            # No default, on purpose. A defaulted threshold is a number in the
            # code AND a number in the config; the two drift and the config
            # stops meaning what it says. Missing is loud.
            problems.append("weather_advice.thresholds.%s is missing" % key)
            continue
        parsed = _number(raw[key], "weather_advice.thresholds.%s" % key,
                         problems, _THRESHOLD_BANDS[key])
        if parsed is not None:
            values[key] = parsed
    if len(values) != len(THRESHOLD_KEYS):
        return None

    # Monotonicity. This is where the validator earns its keep: a config that
    # parses and type-checks can still be nonsense, and nonsense thresholds
    # produce advice that is confidently wrong.
    #
    # DIRECTION NOTE (read before "fixing" these two comparisons): the
    # required ordering is umbrella < wash_no, not the reverse. The silent
    # middle band is "umbrella <= pop < wash_no", so it is non-empty only
    # while umbrella is strictly below wash_no. An inverted pair does not
    # merely reshuffle advice -- it collapses the band to nothing and turns
    # every marginally-wet day into a washing prohibition, which is the exact
    # behaviour this engine exists to avoid. The shipped defaults (50 / 40)
    # satisfy the ordering below and would fail the reverse one.
    if values["umbrella"] >= values["wash_no"]:
        problems.append(
            "weather_advice.thresholds: umbrella (%g) must be BELOW wash_no (%g); "
            "otherwise the deliberate silence between them closes and every "
            "40%%-odd day becomes a washing prohibition"
            % (values["umbrella"], values["wash_no"]))
    if values["warm_strong_c"] >= values["warm_mild_c"]:
        problems.append(
            "weather_advice.thresholds: warm_strong_c (%g) must be BELOW "
            "warm_mild_c (%g); the band between them is the deliberate silence "
            "and a non-empty one means no jacket is ever advised"
            % (values["warm_strong_c"], values["warm_mild_c"]))
    return Thresholds(**values)


def validate_profile(profile_or_path) -> list:
    """Human-readable problems with a profile. Empty list means valid.

    Accepts a Profile, a raw mapping, or a path. A path that does not exist
    is itself a problem, so `--check` and a test can use the same function.

    WHY A LOUD FAILURE AND NOT A DEFAULT
    -------------------------------------
    A silently-defaulted profile yields plausible advice for the wrong place.
    This is not hypothetical: `timezone` and the windows decide which hours
    of the forecast are even in scope, and `visibility_m` and the thresholds
    decide the wording. If the profile silently fell back to a default and
    the default was wrong, the owner would read a perfectly well-formed line
    â€” "Take an umbrella - 70% chance of rain 15:00-17:59" â€” and act on it.
    He would have no way to know the numbers behind it were never his. A
    loud failure costs him one morning of weather advice. Acting on advice
    for the wrong town is a worse cost than that, and a much harder one to
    notice.
    """
    problems: list = []
    raw = profile_or_path

    if isinstance(raw, Profile):
        raw = raw.raw
    elif isinstance(raw, (str, os.PathLike)):
        try:
            raw = _read_profile_file(raw)
        except ProfileError as exc:
            return [str(exc)]

    if not isinstance(raw, dict):
        return ["profile is not a mapping"]

    # -- location ---------------------------------------------------------
    location = raw.get("location")
    if not isinstance(location, dict):
        problems.append("location must be a mapping")
    else:
        lat = _number(location.get("latitude"), "location.latitude", problems,
                      (-90.0, 90.0, "a latitude"))
        lon = _number(location.get("longitude"), "location.longitude", problems,
                      (-180.0, 180.0, "a longitude"))
        del lat, lon
        timezone = location.get("timezone")
        if not isinstance(timezone, str) or not timezone.strip():
            problems.append("location.timezone must be a non-empty string")

    # -- schedule ---------------------------------------------------------
    schedule = raw.get("schedule")
    if not isinstance(schedule, dict):
        problems.append("schedule must be a mapping")
        schedule = {}
    _check_unknown("schedule", schedule, KNOWN_KEYS["schedule"], problems)
    _parse_windows(schedule.get("outdoor_windows"), "schedule.outdoor_windows", problems)
    wash = _parse_window(schedule.get("wash_window"), "schedule.wash_window", problems)
    if wash is None:
        problems.append("schedule.wash_window is required")
    _parse_windows(schedule.get("unavailable_windows"),
                   "schedule.unavailable_windows", problems)

    # -- weather_advice ---------------------------------------------------
    weather = raw.get("weather_advice")
    if not isinstance(weather, dict):
        problems.append("weather_advice must be a mapping")
        weather = {}
    _check_unknown("weather_advice", weather, KNOWN_KEYS["weather_advice"], problems)
    if "enabled" in weather and not isinstance(weather["enabled"], bool):
        problems.append("weather_advice.enabled must be true or false")

    max_age = weather.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)
    if not isinstance(max_age, bool):
        _number(max_age, "weather_advice.max_age_hours", problems, (0.01, 168.0, "an age in hours"))
    else:
        problems.append("weather_advice.max_age_hours must be a number")

    max_lines = weather.get("max_lines", DEFAULT_MAX_LINES)
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) \
            or not 0 <= max_lines <= 50:
        problems.append("weather_advice.max_lines must be a whole number in [0, 50]")

    _parse_thresholds(weather.get("thresholds"), problems)

    # -- for_you ----------------------------------------------------------
    for_you = raw.get("for_you")
    if not isinstance(for_you, dict):
        problems.append("for_you must be a mapping")
        for_you = {}
    _check_unknown("for_you", for_you, KNOWN_KEYS["for_you"], problems)
    if "enabled" in for_you and not isinstance(for_you["enabled"], bool):
        problems.append("for_you.enabled must be true or false")

    min_length = _number(for_you.get("min_length", 0), "for_you.min_length",
                         problems, (0.0, 5000.0, "a character count"))
    max_length = _number(for_you.get("max_length"), "for_you.max_length",
                         problems, (0.0, 5000.0, "a character count"))
    if min_length is not None and max_length is not None and max_length <= min_length:
        problems.append(
            "for_you: max_length (%g) must be GREATER than min_length (%g); "
            "otherwise no story can satisfy both bounds" % (max_length, min_length))

    _number(for_you.get("min_personal_relevance", 0),
            "for_you.min_personal_relevance", problems, (0.0, 10.0, "a relevance score"))
    max_stories = for_you.get("max_stories")
    if isinstance(max_stories, bool) or not isinstance(max_stories, int) \
            or not 0 <= max_stories <= 100:
        problems.append("for_you.max_stories must be a whole number in [0, 100]")

    # Type-checked only, not membership-checked: a category this unit does not
    # recognise may legitimately be added by another unit, and a bad one there
    # costs stories, not weather advice.
    #
    # `banned_openers` and `hedge_markers` are in the same group for a harsher
    # reason: the for_you screen FAILS CLOSED without them, so an empty or
    # malformed list is not a mild annoyance, it silently deletes the entire
    # personal line on every story. This check is what makes that loud.
    for key in ("categories", "reliability_tiers",
                "banned_openers", "hedge_markers"):
        value = for_you.get(key, [])
        if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value):
            problems.append("for_you.%s must be a list of non-empty strings" % key)

    return problems


def _build_profile(raw, path) -> Profile:
    """Turn a validated raw mapping into a Profile. Assumes problems == []."""
    location = raw.get("location") or {}
    schedule = raw.get("schedule") or {}
    weather = raw.get("weather_advice") or {}
    return Profile(
        path=str(path) if path is not None else None,
        location=dict(location),
        outdoor_windows=_parse_windows(
            schedule.get("outdoor_windows"), "schedule.outdoor_windows", []),
        wash_window=_parse_window(schedule.get("wash_window"),
                                  "schedule.wash_window", []),
        unavailable_windows=_parse_windows(
            schedule.get("unavailable_windows"), "schedule.unavailable_windows", []),
        weather_enabled=weather.get("enabled", True) is not False,
        max_age_hours=float(weather.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)),
        max_lines=int(weather.get("max_lines", DEFAULT_MAX_LINES)),
        thresholds=_parse_thresholds(weather.get("thresholds"), []),
        for_you=dict(raw.get("for_you") or {}),
        raw=raw,
    )


#: Environment overrides for `location`. The committed profile carries a
#: generic default so the repository holds no personal place; a deployment
#: injects the real one from the environment. An unset variable changes
#: nothing, so a missing override is never an error.
LOCATION_ENV_KEYS = (
    ("DIGEST_PROFILE_CITY", "city", str),
    ("DIGEST_PROFILE_DISTRICT", "district", str),
    ("DIGEST_PROFILE_PROVINCE", "province", str),
    ("DIGEST_PROFILE_LAT", "latitude", float),
    ("DIGEST_PROFILE_LON", "longitude", float),
)


def apply_location_env(raw):
    """Overlay location.<key> from the environment, in place, and return raw.

    A variable that is unset, empty, or not a number where a number is required
    is skipped rather than raising: the digest must still build on the default.
    """
    location = raw.get("location")
    if not isinstance(location, dict):
        return raw
    for env_name, key, cast in LOCATION_ENV_KEYS:
        value = (os.environ.get(env_name) or "").strip()
        if not value:
            continue
        try:
            location[key] = cast(value)
        except (TypeError, ValueError):
            continue
    return raw


def load_profile(path) -> Profile:
    """Read, parse and validate a profile. Raises ProfileError on any problem.

    Never returns a defaulted profile. See validate_profile() for why.
    """
    raw = apply_location_env(_read_profile_file(path))
    problems = validate_profile(raw)
    if problems:
        raise ProfileError(
            "profile %s is invalid:\n  - %s"
            % (path, "\n  - ".join(problems)))
    return _build_profile(raw, path)


# ---------------------------------------------------------------------------
# YAML: PyYAML when present, a strict stdlib loader when not
# ---------------------------------------------------------------------------

try:  # pragma: no cover - environment dependent
    import yaml as _pyyaml
except ImportError:  # pragma: no cover
    _pyyaml = None

PARSER_NAME = ("PyYAML %s" % _pyyaml.__version__) if _pyyaml is not None \
    else "stdlib _MiniYAML"


class MiniYAMLError(ValueError):
    """Raised for a construct outside the supported YAML subset."""


_MAP_ENTRY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*):(\s.*|)$")


class _MiniYAML:
    """Indentation-driven loader for the profile.yml subset.

    Supports block mappings, block sequences, flow sequences (which is how the
    windows and the category lists are written), single/double-quoted scalars
    and full-line comments. That is the whole grammar config/profile.yml uses.

    Deliberately unsupported, and raising rather than guessing: anchors,
    aliases, tags, multi-document streams, block scalars and folded (`>`)
    scalars. A profile that needs those is a profile this loader should not be
    trusted to read silently.

    Approach borrowed from tests/workflows/__init__.py, which faced the same
    constraint â€” the suite must run on a bare interpreter with no pip install
    â€” for the same reason. Reusing the shape rather than inventing a second
    one keeps one YAML dialect in the repo.

    Scalars are returned as STRINGS here while PyYAML would return typed
    values, so _typed() below does the conversion and the two parsers agree.
    """

    def __init__(self, text: str) -> None:
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    @staticmethod
    def _indent(raw: str) -> int:
        return len(raw) - len(raw.lstrip(" "))

    @classmethod
    def _skip(cls, raw: str) -> bool:
        return raw.strip() == "" or raw.lstrip(" ").startswith("#")

    def _next_significant(self, start: int) -> int:
        i = start
        while i < len(self.lines) and self._skip(self.lines[i]):
            i += 1
        return i

    def parse(self):
        i = self._next_significant(0)
        if i >= len(self.lines):
            return {}
        value, _ = self._parse_block(self._indent(self.lines[i]), i)
        return value

    def _parse_block(self, indent: int, i: int):
        i = self._next_significant(i)
        if i >= len(self.lines):
            return None, i
        text = self.lines[i].strip()
        if text == "-" or text.startswith("- "):
            return self._parse_seq(indent, i)
        if not _MAP_ENTRY_RE.match(text):
            return _scalar(text), i + 1
        return self._parse_map(indent, i)

    def _parse_map(self, indent: int, i: int):
        out: dict = {}
        while True:
            i = self._next_significant(i)
            if i >= len(self.lines):
                break
            raw = self.lines[i]
            current = self._indent(raw)
            if current < indent:
                break
            if current > indent:
                raise MiniYAMLError(
                    "line %d: unexpected indent %d (expected %d): %r"
                    % (i + 1, current, indent, raw.strip()[:60]))
            text = raw.strip()
            if text.startswith("- "):
                raise MiniYAMLError("line %d: sequence item inside a mapping" % (i + 1))
            match = _MAP_ENTRY_RE.match(text)
            if not match:
                raise MiniYAMLError("line %d: not a mapping entry: %r"
                                    % (i + 1, text[:60]))
            key = match.group(1)
            rest = text[len(key) + 1:].strip()
            # An anchor or tag can lead the KEY or the VALUE; `a: &anchor 1`
            # is the easy one to miss and the one that would silently drop the
            # scalar on the floor.
            if key.startswith(("&", "*", "!")) or rest[:1] in ("&", "*", "!"):
                raise MiniYAMLError("line %d: anchors/aliases/tags unsupported" % (i + 1))
            if key in out:
                raise MiniYAMLError("line %d: duplicate key %r" % (i + 1, key))

            if rest == "":
                nxt = self._next_significant(i + 1)
                if nxt < len(self.lines) and self._indent(self.lines[nxt]) > indent:
                    value, i = self._parse_block(self._indent(self.lines[nxt]), nxt)
                else:
                    value, i = None, i + 1
            elif rest[0] in "|>":
                raise MiniYAMLError(
                    "line %d: block scalars are outside the supported subset"
                    % (i + 1))
            elif rest[0] == "[":
                value = _flow_seq(rest, i + 1)
                i += 1
            elif rest[0] == "{":
                raise MiniYAMLError("line %d: flow mappings are unsupported" % (i + 1))
            else:
                value = _scalar(rest)
                i += 1
            out[key] = value
        return out, i

    def _parse_seq(self, indent: int, i: int):
        out: list = []
        while True:
            i = self._next_significant(i)
            if i >= len(self.lines):
                break
            raw = self.lines[i]
            current = self._indent(raw)
            if current < indent:
                break
            if current > indent:
                raise MiniYAMLError("line %d: unexpected indent in sequence"
                                    % (i + 1))
            text = raw.strip()
            if not (text == "-" or text.startswith("- ")):
                break
            if text == "-":
                nxt = self._next_significant(i + 1)
                if nxt < len(self.lines) and self._indent(self.lines[nxt]) > indent:
                    value, i = self._parse_block(self._indent(self.lines[nxt]), nxt)
                else:
                    value, i = None, i + 1
                out.append(value)
                continue
            rest = text[2:]
            lead = len(rest) - len(rest.lstrip(" "))
            col = indent + 2 + lead
            if rest.strip()[:1] in "|>":
                raise MiniYAMLError("line %d: block scalar as a sequence item "
                                     "is unsupported" % (i + 1))
            self.lines[i] = " " * col + rest.strip()
            value, i = self._parse_block(col, i)
            out.append(value)
        return out, i


def _flow_seq(raw: str, line_no: int) -> list:
    """Parse a flow sequence, recursing into nested ones.

    Recursion is not optional here: `outdoor_windows` is written as a list of
    two-element lists, so the loader has to handle `[[a, b], [c, d]]` and not
    just `[a, b]`. A non-recursive version would hand the validator the
    STRING '["07:00", "12:00"]' where it expects a pair, and the failure would
    surface as a window that mysteriously does not validate rather than as a
    parse error.
    """
    text = raw.strip()
    if not text.endswith("]"):
        raise MiniYAMLError("line %d: unterminated flow sequence: %r" % (line_no, raw[:60]))
    inner = text[1:-1]
    items, buf, depth, quote = [], [], 0, None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        items.append("".join(buf))
    return [_flow_item(item.strip(), line_no)
            for item in items if item.strip()]


def _flow_item(raw: str, line_no: int):
    """One flow-sequence element: a nested sequence, or a scalar."""
    text = raw.strip()
    if text.startswith("["):
        return _flow_seq(text, line_no)
    if text.startswith("{"):
        raise MiniYAMLError("line %d: flow mappings are unsupported" % line_no)
    return _scalar(text)


def _strip_comment(raw: str) -> str:
    quote = None
    for idx, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch == "#" and idx > 0 and raw[idx - 1] in " \t":
            return raw[:idx].rstrip()
    return raw.rstrip()


def _unescape_double(raw: str) -> str:
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            out.append(simple.get(raw[i + 1], "\\" + raw[i + 1]))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _scalar(raw: str):
    """A YAML scalar as a Python value, matching PyYAML 1.1 for our subset."""
    text = _strip_comment(str(raw).strip())
    if text[:1] in "'\"" and text[-1:] == text[:1] and len(text) >= 2:
        if text[0] == "'":
            return text[1:-1].replace("''", "'")
        return _unescape_double(text[1:-1])
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _read_profile_file(path) -> dict:
    """Parse a profile YAML file into a mapping. Raises ProfileError."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ProfileError("cannot read profile %s: %s" % (path, exc.strerror or exc))

    try:
        if _pyyaml is not None:
            raw = _pyyaml.safe_load(text)
        else:
            raw = _MiniYAML(text).parse()
    except Exception as exc:  # parser dialect, not our policy
        raise ProfileError("cannot parse profile %s: %s" % (path, exc))

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProfileError("profile %s must be a mapping at the top level" % path)
    return raw


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="weather_advice.py",
        description="Validate a profile and run the deterministic weather rules.")
    parser.add_argument("--check", metavar="PATH",
                        help="validate the profile at PATH; exit 1 on any problem")
    args = parser.parse_args(argv)

    if not args.check:
        parser.print_help()
        return 2

    try:
        profile = load_profile(args.check)
    except ProfileError as exc:
        print("INVALID %s" % args.check)
        for line in str(exc).splitlines():
            print(line)
        return 1
    print("OK %s" % profile.path)
    print("  parser      : %s" % PARSER_NAME)
    print("  place       : %s (%s, %s)"
          % (profile.location.get("city"), profile.location.get("latitude"),
             profile.location.get("timezone")))
    print("  outdoor     : %s" % ", ".join(
        "%02d:00-%02d:00" % (w.start_hour, w.end_hour)
        for w in profile.outdoor_windows))
    print("  wash window : %02d:00-%02d:00"
          % (profile.wash_window.start_hour, profile.wash_window.end_hour))
    print("  thresholds  : wash_no=%s umbrella=%s precip_wet_mm=%s "
          "warm_strong=%s warm_mild=%s wind=%s visibility=%s"
          % (_fmt_num(profile.thresholds.wash_no),
             _fmt_num(profile.thresholds.umbrella),
             _fmt_num(profile.thresholds.precip_wet_mm),
             _fmt_num(profile.thresholds.warm_strong_c),
             _fmt_num(profile.thresholds.warm_mild_c),
             _fmt_num(profile.thresholds.wind_kmh),
             _fmt_num(profile.thresholds.visibility_m)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
