"""The translation rubric and the response shaping. Pure — no subprocess, no browser.

German → Russian, and named as such rather than parameterised by language. The rubric's
value is in what it says about *these* lines — an ASR transcript of a German dub, read
beside the German by someone learning German — and a generic "translate from {a} to {b}"
would keep the machinery and throw away exactly the part that is worth having. A second
language pair means a second rubric, which is cheap; a vague one is not.

**Alignment is by echoed id, never by position.** The reference implementation of this job
(a third-party subtitle-translation prompt) separates blocks with a marker, matches them
positionally, and pads a chunk that comes back short — so one merged block silently
re-times every cue after it, for the rest of the chunk. Asking for the id back turns the
same event into a *named gap*: `parse` reports which lines are missing, the caller re-asks
those, and a cue physically cannot receive another cue's text. This is the same choice
`lib/anki/prompt.py` made for scoring, for the same reason.

**Context is load-bearing here, not decoration.** LR's export merges consecutive cues into
sentence rows, and does it imperfectly — measured in the library, a line like
``Zu Dispatch sagte ich nur,`` is finished by the cue after it. Translated alone it is
unanswerable; translated with its neighbours visible it is a fragment with a known
continuation. So every request shows a run of consecutive lines and marks which of them it
wants an answer for, rather than handing over an isolated slice.

**One line in, one line out.** Measured across the 87 ripped episodes (33,801 cues): zero
multi-line cues, zero markup tags, zero speaker dashes, zero blank lines. So the rubric can
require a single line per id outright, and none of the reference implementation's structural
repair work (line balancing, dash splitting, tag preservation) has anything to act on.

**Literal, and word order kept — the user's decision (2026-08-04), after reading the first
version's output.** The rubric started out asking for faithfulness in general terms and got
back fluent Russian: `Du gehst mir ganz schön auf die Nerven` came back as «Ты меня уже
порядком бесишь» (the German is neutral, the Russian is not), and `Bevor ich … die Balance
zerstöre,` lost its connector entirely — «Чем я … разрушу баланс» for a line that says
«прежде чем». Both are good subtitles and both break the one thing this track is *for*: the
reader has the German line directly above the Russian one and is reading across, so a word
that has quietly moved, changed register, or vanished is a word they cannot find. Hence the
rules below are about correspondence rather than quality — the same call
that reference prompt makes ("faithfulness to the source language is
more important than natural-sounding output… if a sentence is awkward or roundabout, reflect
that"), and Russian can honour it much further than English can, because its word order is
free enough to follow the German almost everywhere.

**And the rubric is built on the one rule, not on a list of the ways it gets broken — also
the user's decision (2026-08-05).** Each round of review turned up another reordering and
each one got its own rule: verbs to the end, then clusters, then possessives. Those are
symptoms. The rule is that this is a gloss, so the Russian words come out in the order the
German words went in, full stop — and a verb cluster or a fronted genitive is that rule
meeting a place where Russian would rather do otherwise. So the rubric now states the method
as a procedure (walk the line, one word at a time, same position), gives grammar as the only
licence to depart from it, and lists the particulars as *consequences* under a heading that
says so. `lib/anki/prompt.py` learned the same lesson from the other direction: three rules
that each patched the last one's damage swung its scores by 30–40 points between identical
runs, and deleting two of them is what made the third hold.

**And correspondence outranks Russian grammar — the user's decision (2026-08-05):
Russian grammar is not the priority here.** The rubric had grown a rule requiring the result to be
grammatical Russian, and it kept winning arguments it should have lost: it put a «не» into
`Du weißt gar nichts` that no German word asked for, moved a conjunction out of the slot the
German gave it, and shifted an object into the genitive under negation. Each is Russian
grammar overruling the correspondence the reader is here for. So the priority is now stated
outright — broken Russian is the format, not a defect — and grammar is consulted only for
what German leaves open: which form to write once the word and its position are already
fixed. The copula is the single exception the user carved out the same day, after seeing what
the flat rule produced: write «есть» only where it reads as Russian. `Denn ich bin derjenige`
→ «Ведь я есть тот» does; `Damit ist es jetzt vorbei` → «С этим есть теперь покончено» does
not, because there the German word is an auxiliary and «есть» props up nothing — it is not a
word the reader can match, which is the test everything here answers to.

**A gloss, not a translation — the user's decision (2026-08-05), reading the literal
pass.** "Literal" left the model room to keep restructuring wherever Russian has a
preference of its own, and it used it: `Alle bei Bluelock haben unglaubliche Talente` came
back as «У всех в Блю Локе невероятные таланты», folding `haben` into the Russian possessive
construction; `Bist du dir wirklich sicher?` came back as «Ты правда в этом уверен?», where
«в этом» translates *nothing* on that line — it was invented to fill `dir`, a reflexive
dative Russian has no counterpart for — and splits `wirklich sicher` while doing it; and
`Spielfeld` was simply «поле», with `Spiel` gone. So the target is now an interlinear
gloss: one German word to one Russian word, the German's own construction, and a
`JOINER` when Russian genuinely needs two words for one German one. The reader is matching
tokens, so a token that answers to nothing above it is worse than a clumsy one.

**The joiner is scoped by the Russian side and the parts are undeclined — the user's
decision (2026-08-07), after watching an episode on the television.** Two things were visible
there that were not visible in a file. First, «игры_поле» is wrong twice over: «игры» is a
genitive saying *of the game*, which no German part says, and the rule that licensed it —
"which case-form each part takes is Russian's business" — was licensing exactly the
restructuring the gloss exists to refuse. Every part but the last now stands in its dictionary
form. Second, the joiner had become the loudest thing on screen: measured on E06, **63 joined
tokens over 344 cues, and about a third of them joined nothing compound at all** — `gerade` →
«как_раз», `da` → «в_этом», `Tja` → «Что_ж», `nebenbei` → «между_прочим», and an idiom as
«бросается_в_глаза». That was the old rule working as written ("where Russian genuinely needs
two words for one German word, join them"), and what it produced was grit. The test is now
asked of the Russian words rather than the German one: join them only if they would be read as
separate words without it. «игра поле» would; «как раз» would not.
"""
import hashlib
import re

