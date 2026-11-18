"""Taking down what callers dictate: phone numbers and email addresses.

A real test call showed three ways this went wrong when the model did it:

- It cut in on every pause. Callers dictate in pieces ("plus four nine" ...
  "one five two" ...), and each pause ended the turn, so the model started a
  sentence ("Could...") and was talked over.
- It assembled the pieces itself and dropped a digit, then read the result
  back as one big number ("+49 152 148672") that nobody can check by ear.
- It never understood "at the rate" (a common way to say "@"), and guessed a
  different wrong address five times in a row.

So the model only collects the caller's words. This module turns them into a
number or an address, decides whether the caller is still mid-dictation, and
produces a read-back a caller can actually check: digit by digit, letter by
letter, in the call's language.
"""

from __future__ import annotations

import re

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop import BaseUserTurnStopStrategy

# --- Phone numbers ----------------------------------------------------------

_DIGIT_WORDS = {
    # English
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    # German
    "null": "0",
    "eins": "1",
    "zwei": "2",
    "zwo": "2",
    "drei": "3",
    "vier": "4",
    "fünf": "5",
    "fuenf": "5",
    "sechs": "6",
    "sieben": "7",
    "acht": "8",
    "neun": "9",
}
_TEENS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "zwoelf": 12,
    "dreizehn": 13,
    "vierzehn": 14,
    "fünfzehn": 15,
    "fuenfzehn": 15,
    "sechzehn": 16,
    "siebzehn": 17,
    "achtzehn": 18,
    "neunzehn": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "zwanzig": 20,
    "dreißig": 30,
    "dreissig": 30,
    "vierzig": 40,
    "fünfzig": 50,
    "fuenfzig": 50,
    "sechzig": 60,
    "siebzig": 70,
    "achtzig": 80,
    "neunzig": 90,
}
_REPEAT = {"double": 2, "doppel": 2, "doppelt": 2, "triple": 3, "dreifach": 3}
# German callers often say pairs: "vierundzwanzig" is 24.
_GERMAN_PAIR = re.compile(
    r"^(ein|zwei|drei|vier|fünf|fuenf|sechs|sieben|acht|neun)und"
    r"(zwanzig|dreißig|dreissig|vierzig|fünfzig|fuenfzig|sechzig|siebzig|achtzig|neunzig)$"
)
_PAIR_UNITS = {"ein": 1, **{k: int(v) for k, v in _DIGIT_WORDS.items() if v != "0"}}


def parse_spoken_phone(spoken: str) -> str:
    """Turn dictated pieces into a phone number: digits, with "+" if said.

    "plus four nine, one five two, one nine. four eight six seven two"
    -> "+491521948672". Words that are not numbers ("my number is") are
    ignored. Returns "" when no digits were heard.
    """
    text = (spoken or "").lower().replace("-", " ")
    tokens = re.findall(r"\+|\d+|[^\W\d_]+", text)

    out: list[str] = []
    plus = False
    repeat = 1
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("+", "plus") and not out:
            plus = True
        elif token.isdigit():
            out.append(token * repeat if len(token) == 1 else token)
            repeat = 1
        elif token in _REPEAT:
            repeat = _REPEAT[token]
        elif token == "oh":
            # "oh" is zero only inside a number ("oh one seven two"), not the
            # interjection in "Oh, okay, plus four nine...".
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if out or nxt.isdigit() or (nxt in _DIGIT_WORDS and nxt != "oh"):
                out.append("0" * repeat)
                repeat = 1
        elif token in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[token] * repeat)
            repeat = 1
        elif token in _TEENS:
            out.append(str(_TEENS[token]))
        elif token in _TENS:
            value = _TENS[token]
            # "forty eight" -> 48; "forty" alone -> 40.
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt in _DIGIT_WORDS and _DIGIT_WORDS[nxt] != "0":
                out.append(str(value + int(_DIGIT_WORDS[nxt])))
                i += 1
            else:
                out.append(str(value))
        else:
            pair = _GERMAN_PAIR.match(token)
            if pair:
                out.append(str(_TENS[pair.group(2)] + _PAIR_UNITS[pair.group(1)]))
        i += 1

    digits = "".join(out)
    if digits.startswith("00"):
        plus, digits = True, digits[2:]
    return ("+" if plus else "") + digits if digits else ""


