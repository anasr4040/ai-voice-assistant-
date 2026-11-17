"""Which language the call is in -- decided by code, not guessed by the model.

Before this module, three things each had their own idea of the language:

- the speech-to-text heard every language (Deepgram ``multi``),
- the model was told by the prompt to "stay German unless...", and
- the voice was hard-wired to German (``language="de"``).

So when a caller switched to English, the model answered in English and the
German-locked voice read it with a German accent and German number rules. And
when speech-to-text misheard "ja" as "yeah", the model sometimes flipped to
English on its own. Both are fixed by making one object -- :class:`LanguagePolicy`
-- own the decision, and having it drive the model AND the voice together.

How a switch is decided, from what Deepgram reports per word:

- One or two stray words ("okay", "yes", a name) never switch.
- A clear sentence (``STRONG_WORDS`` or more) in another language switches now.
- Two shorter utterances in a row in the same other language also switch.
- The caller explicitly asking ("Can we speak English?") switches via the
  ``switch_language`` tool and pins the language for the rest of the call.
- Only languages in ``SUPPORTED`` are ever switched to. Each one needs a voice
  that speaks it and the fixed lines in ``PHRASES`` -- add both to add a
  language.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMUpdateSettingsFrame,
    TranscriptionFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import LLMSettings, TTSSettings
from pipecat.transcriptions.language import Language

BASE_LANGUAGE = "de"

# Words in one utterance that are enough to switch on their own. Five, not
# three: speech-to-text turns short German ("Das ist Klasse B") into plausible
# English ("This is class B") often enough that four words is not proof.
STRONG_WORDS = 5

# Below this, an utterance is ignored entirely for switching purposes.
MIN_WORDS = 2

# Languages the receptionist will switch to, with their German name for the
# prompt. All are heard by Deepgram nova-3 multi and spoken by ElevenLabs
# flash v2.5 (tests/test_language.py checks the second). Hamburg's most common
# non-German callers are covered; Dutch is left out on purpose because phone
# audio of German is misdetected as Dutch more than any other pair.
SUPPORTED: dict[str, str] = {
    "de": "Deutsch",
    "en": "Englisch",
    "tr": "Türkisch",
    "ar": "Arabisch",
    "ru": "Russisch",
    "uk": "Ukrainisch",
    "pl": "Polnisch",
    "ro": "Rumänisch",
    "es": "Spanisch",
    "fr": "Französisch",
    "it": "Italienisch",
    "pt": "Portugiesisch",
}

# Lines spoken without the model -- when it is unreachable, or around a
# transfer. They must exist in every supported language, because the voice is
# set to the caller's language and would mangle a German sentence.
PHRASES: dict[str, dict[str, str]] = {
    # After a technical error nothing happens until the caller speaks again, so
    # this asks them to repeat rather than promising an answer that never comes.
    "fallback": {
        "de": "Entschuldigung, das ist gerade untergegangen. Können Sie das bitte noch einmal sagen?",
        "en": "Sorry, I missed that. Could you say it again, please?",
        "tr": "Kusura bakmayın, bunu kaçırdım. Lütfen tekrar söyler misiniz?",
        "ar": "عذراً، فاتني ذلك. هل يمكنك أن تعيد ما قلته من فضلك؟",
        "ru": "Извините, я не расслышала. Повторите, пожалуйста.",
        "uk": "Вибачте, я не розчула. Повторіть, будь ласка.",
        "pl": "Przepraszam, umknęło mi to. Czy mogę prosić o powtórzenie?",
        "ro": "Scuzați-mă, nu am prins asta. Puteți repeta, vă rog?",
        "es": "Perdone, no lo he entendido. ¿Puede repetirlo, por favor?",
        "fr": "Pardon, je n'ai pas compris. Pouvez-vous répéter, s'il vous plaît ?",
        "it": "Mi scusi, non ho capito. Può ripetere, per favore?",
        "pt": "Desculpe, não percebi. Pode repetir, por favor?",
    },
    "connecting": {
        "de": "Einen Moment bitte, ich verbinde Sie.",
        "en": "One moment please, I'm putting you through.",
        "tr": "Bir dakika lütfen, sizi bağlıyorum.",
        "ar": "لحظة من فضلك، سأحوّلك الآن.",
        "ru": "Одну минуту, я вас соединяю.",
        "uk": "Хвилинку, я вас з'єдную.",
        "pl": "Chwileczkę, już łączę.",
        "ro": "Un moment, vă rog, vă fac legătura.",
        "es": "Un momento, por favor, le paso la llamada.",
        "fr": "Un instant, s'il vous plaît, je vous mets en relation.",
        "it": "Un attimo, per favore, la metto in contatto.",
        "pt": "Um momento, por favor, vou transferir a chamada.",
    },
    "transfer_failed": {
        "de": "Es geht gerade leider niemand ran. Ich notiere Ihnen einen Rückruf. Wie heißen Sie?",
        "en": "Unfortunately nobody is picking up right now. I'll arrange a callback. What's your name?",
        "tr": "Maalesef şu anda kimse açmıyor. Sizin için geri arama not edeyim. Adınız nedir?",
        "ar": "للأسف لا أحد يرد الآن. سأسجل طلب معاودة الاتصال. ما اسمك؟",
        "ru": "К сожалению, сейчас никто не отвечает. Я запишу обратный звонок. Как вас зовут?",
        "uk": "На жаль, зараз ніхто не відповідає. Я запишу зворотний дзвінок. Як вас звати?",
        "pl": "Niestety nikt teraz nie odbiera. Zapiszę prośbę o oddzwonienie. Proszę podać swoje imię i nazwisko.",
        "ro": "Din păcate nu răspunde nimeni acum. Notez o cerere de apel înapoi. Cum vă numiți?",
        "es": "Lo siento, ahora no contesta nadie. Le apunto una llamada de vuelta. ¿Cómo se llama?",
        "fr": "Désolée, personne ne répond pour le moment. Je note un rappel. Quel est votre nom ?",
        "it": "Purtroppo ora non risponde nessuno. Le segno una richiamata. Come si chiama?",
        "pt": "Infelizmente ninguém está a atender. Vou registar uma chamada de volta. Como se chama?",
    },
    "goodbye_idle": {
        "de": "Ich höre Sie leider nicht mehr. Rufen Sie gern jederzeit wieder an. Auf Wiederhören!",
        "en": "I can't hear you anymore. Feel free to call again any time. Goodbye!",
        "tr": "Sizi artık duyamıyorum. İstediğiniz zaman tekrar arayabilirsiniz. Hoşça kalın!",
        "ar": "لم أعد أسمعك. يمكنك الاتصال مرة أخرى في أي وقت. مع السلامة!",
        "ru": "Я вас больше не слышу. Звоните в любое время. До свидания!",
        "uk": "Я вас більше не чую. Телефонуйте будь-коли. До побачення!",
        "pl": "Niestety już nic nie słyszę. Zapraszam do ponownego kontaktu. Do usłyszenia!",
        "ro": "Nu vă mai aud. Ne puteți suna oricând. La revedere!",
        "es": "Ya no le oigo. Puede volver a llamar cuando quiera. ¡Hasta luego!",
        "fr": "Je ne vous entends plus. N'hésitez pas à rappeler. Au revoir !",
        "it": "Non la sento più. Può richiamare quando vuole. Arrivederci!",
        "pt": "Já não o consigo ouvir. Pode voltar a ligar quando quiser. Adeus!",
    },
}


def phrase(key: str, language: str) -> str:
    """A fixed line in the call's language, German if somehow missing."""
    lines = PHRASES[key]
    return lines.get(language) or lines[BASE_LANGUAGE]