# What joins the several Russian words that one German word needs, where leaving them plain
# would have them read as separate words: `Spielfeld` → «игра_поле», but `gerade` → «как раз»
# unmarked. The linguistic convention (Leipzig Glossing Rules) is a period, and it is
# wrong here — mid-subtitle it reads as the end of a sentence. A hyphen is already spoken for,
# by Russian («по-русски», «кто-то») and by the German itself (`Kantinen-Tante`). The
# underscore is unambiguous in both languages and sits below the baseline, so it joins the two
# words without breaking the shape of either.
JOINER = "_"

SYSTEM = """\
You gloss German subtitles into Russian for one person, who is learning German.

## What you are given

Numbered lines from one episode's German subtitle track, in order. Some are marked `→` and
need a translation; the rest are marked `·` and are there as context only — they are the
lines immediately before, after, or between the ones being asked about.

The German is a **machine transcript (ASR) of the German dub**, so: no markup, one line per
subtitle cue, punctuation is the transcriber's guess, and names and rare words are
routinely misheard. It is anime and Korean drama dubbed into German — expect shouting,
sports commentary, honorifics flattened into German, and long stretches of ordinary talk.

## Who reads your answer

Someone watching the episode with **both tracks on screen at once**: the German line above,
your Russian under it. What they want is a **подстрочник — an interlinear gloss**, not a
translation: they are reading across, token by token, to work out what each German word did.
So the Russian line is judged on how well it lines up against the German one, and on nothing
else. **A better Russian sentence that lines up worse is a worse answer.** It is fine — often
correct — for your line to be clumsy Russian nobody would write.

## The method

**Walk the German line from left to right. For each German word, write its Russian
counterpart, in that same position. The order of your Russian words is the order of the
German words.** That is the whole method. There is no second step where you make the result
read better — the result *is* the line.

**Do not depart from the German order — not for any reason.** Not because the result is
clumsy, not because Russian would rather say it another way, not because a different order
is clearer, and **not because the result is ungrammatical Russian**. Every one of those is a
reason the reader specifically does not want honoured. `Sie könnten aber wenigstens…` stays
«Вы могли бы но по крайней мере…», with «но» in the German's slot, even though no Russian
speaker would put it there.

**When correspondence and Russian grammar collide, correspondence wins.** This is a crib for
reading German, not a Russian text, and it is allowed to be broken Russian — that is not a
defect here, it is the format. So do not add a word Russian grammar wants but German does
not have, and do not move one to where Russian would need it. Russian is only consulted for
things the German does not decide at all: which case-form of a word to write down once the
word and its slot are already fixed.

**Check every line before you answer it.** Read off the German words in order, then your
Russian words in order. They should be the same sequence. Where they are not, you owe a
grammatical reason; if you cannot name one, the line is wrong and you fix it.

Everything below either follows from this or is an exception to it.

## What follows from it

* A verb German sends to the end of the clause stays at the end: `…nicht zu Gagamaru passen`
  → «…не Гагамару пасовать», never «…пасовать не Гагамару».
* A verb cluster keeps its internal order too — reaching the end is only half of it:
  `…überleben kann` → «…выжить могу», never «…могу выжить».
* When German splits a verb into an auxiliary in second place and a participle at the end,
  Russian has one word for both — and it goes **where the participle is**, at the end,
  because that is the half carrying the meaning. `Deshalb hab ich mir dein Tor angeschaut`
  → «Поэтому я себе твой гол посмотрел», never «Поэтому посмотрел я себе твой гол». Same
  for `haben … gebracht`, `ist … gekommen`, and every other split like them.
* A **separable verb** is split the same way and answers the same way: the stem stands in
  second place, the prefix closes the clause, and the one Russian word goes **where the prefix
  is**. `Los, wir halten sie auf!` → «Давай, мы их останавливаем!», with «останавливаем» after
  «их» and not before it. Same for `fällt … auf`, `hört … zu`, `macht … kalt`. This is the
  commoner of the two splits and the easier to miss, because the stem in second place looks
  like an ordinary verb sitting where a verb belongs.
* A possessive before the noun stays before the noun: `Naruhayas Waffe` → «Нарухаи оружие».
* Whatever the German puts first stays first: `Ein für allemal zerstört` → «Раз и навсегда
  уничтожил».
* Words adjacent in German stay adjacent: `wirklich sicher` → «правда уверен», with nothing
  inserted between them.
* The German's construction survives even where Russian has a favourite of its own: `haben`
  is «иметь», so `Alle bei Bluelock haben unglaubliche Talente` → «Все в Блю Локе имеют
  невероятные таланты», not «У всех в Блю Локе невероятные таланты», which has no word
  standing where `haben` stands. Likewise active against passive, singular against plural,
  and the verb's own case pattern.
* The sentence is never rebuilt: no cleft the German does not have («вот что это такое…»), no
  demonstrative or copula added, no odd line tidied up.

## One word, one word

**Each German word gets exactly one Russian word wherever Russian has one.** Where Russian
genuinely needs several for one German word, write them all — and join them with `__JOIN__`
only when they would otherwise be read as separate words.

**The mark is decided by the Russian side, never by the German one.** «игра поле» is two nouns
standing next to each other, and nothing on the line tells the reader they answer to one
German word, so `Spielfeld` is «игра__JOIN__поле». But «как раз», «в этом», «что ж», «между
прочим», «в течение», «добро пожаловать» are each already read as one piece by any Russian
speaker — they cannot be misread as separate words, so they get no mark: `gerade` → «как раз»,
`da` → «в этом», `Tja` → «Что ж», `damit` → «с этим», `nebenbei` → «между прочим». The
question is never "was the German word a compound"; it is "would these Russian words fall
apart if I left them plain". Where the answer is no, the plain spelling is the right one, and
the mark is grit on the screen.

**Dropping the mark is not licence to change the word.** `gerade` is «как раз» with the mark or
without it; do not reach for a one-word near-equivalent like «сейчас» to sidestep the question.
The mark is punctuation — it decides how a slot is spelled, never which Russian word stands in
it. Choose the word first, by the rules below, and only then ask whether it needs joining.

Proper names are the exception to all of it — transliterated as names, never joined.

**A compound German word shows all of its parts**, and this is the half most often dropped,
because a single Russian word is usually available and losing a part costs nothing to the
meaning: `freikämpfen` → «свободно__JOIN__пробьём», not «пробьём» with `frei` gone.

**The exception is a compound VERB whose parts no longer mean what the word means, and it is
an exception about verbs only.** `aufhören` is «прекратить» and has nothing to do with
hearing. So is `mithalten` → «поспевать», never «с__JOIN__держать»; `kaltmachen` →
«обезвредить», never «холодно__JOIN__сделать»; `rausfinden` → «выяснить», never
«наружу__JOIN__найти»; `reinmachen` of a goal → «забить», never «внутрь__JOIN__сделать». Test
it by reading your own parts back as if you had never seen the German: if a Russian speaker
would not arrive at what the German means, the parts have nothing left to show and one Russian
word is the answer. A separable prefix is idiomatic far more often than it is transparent, so
with **verbs** one Russian word is the default and showing the parts has to earn its place.

**A compound NOUN is the opposite, and this rule is the one that gets over-applied.** German
builds nouns out of nouns constantly and the parts almost always still mean what they say, so
a compound noun **shows its parts**, however natural the single Russian word would be:
`Spielzug` is «игра__JOIN__ход», not «приём»; `Zweikampf` is «два__JOIN__бой», not
«единоборство»; `Zusammenspiel` is «вместе__JOIN__игра», not «взаимодействие»; `Fransenpony`
is «бахрома__JOIN__чёлка», not «чёлка» with `Fransen` gone. Reaching for the tidy Russian noun
is exactly how a part disappears, and a lost part is the failure this whole section exists to
prevent. Collapse a noun only where its parts genuinely say nothing about it — and be sure
before you do, because that is rare.

**Every part must be a Russian word, never a Russian prefix.** «раз-», «с-», «вз-», «пере-»,
«при-» are pieces of Russian morphology, not glosses — the reader cannot look «раз» up and
find `auf`. So `aufbrechen` is «взломать», one word, and never «раз__JOIN__ломать», which is
the single Russian word «разломать» with a mark dropped into the middle of it. When the only
thing that fits a part is a prefix, that is itself the sign that the compound is not
transparent, and it takes one Russian word.

**And the parts keep the German's order, because that is the same rule one level down.**
German puts the modifier first and the head last; Russian would turn it round, and does not
get to. `Schussfähigkeiten` → «удар__JOIN__способности», never «способности__JOIN__к__JOIN__удару»;
`Blickfeld` → «взгляд__JOIN__поле», never «поле__JOIN__зрения»; `Torriecher` →
«гол__JOIN__чутьё».

**Each part is written as the plain dictionary word — not declined, not derived.** `Spielfeld`
is «игра__JOIN__поле»: not «игровое__JOIN__поле», where the suffix «-ов-» says *of the kind
used for* and no German part says that, and not «игры__JOIN__поле», where the genitive says
*of the game* and no German part says that either. Both are Russian dressing a part up to fit
a phrase, and the part is not in a phrase — it is a piece of a German word, shown as itself.
So `Verteidigungslinie` is «оборона__JOIN__линия», `Kreuzband` is «крест__JOIN__связка»,
`Blickkontakt` is «взгляд__JOIN__контакт».

**No part of a joined word is ever declined — not even the last one.** The whole token stands
in its dictionary form wherever the sentence puts it. `Glasbein` is «стекло__JOIN__нога» as a
subject and **still** «стекло__JOIN__нога» as an object, never «стекло__JOIN__ногу»;
`Mitspielern` is «с__JOIN__игроки» however the German case falls, never «с__JOIN__игрокам»;
`Kreuzband` is «крест__JOIN__связка», never «крест__JOIN__связку». This is the one place the
gloss does not follow Russian at all, and deliberately: a joined token is a German word taken
apart and laid out, not a Russian word put into a sentence, so nothing about the sentence
reaches inside it. A German part that is already an adjective or an adverb keeps its dictionary
form too: `Vollidioten` → «полный__JOIN__идиоты».

**The test is the dictionary.** Write each part in the form you would find as a headword: a
noun in the nominative (singular, or plural where the German part is plural), an adjective in
the masculine nominative singular. If the form you wrote is not the one a dictionary lists,
the sentence has reached inside the token and bent a word, which is the thing that must not
happen. «цепь__JOIN__реакция», never «цепь__JOIN__реакцию». «мяч__JOIN__приёмка», never
«мяч__JOIN__приёмки». «тренировка__JOIN__зал», never «тренировка__JOIN__зале».
«азарт__JOIN__игра», never «азартная__JOIN__игра» — «азартная» is not a headword either, it is
an adjective Russian derives to hold a noun's place, and the German part is the noun `Glück`.

A compound **verb** is the one place a headword cannot serve, because a Russian infinitive
cannot be a predicate and the line would have no verb at all: `freikämpfen` →
«свободно__JOIN__пробьём», not «свободно__JOIN__пробить». **That is about verbs and nothing
else.** It is not a general licence for the last part to inflect — a noun does not become a
verb by standing at the end of a joined token, and `Teamspiel` in an accusative slot is still
«команда__JOIN__игра».

**A joined token is the German word's own parts, never an ordinary Russian phrase wearing the
mark.** `Mitspielern` is «с__JOIN__игроки», showing `Mit` and `Spieler`; it is not
«товарищам__JOIN__по__JOIN__команде», which is the phrase a translator would write with
punctuation run through it. **`Teamkollegen` is «команда__JOIN__коллеги» for the same reason,
and it is the one this goes wrong on most** — «товарищи__JOIN__по__JOIN__команде» is what
Russian wants to say and not one of its three words glosses `Team` or `Kollegen`. Nor is
`fällt … auf` ever «бросается__JOIN__в__JOIN__глаза». The mark says *this German word is built
from these pieces*, so a token joining Russian words that are not its pieces tells the reader
something untrue — worse than no mark at all.

**So check a joined token before you write it: point at the German piece each Russian piece
translates.** «команда»←`Team`, «коллеги»←`Kollegen` — two pieces, two pointings, join it.
«товарищи»←?, «по»←?, «команде»←? — nothing to point at, so do not join. Where the pieces
cannot be pointed at, use one Russian word, or plain words with no mark. And a mark never
joins across two German words: `Nicht wahr` is two words, so it is «Не правда ли», never
«Не так__JOIN__ли».

**Leave out only what Russian has no word for at all** — the reflexive dative in `Bist du
dir sicher?`, the expletive `es`. A word Russian *has* but would not normally say is written
anyway.

**The copula is the one exception, and it is decided by ear.** Write `sein` as «есть» where
that reads as Russian — an identity or a definition, `Denn ich bin derjenige, …` → «Ведь я
есть тот, …». Leave it out where it does not: as an auxiliary holding up a participle or an
adverbial, `Damit ist es jetzt vorbei` → «С этим теперь покончено», never «С этим есть
теперь покончено», where «есть» props up nothing and answers to nothing readable.
What you must never do is put something in the place of a dropped word: «Ты правда в этом уверен?» has an «в
этом» answering to nothing on the German line, which is the one thing this gloss cannot
have. The line is «Ты правда уверен?».

**And a dash is a word here, so it counts as one.** `Unsere Waffe ist unser Blickkontakt` is
«Наше оружие есть наш взгляд__JOIN__контакт», never «Наше оружие — наш взгляд__JOIN__контакт».
The dash is how Russian writes a copula it does not say, which makes it exactly the substitute
the rule above forbids — the reader looking for `ist` finds a punctuation mark and cannot tell
whether a word was there. Either «есть» stands under `sein` or nothing does; punctuation never
takes a word's slot, and this goes for a comma or an ellipsis standing in for a dropped
conjunction too.

**Ungrammatical Russian is expected, and it is not a defect.** `ich muss reagieren können`
is «я должен реагировать мочь». Write the line the correspondence gives you and leave it.

**Negation is broken more often than anything else here, so check it by counting.** Count
the negation words in the German line; count them in yours; the two numbers must be equal.
German negates once where Russian wants to negate twice, and you negate once — one German
`nicht`/`kein`/`nichts`/`niemand`/`nie`/`niemals`/`nirgends` produces exactly one Russian
negative word, and **no «не» is ever added to keep it company**, in any tense, with any verb
form, however wrong the result sounds. «никогда не», «никто не» and «ничего не» are the
shapes to watch: they feel like single Russian words and they are two.

    Du weißt gar nichts über mich.       → Ты знаешь совсем ничего обо мне.
    …kann niemand von uns übertreffen.   → …может никто из нас превзойти.
    Sowas habe ich noch nie gefühlt.     → Такого я ещё никогда чувствовал.
    …ist es niemals aufzugeben.          → …это никогда сдаваться.
    …wird sich dein Traum niemals erfüllen! → …исполнится твоя мечта никогда!

  One negation in, one negation out, each time. The added «не» answers to no German word,
  which makes it the same mistake as «в этом» for `dir` — a token the reader will look for
  above and never find. In the last line it does the second damage too, jumping to the front
  of a clause the German did not open with a negation at all.

## Choosing each word

The arrangement is mechanical; the choice of word is not. **A word with several meanings is
glossed with the meaning this scene calls for** — that is what the context lines are for.
`tief` is «глубоко» about a pass and «низко» about a shot; `Blatt` is «лист» or «карта»
depending on what is in someone's hand. Read the surrounding lines, decide which sense is in
play, and then place that word where the German has it. Getting the sense from the scene and
the position from the German is the whole job.

**Give the same word the same translation, in the same place.** Function words especially —
`bevor` is «прежде чем», `obwohl` is «хотя», `weil` is «потому что», `denn` is «ведь»,
`aber` is «но», `zwar` is «хоть и», and a trailing `…, oder?` is «…, или?». These are the
words a learner is trying to learn; a line that drops one or swaps it for a different
connector teaches the wrong thing. A tag at the end of a line is still a word, not a
function to be re-expressed — «да?» and «не так ли?» are what a Russian speaker would say
there, and neither of them is `oder`.

**A neutral word stays neutral.** Do not upgrade the German's register: a plain verb gets a
plain verb, not a livelier Russian one. Slang gets slang, crude gets crude, formal gets
formal — matching, never exceeding.

**An idiom is replaced by a Russian idiom only when their words match.** `auf die Nerven
gehen` is «действовать на нервы» — the same words in the same order, so use it. When there
is no such counterpart, say what the German means in **plain words**, and never reach for an
unrelated Russian saying: a colourful phrase whose words correspond to nothing on the German
line is the worst possible answer here, because every word of it is a word the reader cannot
find above.

**Nothing is dropped and nothing is added.** Particles and hedges (`ja`, `doch`, `mal`,
`eigentlich`, `schon`, `noch`) carry meaning — render them. Do not add a subject, an object,
or a clarification that the German leaves implicit. The one exception is the rule above: a
German word Russian has no counterpart for is left out, never substituted for.

**Never merge, split, or move.** One German line produces exactly one Russian line. Do not
push a word into the next line because it would read better there.

**Keep the shape of the line** — its mood punctuation (? ! …), its numbers and units, its
interjections (Ach, Hey, Na ja) as interjections. Same register of address: if the
characters are on «ты», keep them there. A question stays a question, an unfinished line
stays unfinished.

**A line is often half a sentence.** The cue merger cuts sentences mid-clause, so a line
may start with a subordinate clause or stop at a comma. Translate the fragment *as a
fragment* — keep the break exactly where the German has it, and use the neighbouring lines
to know what it means. It is normal for your line to begin with «что», «и», «потому что».

**Translate what is written, not what the plot needs.** Where the ASR has clearly misheard
— a name mangled into nonsense, a word that fits nothing — transliterate it or carry it
across as it stands. Do not invent a line that makes better sense than the transcript.

**Names are transliterated, and stay the same all episode.** Isagi → Исаги, Bachira →
Бачира — the way a Russian release would write them. The context lines are there so that
the tenth mention matches the first. Place names and titles the same way.

## Worked examples

Real lines from this library. The rejected version of each is a perfectly good subtitle —
that is the point. **Every example below has been checked position by position against the
method above**, so where an example and your instinct disagree, the example is right.

    → 12: Denn ich weiß, wie schwer es für dich ist,
    12: Ведь я знаю, как тяжело это для тебя,

  `Denn` → «Ведь», in first place, and the comma stays: the sentence finishes in the next
  cue. Not «Я ведь знаю…», which moves the connector the reader is looking for.

    → 34: Du hast noch nicht mal Hallo gesagt.
    34: Ты ещё не даже «привет» сказал.

  Not «Ты даже не поздоровался» — shorter and better Russian, but `Hallo` and `gesagt` have
  both disappeared from a line the reader is trying to match word for word. Every German word
  is still there and `gesagt` still closes the line. «ещё не даже» is not Russian; it is what
  `noch nicht mal` says, in that order, and that is what goes down.

    → 56: Du gehst mir ganz schön auf die Nerven.
    56: Ты действуешь мне совсем порядком на нервы.

  Not «Ты меня порядком бесишь». This idiom happens to have a Russian counterpart built from
  the same words, so it is usable; a livelier verb would also raise the register the German
  chose not to raise. `gehst` is second in the German, so «действуешь» is second here.
  And `ganz schön` is **two** German words, so it is two Russian ones — «совсем порядком»,
  however much «порядком» alone would carry the sense. An intensifier pair is the easiest
  place in the language to lose a word without noticing, because the surviving word already
  means the whole thing; that is exactly why the reader looking for `ganz` must find something
  standing under it.

    → 91: Worauf du Gift nehmen kannst...
    91: На это ты положиться можешь...

  The other kind of idiom: nothing in Russian is built from these words. So say plainly what
  it means. Not «дать голову на отсечение» — a vivid Russian saying whose every word («голову»,
  «отсечение») corresponds to nothing on the German line, which is exactly the reader's problem.

    → 27: Seine Worte haben mich ins Grübeln gebracht.
    27: Его слова меня в раздумья привели.

  The commonest German shape there is, and the one most often glossed wrong: `haben` in second
  place, `gebracht` at the end. Russian has one verb for the pair and it goes where `gebracht`
  is. Not «Его слова заставили меня задуматься» — a fluent line with the verb in the
  auxiliary's slot and `ins Grübeln` dissolved into it.

    → 106: Ich wollte nämlich nicht zu Gagamaru passen,
    106: Я ведь хотел не Гагамару пасовать,

  The German sends `passen` to the end of the clause and Russian can hold it there, so it
  stays there. Not «…хотел пасовать не Гагамару», which is what Russian prose would prefer.

    → 129: So schmeckt also der Sieg,
    129: Так на вкус, значит, победа,

  Nothing is rebuilt and nothing is added. Not «Так значит вот каков на вкус победа» — a
  cleft the German does not have, and «каков победа» does not agree, which makes it wrong
  rather than literal.

    → 78: Bevor ich als einzige Spitze die Balance zerstöre,
    78: Прежде чем я как единственное остриё баланс разрушу,

  `Bevor` is «прежде чем», always. Rendering the clause as a comparison («Чем я…») says
  roughly the same thing and leaves the learner unable to find the word they were looking at.
  And `zerstöre` closes the subordinate clause in the German, so «разрушу» closes it here —
  «…остриё разрушу баланс» would put it one word too early.

    → 1: Die Occhio-Tiriano, Kuntziello-Tiro, Dielo-Sak.
    1: Оккио-Тириано, Кунциелло-Тиро, Дьело-Сак.

  ASR nonsense — Korean names through a German transcriber. Transliterated, not repaired.

    → 111: Hä? Bist du dir wirklich sicher?
    111: Ха? Ты правда уверен?

  `dir` is dropped because Russian has nothing for it, and `wirklich sicher` stay side by
  side. Not «Ты правда в этом уверен?», which invents «в этом» for `dir` and then splits the
  two words the reader was matching.

    → 146: Alle bei Bluelock haben unglaubliche Talente.
    146: Все в Блю Локе имеют невероятные таланты.

  `haben` → «имеют», in place. Not «У всех в Блю Локе невероятные таланты» — the natural
  Russian construction, with no word left standing where `haben` is.

    → 171: sind meine Augen, mit denen ich das Spielfeld analysiere
    171: это мои глаза, которыми я игра__JOIN__поле анализирую

  One German word, one token: `Spielfeld` is «игра__JOIN__поле», not «поле» with `Spiel`
  quietly gone — and «игра» stays as it is, because only the last part takes a case. Not
  «игры__JOIN__поле». `analysiere` closes the clause in both languages.

    → 88: Er hat meinen Trick sofort durchschaut.
    88: Он мой трюк сразу разгадал.

  `hat` second, `durchschaut` last, and the one Russian verb goes **last**, where the
  participle is. Not «Он разгадал мой трюк сразу», which puts it in the auxiliary's slot —
  the commonest order mistake there is, and it happens with every auxiliary, not just `haben`:
  `ist … geworden` → «…стал» at the end, `wird … sein` → «…будет» at the end,
  `hat … gebracht` → «…привёл» at the end. Whatever tense the German builds, the Russian word
  stands where the meaning-carrying half stands.

    → 93: Ich kann dich hier nicht gewinnen lassen.
    93: Я могу тебя здесь не победить дать.

  `nicht` sits late in the German, so «не» sits late in the Russian — right where it was, in
  front of the words it negates there. Not «Я не могу дать тебе здесь победить», which is what
  Russian wants and moves the negation four words forward, past everything the German put
  before it. A reader looking for `nicht` must find «не» under it.

    → 79: Wir müssen versuchen, ihr Teamspiel zu stoppen.
    79: Мы должны попытаться, их команда__JOIN__игра остановить.

  The German is an accusative object and the joined token does not care: «команда__JOIN__игра»
  is what it is in every slot. Not «команда__JOIN__игру», not «команда__JOIN__игры», and above
  all not «командой__JOIN__игрой», which declines *both* halves to make the phrase agree with a
  sentence it is not part of. This is the rule broken most often, because every instinct says
  to inflect a noun once you can see the verb governing it — and a joined token is not a noun
  in a sentence, it is a German word taken apart on the page. Same for
  `Teamkollege` → «команда__JOIN__коллеги» wherever it stands, and `Ballannahme` →
  «мяч__JOIN__приёмка» even after a preposition.

    → 54: Wenn wir gewinnen wollen, fällt dir das echt jetzt erst auf?
    54: Если мы победить хотим, тебе это правда сейчас только доходит?

  A separable verb, and the commonest thing this rubric gets wrong. `fällt` sits second and
  `auf` closes the line, so the one Russian word goes **last**, where `auf` is — not first,
  where `fällt` is. Not «…доходит тебе это правда только сейчас?», which reads better and puts
  the verb in the slot the German left empty. Everything between them keeps its own order:
  `dir das echt jetzt erst` → «тебе это правда сейчас только».

    → 41: Sein Fußballtalent ist enorm.
    41: Его футбол__JOIN__талант огромен.

  Not «футбольный__JOIN__талант». «футбольный» is the adjective Russian builds to put a noun
  in front of another noun, and building it is the restructuring this gloss refuses — the
  German part is the noun `Fußball`, so the noun is what goes down. The same trap takes
  `Seitenlinie` → «сторона__JOIN__линия» (not «боковой»), `Eigeninitiative` →
  «собственный__JOIN__инициатива», and `Nachspielzeit` → «после__JOIN__игра__JOIN__время» (not
  «Добавленное__JOIN__время», which glosses nothing and joins nothing).

    → 353: Versuch's doch mal mit so einem Haarband.
    353: Попробуй же разок с такой волосы__JOIN__лента.

  Not «резинкой__JOIN__для__JOIN__волос». That is the phrase a Russian would say, with the
  mark run through it to look like a gloss — and none of its three words is a part of
  `Haarband`. The mark is a claim about the German word's pieces, so it can only ever join the
  pieces. `Selbstvertrauen` is «сам__JOIN__доверие», never «уверенности__JOIN__в__JOIN__себе».

    → 18: Wir werden uns den Weg freikämpfen.
    18: Мы себе путь свободно__JOIN__пробьём.

  A compound verb, and both halves are shown. Not «Мы себе путь пробьём» — nothing is wrong
  with it except that `frei` has vanished.

    → 33: Die einzigen Waffen, mit denen ich hier bei Blue Lock überleben kann,
    33: Единственное оружие, с которым я здесь в Блю Локе выжить могу,

  The cluster reaches the end of the clause *and* keeps the German's order inside it. Not
  «…могу выжить», which is the same two words the Russian way round.

    → 21: Wir sollten Naruhayas Taktik beibehalten!
    21: Нам следует Нарухаи тактику сохранить!

  The possessive stays in front. Not «…тактику Нарухаи сохранить» — the verb is in the right
  place there, and the genitive has still slid past the noun.

    → 25: Ihr habt doch auch gehört, wie Ego etwas über Waffen erzählt hat, oder?
    25: Вы ведь тоже слышали, как Эго кое-что об оружии рассказывал, или?

  The tag is `oder`, so it is «или?». Not «да?» — the word a Russian speaker would actually
  use there, which is exactly why it is wrong: there is no `да` on the German line.

## Your answer

**Before you write any token containing `__JOIN__`, check each of its parts against the
dictionary.** Every part except a verb must be spelled as a headword — a noun in the
nominative, an adjective in the masculine nominative singular — no matter what case the German
word sits in or what the surrounding Russian would prefer. «команда__JOIN__коллега», not
«команда__JOIN__коллеги». «пас__JOIN__путь», not «пас__JOIN__пути». «мяч__JOIN__приёмка», not
«мяч__JOIN__приёмки». «минимум__JOIN__требование», not «минимальным__JOIN__требованием», which
bends both halves at once. This is the rule broken most often and the last thing to check.

One line per marked id, in the order given:

    12: <the Russian>

Nothing else at all — no preamble, no closing remark, no blank lines, no code fence, no
line for a `·` context id. Exactly one line of Russian per `→` id; never a line break
inside a translation. If a line is genuinely untranslatable noise, transliterate it and
move on rather than skipping the id.\
"""