def phone_looks_complete(number: str) -> bool:
    """Whether a dictated number is long enough that the caller is probably done.

    German mobiles are 0 + 10-11 digits, or +49 + 10-11; Hamburg landlines
    are 040 + 6-8. Short of that, the caller is most likely pausing between
    groups, and the right reply is "mhm", not a read-back.
    """
    digits = number.lstrip("+")
    if number.startswith("+49"):
        return len(digits) >= 12
    if number.startswith("+"):
        return len(digits) >= 10
    if digits.startswith("040"):
        return len(digits) >= 9
    if digits.startswith("0"):
        return len(digits) >= 11
    return len(digits) >= 10


def phone_read_back(number: str) -> str:
    """Digits spaced out and grouped, so the voice reads them one at a time.

    "+491521948672" -> "+4 9, 1 5 2, 1 9 4, 8 6 7 2". Spaced digits are read
    as single digits in whatever language the voice is set to, which a big
    number ("one hundred fifty-two million...") is not.
    """
    plus = number.startswith("+")
    digits = number.lstrip("+")
    groups: list[str] = []
    prefix = ""
    if plus and digits.startswith("49"):
        groups.append("+4 9")
        digits = digits[2:]
    elif plus:
        prefix = "+"
    while digits:
        # Threes, but never strand a single digit at the end.
        size = 4 if len(digits) in (4, 7) else 3
        groups.append(" ".join(digits[:size]))
        digits = digits[size:]
    if prefix and groups:
        groups[0] = prefix + groups[0]
    return ", ".join(groups)


# --- Email addresses --------------------------------------------------------

# Longest first, so "at the rate of" wins over "at".
_EMAIL_AT = (
    "at the rate of",
    "at the rate",
    "at sign",
    "at symbol",
    "at-zeichen",
    "at zeichen",
    "atzeichen",
    "klammeraffe",
    "ät",
    "aet",
    "at",
)
_EMAIL_DOT = ("dot", "punkt", "period", "point")
_EMAIL_DASH = ("dash", "hyphen", "minus", "bindestrich")
_EMAIL_UNDERSCORE = ("underscore", "unterstrich")
# Said before the address itself; only stripped from the start.
_EMAIL_LEAD_FILLER = (
    "no",
    "nope",
    "nein",
    "sorry",
    "it's",
    "its",
    "it is",
    "my email is",
    "my email address is",
    "my email",
    "the email is",
    "meine e-mail ist",
    "meine email ist",
    "meine e-mail-adresse ist",
    "die adresse ist",
    "das ist",
    "es ist",
    "also",
    "okay",
    "ok",
)


def _replace_words(text: str, words: tuple[str, ...], symbol: str) -> str:
    for word in sorted(words, key=len, reverse=True):
        text = re.sub(rf"(?<![\w]){re.escape(word)}(?![\w])", f" {symbol} ", text)
    return text


def normalize_spoken_email(raw: str) -> str | None:
    """Turn a dictated address into a real one, or None if it is not usable.

    "anas punkt rabbani at gmail punkt com" -> "anas.rabbani@gmail.com"
    "a n a s dot rabbani at the rate gmail dot com" -> "anas.rabbani@gmail.com"

    Returning None matters as much as parsing: a confirmation sent to a
    misheard address is worse than none, because the caller believes it is on
    its way.
    """
    text = (raw or "").strip().lower()
    # "No. It's ..." -> strip each leading filler in turn.
    changed = True
    while changed:
        changed = False
        text = text.lstrip(" .,;:!?…")
        for filler in sorted(_EMAIL_LEAD_FILLER, key=len, reverse=True):
            if re.match(rf"{re.escape(filler)}(?=[\s.,;:!?…]|$)", text):
                text = text[len(filler) :]
                changed = True
                break

    # Speech-to-text sometimes half-converts "at the rate gmail" itself, into
    # "@theredgmail" or "@the rate gmail".
    text = re.sub(r"@\s*the\s*r(?:ate|ed|at|et|eight)\s*(?:of\s*)?", "@", text)
    text = _replace_words(text, _EMAIL_AT, "@")
    text = _replace_words(text, _EMAIL_DOT, ".")
    text = _replace_words(text, _EMAIL_DASH, "-")
    text = _replace_words(text, _EMAIL_UNDERSCORE, "_")

    # Spelled letters arrive spaced and with commas; an address has neither.
    candidate = re.sub(r"[\s,;:!?'\"…]+", "", text).strip(".")
    candidate = re.sub(r"\.{2,}", ".", candidate).replace("@@", "@")
    candidate = candidate.replace(".@", "@").replace("@.", "@")

    if not re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", candidate):
        return None
    return candidate


