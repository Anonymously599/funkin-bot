"""
fnf_song.py  —  FNF Chart Parser
Supports: Vanilla FNF .json, Psych Engine .json, P-Slice .json, .fnfc (zip),
          .zip chart packs, and the newer FunkinCrew multi-difficulty .json format.
"""

import json
import zipfile
import os
from dataclasses import dataclass, field
from typing import List, Optional, Set


# ── Lane convention ─────────────────────────────────────────────────────
#
# Lane ownership depends on whether the chart has been through Psych
# Engine's Song.hx convert() step, which is signaled by a top-level
# "format": "psych_v1_convert" field:
#
#   CONVERTED  (format == "psych_v1_convert"):
#       Ownership is ABSOLUTE — raw lane 0-3 is always the player, 4-7 is
#       always the opponent. mustHitSection is not consulted. This also
#       matches P-Slice's own generateSong():
#           var gottaHitNote:Bool = (songNotes[1] < totalColumns); // totalColumns = 4
#
#   UNCONVERTED  (no "format" field, i.e. a raw/original Psych Engine
#   chart straight out of the editor):
#       Ownership follows Psych's own generateSong() SWAP rule instead:
#           gottaHitNote = section.mustHitSection;
#           if (songNotes[1] > 3) gottaHitNote = !section.mustHitSection;
#       i.e. raw lanes 0-3 belong to whichever side mustHitSection points
#       to, and raw lanes 4-7 belong to the OTHER side.
#
# Using the wrong rule for a given file silently swaps which notes are
# treated as the player's — it does not raise an error, so this check
# must run before ownership is decided for every note.


# ── Note type classification ──────────────────────────────────────────
#
# HARMFUL — skip these completely (they damage/kill the player):
HARMFUL_NOTE_TYPES: Set[str] = {
    "missnote", "miss note", "void note", "mine", "hurt note",
    "hurt", "death note", "bomb",
}

# OPPONENT MARKER — skip notes whose type contains this substring
# (used by mods like Double Trouble where a 2nd opponent uses lanes 4-7
#  but tags those notes so the parser knows they aren't for the player)
OPPONENT_MARKER = "opponent"

# COSMETIC — these change animation only; the note itself must still be clicked
COSMETIC_NOTE_TYPES: Set[str] = {
    "alt animation", "no animation", "no sing",
    "trail note", "trail note alt",
}

def classify_note_type(raw_type: str):
    """
    Returns one of:
      'harmful'   — skip, damages player
      'opponent'  — skip, belongs to opponent strumline
      'cosmetic'  — click normally (animation-only change)
      'normal'    — plain note, click normally
    """
    if not raw_type:
        return "normal"
    t = raw_type.lower().strip()
    if t in HARMFUL_NOTE_TYPES:
        return "harmful"
    if OPPONENT_MARKER in t:
        return "opponent"
    if t in COSMETIC_NOTE_TYPES:
        return "cosmetic"
    return "normal"


@dataclass
class FNFNote:
    time:        float
    lane:        int
    hold_length: float
    raw_lane:    int
    note_type:   str = ""     # raw string from the chart, e.g. "Trail Note", "MissNote"

    @property
    def type_class(self) -> str:
        """'normal', 'cosmetic', 'harmful', or 'opponent'."""
        return classify_note_type(self.note_type)

    @property
    def is_playable(self) -> bool:
        """True if the bot should press this note (normal + cosmetic)."""
        return self.type_class in ("normal", "cosmetic")

    @property
    def is_harmful(self) -> bool:
        return self.type_class == "harmful"

    def __repr__(self):
        NAMES = ["LEFT", "DOWN", "UP", "RIGHT"]
        hold  = "  hold={:.0f}ms".format(self.hold_length) if self.hold_length > 0 else ""
        typ   = "  [{}]".format(self.note_type) if self.note_type else ""
        return "Note({} @{:.0f}ms{}{})".format(NAMES[self.lane % 4], self.time, hold, typ)


@dataclass
class FNFSection:
    notes:            List[FNFNote] = field(default_factory=list)
    # Consulted for note-ownership decisions ONLY when the chart is
    # unconverted (no "format": "psych_v1_convert" tag) — see the
    # module docstring above for the two ownership rules.
    must_hit_section: bool          = True


@dataclass
class FNFSong:
    song_name: str              = "Unknown"
    bpm:       float            = 100.0
    sections:  List[FNFSection] = field(default_factory=list)
    speed:     float            = 1.0

    def all_player_notes(self) -> List[FNFNote]:
        """Collect, sort, and deduplicate ALL player notes (includes harmful)."""
        raw = []
        for sect in self.sections:
            raw.extend(sect.notes)
        raw.sort(key=lambda n: (n.time, n.lane))
        deduped: List[FNFNote] = []
        for note in raw:
            if deduped and deduped[-1].lane == note.lane \
                    and abs(deduped[-1].time - note.time) < 5:
                continue
            deduped.append(note)
        return deduped

    def playable_notes(self, skip_harmful: bool = True) -> List[FNFNote]:
        """
        Returns notes the bot should actually press.
          skip_harmful=True  — skip Mine/MissNote/Void/Hurt/etc. (default, recommended)
          skip_harmful=False — click everything including harmful types
        Opponent-tagged notes are ALWAYS skipped regardless of the setting.
        """
        all_notes = self.all_player_notes()
        result = []
        for n in all_notes:
            tc = n.type_class
            if tc == "opponent":
                continue                  # always skip opponent-tagged notes
            if skip_harmful and tc == "harmful":
                continue                  # skip mines/missnotes when setting is on
            result.append(n)
        return result

    def all_note_types(self) -> List[str]:
        """Sorted list of unique non-empty note types in this chart."""
        types = set()
        for sect in self.sections:
            for note in sect.notes:
                if note.note_type:
                    types.add(note.note_type)
        return sorted(types)


