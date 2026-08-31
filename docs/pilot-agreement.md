# Mnemos pilot — mutual understanding

**Between:** Mnemos Labs ("we") and Boost Run ("you")
**Covers:** the 4-week single-user pilot beginning mid-September 2026
**Status:** plain-language understanding between two parties who want to work
together. It is not a contract and has not been reviewed by a lawyer — if
either side needs one, this document is the brief to hand them.

---

## 1. What this is

Experimental research software, handed to you early on purpose. It is not
production software, it is not certified against anything, and it will have
bugs. You are testing a prototype in exchange for shaping what it becomes.

Concretely, that means: it may crash, it may mis-hear people, it may attribute
a sentence to the wrong speaker, and a build may need replacing mid-pilot. We
fix what you hit; we do not promise uptime, accuracy, or that any particular
thing works on your hardware until we have seen it work on your hardware.

## 2. What we collect

**Centrally: nothing.** There is no Mnemos server holding anyone's memory.
Recording, transcription, and memory all happen on the tester's own machine,
and the app is not reachable from the network. We cannot read a tester's
memory, and neither can their colleagues or their employer — not because we
promise not to, but because there is no copy to read.

Three things can leave a tester's machine, all visible in the app under
**Privacy controls → What has left this machine**, which reads the machine's
own logs rather than repeating this page:

| What | When | Contains |
|---|---|---|
| Questions to a frontier model (Anthropic) | Only for questions the local models can't answer, and only under a hard **$2/day** cap per user | The question and the relevant context, with sensitive classes redacted first. Never the recordings. |
| Anonymous usage counts | **Off by default.** Only if the tester turns on the weekly ping, and only to the endpoint we configure | Counts and dates: how many searches, meetings, reviews. A random install id. No content, ever — no text of anything said, searched, or seen. The tester can see the exact payload before consenting. |
| A version check | If left on | An unconditional download of a static version file. Sends nothing about the tester — not even which version they run. |

The usage counts are the only thing we receive, and they are the only way we
can compute the pilot numbers we have told you about (weekly actives, week-2
retention). If every tester declines, we will ask them to email us the same
file by hand, and if they decline that too, we will report the pilot on your
qualitative feedback alone.

**We will never ask a tester for their memory, their recordings, or their
transcripts, and we have built no way to receive them.**

## 3. Stopping and leaving

Any tester, at any time, without telling us:

- **Stop capture instantly** — one click on the recording bar ("Stop all"), or
  Privacy controls → "Stop capture now". It revokes the allow-list *and* halts
  the running pipelines, so nothing is recorded until they turn it back on.
- **Take their data out** — Privacy controls → "Back up my memory" (restorable)
  or "Export my data" (readable JSONL, no Mnemos needed).
- **Delete everything** — Privacy controls → "Delete everything", or
  `uninstall.bat` / `uninstall.command` with the app closed. It empties every
  directory Mnemos writes and produces a **deletion receipt**: a small JSON
  file naming each directory, its size before, and anything that could not be
  removed. Nothing personal is in the receipt.

Because there is no central copy, a deleted memory is gone everywhere it ever
existed. We can and will state that in writing for any tester who asks, and
their receipt is the evidence.

A tester who wants out on day 2 owes us no explanation and no exit interview.

## 4. Confidentiality, both ways

**Ours to yours.** Anything we learn about Boost Run through the pilot —
company names, deal conversations, anything a tester shows us in a support
session — is confidential. We will not repeat it, publish it, or use it in
marketing. If we want to name Boost Run as a pilot customer anywhere public, we
ask Andy first and take "no" as the answer.

**Yours to ours.** Mnemos, its roadmap, its pricing, and anything in this
document are confidential to Boost Run and not to be shared outside the firm
during the pilot.

Neither side owes the other exclusivity, and either can end the pilot at any
point without cause. If you end it, we delete the usage counts on request; if
we end it, testers keep their data and the deletion tools keep working.

## 5. What each side is putting in

**We provide:** the build and invite codes; a 30-minute install call per
tester; a support channel staffed with a same-day response promise for weeks
0–4; weekly 15-minute check-ins; and a week-4 readout.

**You provide:** 2–4 volunteers who will actually use it on real workdays, the
honest answer when they stop, and 30 minutes of Andy's time at week 4.

**Volunteers, not assignees.** Ambient capture on a work laptop is a personal
decision. We would rather have two willing testers than four assigned ones, and
we would rather a tester turn a source off than leave it on uncomfortably.

## 6. Liability, stated plainly

This is free, experimental software. We are not liable for lost data, missed
commitments, or anything a tester relied on it to remember. It is a second set
of notes, not a system of record. Nothing here transfers ownership of anything:
your data stays yours, our software stays ours.

---

*Mnemos Labs · Justin Adorante · [`README.md`](../README.md) for what the
system actually does · [`TESTER_SETUP.md`](../TESTER_SETUP.md) for install.*