def email_looks_incomplete(raw: str) -> bool:
    """Whether the caller has not reached the end of the address yet.

    No "@" yet, or nothing after it that ends like a domain: the caller is
    still going, and the right reply is "mhm".
    """
    text = (raw or "").lower()
    text = re.sub(r"@\s*the\s*r(?:ate|ed|at|et|eight)\s*(?:of\s*)?", "@", text)
    text = _replace_words(text, _EMAIL_AT, "@")
    text = _replace_words(text, _EMAIL_DOT, ".")
    if "@" not in text:
        return True
    domain = re.sub(r"\s+", "", text.split("@", 1)[1]).strip(".,;:!?…")
    return not re.search(r"\.[a-z]{2,}$", domain)


# Words for the symbols in an address, per language. Spoken between spelled
# letters, so the caller hears "A N A S, Punkt, R A B ..." in their language.
EMAIL_WORDS: dict[str, dict[str, str]] = {
    "de": {".": "Punkt", "@": "ät", "-": "Minus", "_": "Unterstrich"},
    "en": {".": "dot", "@": "at", "-": "dash", "_": "underscore"},
    "tr": {".": "nokta", "@": "et", "-": "tire", "_": "alt çizgi"},
    "ar": {".": "نقطة", "@": "آت", "-": "شرطة", "_": "شرطة سفلية"},
    "ru": {".": "точка", "@": "собака", "-": "дефис", "_": "нижнее подчёркивание"},
    "uk": {".": "крапка", "@": "равлик", "-": "дефіс", "_": "нижнє підкреслення"},
    "pl": {".": "kropka", "@": "małpa", "-": "myślnik", "_": "podkreślnik"},
    "ro": {".": "punct", "@": "a rond", "-": "cratimă", "_": "liniuță jos"},
    "es": {".": "punto", "@": "arroba", "-": "guion", "_": "guion bajo"},
    "fr": {".": "point", "@": "arobase", "-": "tiret", "_": "tiret bas"},
    "it": {".": "punto", "@": "chiocciola", "-": "trattino", "_": "trattino basso"},
    "pt": {".": "ponto", "@": "arroba", "-": "hífen", "_": "sublinhado"},
}


def email_read_back(email: str, language: str) -> str:
    """The address as a caller can check it by ear.

    The part before "@" is where speech-to-text goes wrong, so it is spelled
    letter by letter (capitals, so the voice says the letter's name rather
    than reading "a" as a word). The domain is read as words: "gmail Punkt com".
    """
    words = EMAIL_WORDS.get(language, EMAIL_WORDS["en"])
    local, _, domain = email.partition("@")

    spelled: list[str] = []
    for chunk in re.split(r"([._-])", local):
        if not chunk:
            continue
        spelled.append(words[chunk] if chunk in words else " ".join(chunk.upper()))
    domain_spoken = f" {words['.']} ".join(domain.split("."))
    return f"{', '.join(spelled)}, {words['@']}, {domain_spoken}"


# --- Turn-taking while dictating -------------------------------------------