# ── Helpers ───────────────────────────────────────────────────────────
def _sf(val, default=0.0) -> float:
    try:
        if val is None: return default
        return float(val)
    except (TypeError, ValueError):
        return default

def _si(val, default=0) -> int:
    try:
        if val is None: return default
        return int(float(val))
    except (TypeError, ValueError):
        return default


# ── Parser ────────────────────────────────────────────────────────────
class ChartParser:

    @staticmethod
    def load(path: str, difficulty: Optional[str] = None) -> FNFSong:
        if not os.path.exists(path):
            raise FileNotFoundError("File not found: {}".format(path))
        ext = os.path.splitext(path)[1].lower()
        if ext in (".fnfc", ".zip"):
            return ChartParser._load_zip(path, difficulty)
        elif ext == ".json":
            return ChartParser._load_json_file(path, difficulty)
        else:
            try:
                return ChartParser._load_json_file(path, difficulty)
            except Exception:
                raise ValueError("Unsupported file format: {}".format(ext))

    @staticmethod
    def _load_zip(path: str, difficulty: Optional[str] = None) -> FNFSong:
        if not zipfile.is_zipfile(path):
            raise ValueError("'{}' is not a valid ZIP/FNFC archive.".format(
                os.path.basename(path)))
        with zipfile.ZipFile(path, "r") as zf:
            names      = zf.namelist()
            json_names = [n for n in names if n.lower().endswith(".json")]
            if not json_names:
                raise ValueError("No .json found in archive.\nContents: {}".format(
                    ", ".join(names[:10])))
            PRIORITY = ["hard.json","normal.json","easy.json",
                        "erect.json","nightmare.json","chart.json","song.json"]
            chart_file = None
            for want in PRIORITY:
                for n in json_names:
                    if os.path.basename(n).lower() == want:
                        chart_file = n; break
                if chart_file: break
            if not chart_file:
                best = -1
                for n in json_names:
                    try:
                        with zf.open(n) as f:
                            raw  = f.read().decode("utf-8-sig", errors="replace")
                            data = json.loads(raw)
                        cnt = ChartParser._count_notes(data)
                        if cnt > best:
                            best = cnt; chart_file = n
                    except Exception:
                        pass
            if not chart_file:
                chart_file = json_names[0]
            try:
                with zf.open(chart_file) as f:
                    raw  = f.read().decode("utf-8-sig", errors="replace")
                    data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError("Invalid JSON in '{}': {}".format(chart_file, e))
        return ChartParser._parse(data, os.path.basename(path), difficulty)

    @staticmethod
    def _count_notes(data) -> int:
        try:
            if isinstance(data, dict) and isinstance(data.get("song"), dict):
                data = data["song"]
            return sum(len(s.get("sectionNotes",[]))
                       for s in data.get("notes",[]) if isinstance(s, dict))
        except Exception:
            return 0

    @staticmethod
    def _load_json_file(path: str, difficulty: Optional[str] = None) -> FNFSong:
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                raw = f.read().strip()
        except OSError as e:
            raise ValueError("Cannot read file: {}".format(e))
        if not raw:
            raise ValueError("Chart file is empty.")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError("Not valid JSON: {}".format(e))
        return ChartParser._parse(data, os.path.basename(path), difficulty)

    @staticmethod
    def _parse(data, filename: str, difficulty: Optional[str] = None) -> FNFSong:
        if isinstance(data, str):
            try:   data = json.loads(data)
            except Exception:
                raise ValueError("Chart data is a plain string, not a JSON object.")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object, got {}.".format(type(data).__name__))

        inner = data.get("song")
        if isinstance(inner, dict):
            data = inner

        notes_field = data.get("notes")

        # NEW FORMAT: "notes" is a dict of {difficulty: [note, ...]}
        if isinstance(notes_field, dict):
            return ChartParser._parse_v2(data, filename, notes_field, difficulty)

        # ── OLD FORMAT: "notes" is a list of sections ──────────────────
        #
        # Ownership rule is decided ONCE per file, from the top-level
        # "format" field — see module docstring:
        #   "psych_v1_convert" present → converted  → ABSOLUTE rule
        #   field absent               → unconverted → SWAP rule
        is_converted = data.get("format") == "psych_v1_convert"

        song           = FNFSong()
        raw_name       = data.get("song")
        song.song_name = str(raw_name) if raw_name else \
                         filename.replace(".json","").replace(".fnfc","")
        song.bpm       = _sf(data.get("bpm"),   100.0) or 100.0
        song.speed     = _sf(data.get("speed"),   1.0) or 1.0

        if not isinstance(notes_field, list):
            return song

        for sd in notes_field:
            if not isinstance(sd, dict):
                continue
            sect     = FNFSection()
            mhs      = sd.get("mustHitSection")
            sect.must_hit_section = bool(mhs) if mhs is not None else True
            raw_notes = sd.get("sectionNotes")
            if not isinstance(raw_notes, list):
                song.sections.append(sect)
                continue
            for nd in raw_notes:
                note = ChartParser._parse_note(nd, sect.must_hit_section, is_converted)
                if note is not None:
                    sect.notes.append(note)
            song.sections.append(sect)
        return song

    @staticmethod
    def _parse_v2(data, filename: str, notes_by_diff: dict,
                   difficulty: Optional[str] = None) -> FNFSong:
        """
        Newer chart format (FunkinCrew engine, v2.x): all difficulties share
        one file. Each note is {"t": ms, "d": 0-7, "l": hold_ms, "k": kind}.
        This format is always post-conversion, so ownership is always
        ABSOLUTE: d < 4 is the PLAYER, d >= 4 is the OPPONENT (lane = d % 4).
        """
        available = [k for k, v in notes_by_diff.items() if isinstance(v, list)]

        if difficulty and difficulty in notes_by_diff:
            chosen = difficulty
        elif "normal" in notes_by_diff:
            chosen = "normal"
        elif available:
            chosen = available[0]
        else:
            chosen = None

        song = FNFSong()
        song.song_name = filename.replace(".json", "").replace(".fnfc", "")
        song.bpm       = _sf(data.get("bpm"), 100.0) or 100.0

        speeds = data.get("scrollSpeed")
        song.speed = _sf(speeds.get(chosen), 1.0) if isinstance(speeds, dict) and chosen in speeds else 1.0

        section = FNFSection(must_hit_section=True)
        raw_notes = notes_by_diff.get(chosen, []) if chosen else []

        for nd in raw_notes:
            if not isinstance(nd, dict):
                continue
            t    = _sf(nd.get("t"))
            d    = _si(nd.get("d"))
            hold = _sf(nd.get("l"), 0.0)
            kind = nd.get("k") or ""

            if t < 0 or d < 0 or d >= 8:
                continue
            if d >= 4:
                continue   # opponent note — discard

            section.notes.append(FNFNote(
                time=t,
                lane=d % 4,
                hold_length=max(0.0, hold),
                raw_lane=d,
                note_type=str(kind).strip(),
            ))

        song.sections.append(section)
        return song

    @staticmethod
    def list_difficulties(path: str) -> List[str]:
        """Returns difficulty names if this is the new multi-difficulty
        format, or [] for the old format (nothing to choose)."""
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                data = json.loads(f.read())
            if isinstance(data, dict):
                inner = data.get("song")
                if isinstance(inner, dict):
                    data = inner
                notes_field = data.get("notes")
                if isinstance(notes_field, dict):
                    return [k for k, v in notes_field.items() if isinstance(v, list)]
        except Exception:
            pass
        return []

    @staticmethod
    def _parse_note(nd, must_hit_section: bool, is_converted: bool) -> Optional[FNFNote]:
        if not isinstance(nd, (list, tuple)) or len(nd) < 2:
            return None
        try:
            t    = _sf(nd[0])
            rl   = _si(nd[1])
            hold = _sf(nd[2]) if len(nd) > 2 else 0.0
            # index 3 = note type string (Psych Engine / most mods)
            raw_type  = nd[3] if len(nd) > 3 else ""
            note_type = str(raw_type).strip() if raw_type is not None else ""
        except Exception:
            return None

        if t < 0 or rl < 0 or rl >= 100:
            return None

        # ── OPPONENT NOTE DETECTION ───────────────────────────────────
        # See module docstring for the two rules. Which one applies is
        # decided once per file (is_converted), not per note.
        lane = rl % 4
        if is_converted:
            # CONVERTED chart ("psych_v1_convert"): ownership is absolute.
            is_player = rl < 4
        else:
            # UNCONVERTED chart (no "format" tag): swap rule.
            #   raw lane 0-3 → owned by whichever side mustHitSection points to
            #   raw lane 4-7 → owned by the OTHER side
            if rl < 4:
                is_player = must_hit_section
            else:
                is_player = not must_hit_section

        if not is_player:
            return None   # opponent note — discard entirely

        # ── OPPONENT-TAGGED NOTE ──────────────────────────────────────
        # Some mods (e.g. Double Trouble) put notes in player lanes but tag
        # them with a type containing "opponent" to mark them as a second
        # opponent's notes. We keep them in the song but mark them so
        # playable_notes() can filter them out.
        # (We do NOT discard here so the GUI can still show/count them.)

        return FNFNote(
            time=t,
            lane=lane,
            hold_length=max(0.0, hold),
            raw_lane=rl,
            note_type=note_type,
        )
        