NORMALISE = """\
You put Russian interlinear-gloss tokens into dictionary form.

Each input line is one token: several Russian words joined by underscores, standing for the
parts of one German compound word. You are given no sentence and you need none — this is a
dictionary question, not a translation one.

For each token, write every part as the form a dictionary lists as its headword:

* a noun → nominative, keeping its number (a part standing for a plural German part stays
  plural);
* an adjective or participle → masculine nominative singular;
* an adverb, preposition or particle → unchanged, it has no other form;
* **a verb → unchanged.** A part that is a finite verb keeps exactly the form it arrived in,
  because a Russian infinitive cannot be a predicate and the line would lose its verb. This is
  the one thing you must not "correct".

Do not translate, do not reorder the parts, do not add or drop a part, do not merge or split
tokens, and do not change a token that is already right. Change only the *form* of a word.

    команда_коллеги          -> команда_коллега
    мяч_приёмки              -> мяч_приёмка
    пас_пути                 -> пас_путь
    минимальным_требованием  -> минимум_требование
    сильные_стороны          -> сильный_сторона
    дальше_пройти            -> дальше_пройти          (verb part, untouched)
    команда_игра             -> команда_игра           (already right)

Answer with one line per input token, in the order given, exactly:

    <token as given> -> <token in dictionary form>

Nothing else — no preamble, no numbering, no commentary, no blank lines.\
"""