def normalize(code: str | Language | None) -> str | None:
    """'en-US', Language.EN_US, 'EN' -> 'en'. None for anything empty."""
    if code is None:
        return None
    raw = code.value if isinstance(code, Language) else str(code)
    raw = raw.strip().lower().replace("_", "-")
    return raw.split("-")[0] or None


def resolve(requested: str) -> str | None:
    """Map what the model passes to switch_language onto a supported code.

    Accepts a code ('en', 'en-US') or a name in German or English ('Englisch',
    'English', 'Türkisch', 'Turkish'), because models pass whichever they like.
    """
    text = (requested or "").strip().lower()
    code = normalize(text)
    if code in SUPPORTED:
        return code
    english_names = {
        "german": "de", "english": "en", "turkish": "tr", "arabic": "ar",
        "russian": "ru", "ukrainian": "uk", "polish": "pl", "romanian": "ro",
        "spanish": "es", "french": "fr", "italian": "it", "portuguese": "pt",
    }  # fmt: skip
    if text in english_names:
        return english_names[text]
    for code, german_name in SUPPORTED.items():
        if text == german_name.lower():
            return code
    return None


def word_languages(frame: TranscriptionFrame) -> Counter:
    """How many words of one finished utterance were in each language.

    Deepgram's multilingual model tags every word; that is far more reliable
    than the single utterance-level label, which goes to whichever language
    the first word happened to be in.
    """
    counts: Counter = Counter()
    try:
        words = frame.result.channel.alternatives[0].words or []
    except (AttributeError, IndexError, TypeError):
        words = []
    for word in words:
        code = normalize(getattr(word, "language", None))
        if code:
            counts[code] += 1

    if not counts:
        # No per-word tags (another STT, or a test): fall back to the frame.
        code = normalize(frame.language)
        if code:
            counts[code] = len(frame.text.split())
    return counts