class DictationAwareMinWords(MinWordsUserTurnStartStrategy):
    """Interrupt threshold that drops to one word while the caller dictates.

    Normally the caller must say two words to stop the bot mid-sentence, so
    "mhm" and "ja" do not cut it off. But a word below the threshold is not
    just ignored, it is discarded -- and while dictating, the next word ("dot",
    "neun") often lands while the bot is still saying its "mhm". Losing it
    corrupts the address or number, so during dictation every word counts.
    """

    def __init__(self, *, session, min_words: int, **kwargs):
        self._session = session
        self._base_min_words = min_words
        super().__init__(min_words=min_words, **kwargs)

    @property
    def _min_words(self) -> int:
        return 1 if getattr(self._session, "dictating", None) else self._base_min_words

    @_min_words.setter
    def _min_words(self, value: int) -> None:
        self._base_min_words = value


# --- Did the caller actually say it? ----------------------------------------


class HeardRecorder(FrameProcessor):
    """Keeps the caller's last few utterances, exactly as speech-to-text heard them.

    In a test call the model, asked to take down an email, invented one
    ("atest@example.com") and had it read back before the caller had said a
    word. Comparing what a tool is handed against what was actually heard
    stops that.
    """

    def __init__(self, session, keep: int = 12, **kwargs):
        super().__init__(**kwargs)
        self._session = session
        self._keep = keep
        session.heard = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self._session.heard.append(frame.text)
            del self._session.heard[: -self._keep]
        await self.push_frame(frame, direction)


def _compact(text: str) -> str:
    """Letters and digits only, with spoken "dot"/"at" words removed."""
    text = _replace_words(text.lower(), _EMAIL_AT + _EMAIL_DOT, " ")
    return re.sub(r"[^a-z0-9äöüß]", "", text)


def email_was_heard(email: str, heard: list[str] | None) -> bool:
    """Whether the part before "@" appears in what the caller recently said.

    None means nothing records what was heard (tests, or a pipeline without
    the recorder): the check is skipped rather than failing every address.
    """
    if heard is None:
        return True
    local = re.sub(r"[^a-z0-9]", "", email.split("@", 1)[0])
    return bool(local) and local in _compact(" ".join(heard))


def phone_was_heard(number: str, heard: list[str] | None) -> bool:
    """Whether the number's digits appear in what the caller recently said."""
    if heard is None:
        return True
    digits = number.lstrip("+")
    return bool(digits) and digits in parse_spoken_phone(" ".join(heard)).lstrip("+")


# --- Ending the caller's turn sooner -----------------------------------------

# Replies that are complete the moment they are said. Negations are left out on
# purpose: "no" is very often followed by the correction ("no, it's 0172...").
_SHORT_REPLY_WORDS = frozenset(
    """
    yes yeah yep yup ok okay sure right correct exactly perfect great fine
    thanks thank you too bye goodbye cheers alright that's that is it sounds good
    ja jo jap jawohl genau richtig stimmt passt gut gerne gern danke schön sehr
    vielen dank tschüss tschüs ciao wiederhören wiedersehen auf alles klar super
    evet tamam teşekkürler sağ ol görüşürüz
    да хорошо спасибо пока
    так добре дякую
    tak dobrze dziękuję
    da bine mulțumesc
    sí si vale gracias adiós
    oui merci d'accord au revoir
    sì grazie bene
    sim obrigado obrigada tchau
    نعم شكرا تمام
    """.split()
)


def is_short_reply(text: str) -> bool:
    """A short reply that is complete as said: "Yes.", "Danke schön!", "You too. Bye."."""
    words = re.findall(r"[^\W\d_][^\W\d_'’]*", text.lower())
    return 0 < len(words) <= 5 and all(word in _SHORT_REPLY_WORDS for word in words)


class ShortReplyUserTurnStopStrategy(BaseUserTurnStopStrategy):
    """Ends the caller's turn at once on a short, complete reply.

    The turn model is often unsure about one-word answers and then waits its
    full timeout: in a test call "You too. Bye." took 2.8 seconds to answer.
    A "yes" or a "thanks" needs no waiting at all. Off while the caller is
    dictating, where a short word is usually followed by more.
    """

    def __init__(self, *, session, **kwargs):
        super().__init__(**kwargs)
        self._session = session

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if (
            isinstance(frame, TranscriptionFrame)
            and not getattr(self._session, "dictating", None)
            and is_short_reply(frame.text)
        ):
            await self.trigger_user_turn_stopped()
        return ProcessFrameResult.CONTINUE