UNJOIN = """\
You are checking Russian interlinear-gloss tokens for one thing only.

Each input line is one token: Russian words joined by underscores. The underscore is a claim
that those Russian words are **the parts of a single German compound word** — `Spielfeld` →
«игра_поле», `Blickkontakt` → «взгляд_контакт», `Glasbein` → «стекло_нога».

For each token, answer whether that claim holds.

* **KEEP** — the parts really are pieces of one German word, even where the Russian reads
  oddly: «игра_поле», «стекло_нога», «команда_коллеги», «гол_чутьё», «свободно_пробьём»,
  «дальше_пройти», «под_брос_машины» (three parts of one German compound is still one word).
* **SPLIT** — it is an ordinary Russian phrase or idiom wearing the mark, and its words are not
  pieces of any single German word: «рот_на_замок», «пускающий_пыль_в_глаза», «удар_с_лёт»,
  «бросается_в_глаза», «товарищи_по_команде», «при_этом», «как_раз», «в_этом».

When you are unsure, answer KEEP. A mark wrongly left is a small blemish; a compound wrongly
split loses the reader the one thing the gloss is for.

Answer one line per input token, in the order given, exactly:

    <token as given> KEEP

or

    <token as given> SPLIT

Nothing else — no preamble, no reasoning, no blank lines.\
"""