@dataclass
class LanguagePolicy:
    """The current language of one call, and the rule for changing it."""

    current: str = BASE_LANGUAGE
    pinned: bool = False
    _pending: str | None = field(default=None, repr=False)

    def observe(self, counts: Counter) -> str | None:
        """Feed one finished utterance. Returns a language to switch to, or None."""
        total = sum(counts.values())
        if not total:
            return None
        language, words = counts.most_common(1)[0]

        if language == self.current:
            self._pending = None
            return None
        if self.pinned or language not in SUPPORTED:
            return None
        # A mixed sentence that is mostly German is German.
        if words < MIN_WORDS or words * 2 <= total:
            return None
        if words >= STRONG_WORDS or self._pending == language:
            return self._switch(language)
        self._pending = language
        return None

    def choose(self, language: str) -> bool:
        """The caller asked for a language. Returns whether it changed."""
        self.pinned = True
        if language == self.current:
            return False
        self._switch(language)
        return True

    def _switch(self, language: str) -> str:
        logger.info(f"Call language: {self.current} -> {language}")
        self.current = language
        self._pending = None
        return language


def switch_frames(language: str) -> list[Frame]:
    """Frames that move the model and the voice to `language` together.

    The model gets a rebuilt system prompt that names the language, so it
    never has to guess. The voice gets the matching language code, so English
    is pronounced as English and numbers follow that language's rules.
    """
    from receptionist import prompts  # here to avoid an import cycle

    return [
        LLMUpdateSettingsFrame(
            delta=LLMSettings(system_instruction=prompts.system_prompt(language))
        ),
        TTSUpdateSettingsFrame(delta=TTSSettings(language=Language(language))),
    ]


class LanguageSwitcher(FrameProcessor):
    """Sits right after speech-to-text and switches the call when the caller does.

    The switch frames are pushed BEFORE the transcription they were triggered
    by, so the model already has the new instruction when it answers it.
    """

    def __init__(self, policy: LanguagePolicy, **kwargs):
        super().__init__(**kwargs)
        self._policy = policy

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            switched_to = self._policy.observe(word_languages(frame))
            if switched_to:
                for update in switch_frames(switched_to):
                    await self.push_frame(update, direction)
        await self.push_frame(frame, direction)
