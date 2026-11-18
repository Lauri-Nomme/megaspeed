# 35 — One time unit: one "d", one clock

`docs/concepts.md` §1 defines the units. The product violates it in one place and misleads in another.

## Finding solved (docs/QA-exploratory.md)

- **#11** one unit letter, three clocks: "d" = 24 *business* hours in Overview/Delivery/Rules ("7 d 0 h"
  = 29 calendar days; "cycle < 3 d" = 9 working days), while the PR drawer uses *wall-clock* ("90 d 9 h")
  on a page that claims business hours. Timestamps are UTC while business hours are Tallinn, so "PR open →
  first review 0 h" next to timestamps 1 h 47 apart reads as a bug.

## Fix (conforms to concepts.md §1)

1. **`d` means 8 business hours — one working day — everywhere** a duration is rendered (`d = h/8`,
   so 168 business hours = `21 d`, not `7 d`). The PR drawer's wall-clock span is re-labelled as
   calendar time (e.g. "90 calendar days") so the two are never the same letter.
2. **State the clock where it matters** — the ticket/PR view says its timestamps are UTC and its
   durations are business hours on the team's (Tallinn) calendar, once, so a 0 h span between two UTC
   timestamps is explained rather than looking wrong.
3. **Label the calendar-day rendering** with the word "calendar", never "d", wherever an elapsed
   wall-clock figure is shown.

## Tests

- The PR drawer and the delivery list render the same underlying span as "N d M h" (business) vs
  "N calendar days" respectively; a duration label never uses "d" for a wall-clock span; the clock note
  is present on the ticket view.