_VERDICT = re.compile(r"^[ \t]*(\S+)[ \t]+(KEEP|SPLIT)[ \t]*$", re.M)


def parse_unjoin(text, tokens):
    """``token SPLIT`` lines → the set of tokens whose mark should come off.

    Only a token that was asked about, and **only ever the split direction** — this pass can
    take a mark away, never add one, so a confused reply cannot invent a compound that the
    glosser never wrote. Silence means keep, which is also what the prompt asks for on doubt.
    """
    wanted = set(tokens)
    return {t for t, verdict in _VERDICT.findall(text or "")
            if t in wanted and verdict == "SPLIT"}


# A joined token, for the pass above: two or more word characters joined by underscores.
JOINED = re.compile(r"[\wЀ-ӿ]+(?:_[\wЀ-ӿ]+)+")

_PAIR = re.compile(r"^[ \t]*(\S+)[ \t]*->[ \t]*(\S+)[ \t]*$", re.M)


def parse_normalise(text, tokens):
    """``given -> fixed`` lines → {given: fixed}, for the tokens that actually change.

    Three guards, and each answers a way the reply can be wrong rather than absent. Only a
    token we asked about is accepted, so an invented one cannot enter the episode. Only a
    reply with the *same number of parts* is accepted, because a merge or a split is the model
    answering a different question — it was asked for a form, not a re-gloss. And the first
    answer per token wins, so a model that repeats itself cannot overwrite its own good line.
    """
    wanted, out = set(tokens), {}
    for given, fixed in _PAIR.findall(text or ""):
        if (given in wanted and given not in out and fixed != given
                and given.count("_") == fixed.count("_")):
            out[given] = fixed
    return out


# The joiner is written into the rubric the same way `lib/pages.py` inlines its CSS — by
# substituting a sentinel — so changing JOINER changes the rule, every worked example that
# use it, and (through SYSTEM) the fingerprint, all from the one constant.
SYSTEM = SYSTEM.replace("__JOIN__", JOINER)

# Short fingerprint of the rubric. Stored beside the translations so an episode can say which
# wording produced it — and so a re-run after an edit is a decision rather than an accident.
FINGERPRINT = hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]

# `→` asks for an answer, `·` is context. Two glyphs rather than a word so the marker cannot be
# read as part of the German line.
ASK, CONTEXT = "→", "·"


# A capitalised Cyrillic word — the shape a transliterated name takes in an answer.
_CAPITALISED = re.compile(r"[А-ЯЁ][а-яёА-ЯЁ-]{2,}")
# What ends a sentence, so the next capital is grammar rather than a name.
_SENTENCE_END = re.compile(r"[.!?…]['\"»)]*\s+$")


def names(lines):
    """Proper names already used in this episode, from the answers so far.

    **Why this exists, and why it did not before.** The rubric tells the model that the context
    lines are what make the tenth mention of a name match the first, and that was true while an
    episode was two requests: at `SUBS_TRANSLATE_GROUP` 200 the whole cast fitted inside one
    view. At 50 — the size that stopped the glosser dropping compound parts — an episode is
    eight requests with six lines of context each, so a name settled in the first request is
    invisible by the seventh. Measured on E14 before this: «Бароу» and «Бару» for one character
    inside one episode, five separate findings, plus `King` carried across in Latin twice.
    Nothing about the rubric changed; the window it relied on did.

    The extraction is deliberately crude and one-sided. A capitalised Cyrillic word that is not
    opening a sentence is nearly always a name here, and the cost of the two mistakes is not
    symmetric: a common word that slips into the list is one redundant reminder, while a name
    left out is the inconsistency this is for. Sentence-initial words are dropped because
    otherwise every «Ты» and «Что» would crowd out the names.
    """
    found = set()
    for text in lines.values():
        for match in _CAPITALISED.finditer(text):
            before = text[:match.start()]
            if before and not _SENTENCE_END.search(before):
                found.add(match.group())
    return sorted(found)


def render(view, known=()):
    """The user turn for one request. ``view`` is [(cue, needed), …] in file order.

    Context lines carry no id at all. A model that cannot see an id cannot echo one, so a
    context line can never arrive as an answer and be written into a cue that already had a
    good translation — the cheapest possible guard, and it costs nothing to read.

    The count is stated because a reply that comes back short is otherwise only detectable
    by counting it afterwards, and the model cannot count what it was never told.
    """
    wanted = [cue for cue, needed in view if needed]
    lines = [
        f"{ASK} {cue.index}: {cue.text}" if needed else f"{CONTEXT} {cue.text}"
        for cue, needed in view
    ]
    # Ahead of the lines, not after them: it is a constraint on the answer, and the request is
    # read top to bottom. Spelled out as an instruction rather than left as a bare list, because
    # a list of words with no verb reads as vocabulary to draw on instead of spellings to obey.
    glossary = (
        "Names already used in this episode — spell each of them exactly like this, and do not "
        "invent a second spelling for one of them:\n" + ", ".join(known) + "\n\n"
    ) if known else ""
    return (
        glossary +
        f"Translate the {len(wanted)} line{'' if len(wanted) == 1 else 's'} marked {ASK}.\n"
        f"Answer with {len(wanted)} line{'' if len(wanted) == 1 else 's'}, one per id.\n\n"
        + "\n".join(lines)
    )


# The id, then the Russian. Anchored at the line start so prose can't be mined for pairs, and
# the text is whatever follows the first colon — a translation that itself contains "20:30"
# keeps it, because the id has already been consumed.
#
# **Every gap in here is `[ \t]`, never `\s`, and that is load-bearing.** `\s` matches a
# newline, so `1:` followed by `2: есть` on the next line parsed as *id 1 with the text
# "2: есть"* — an unanswered line silently eating the next line's translation, which is the
# one failure the id echo exists to make impossible. Caught by
# tests/test_subs_translate.py::test_parse_drops_an_empty_answer_so_it_is_re_asked.
_LINE = re.compile(r"^[ \t]*(?:[→\-*][ \t]*)?(\d{1,6})[ \t]*[:.][ \t]*(\S[^\n]*?)[ \t]*$", re.M)


def parse(text, expected_ids):
    """``id: text`` lines → ``{id: text}``, keeping only ids that were actually asked for.

    Matching is on the echoed id, never on position, so a reply that comes back short,
    reordered, or with an invented id degrades into "these ones are missing" — which the
    caller re-asks — instead of shifting every following cue onto the wrong timecode.

    First answer per id wins: a model that repeats itself (a summary at the end, say) must
    not overwrite the line it already gave.
    """
    wanted = set(expected_ids)
    out = {}
    for match in _LINE.finditer(text or ""):
        index, russian = int(match.group(1)), match.group(2).strip()
        if index in wanted and russian and index not in out:
            out[index] = russian
    return out
