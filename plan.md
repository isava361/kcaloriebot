# KCalorieBot — Review Findings and Improvement Plan

Date: 2026-07-13

## Overall verdict

The codebase is in unusually good shape for a personal bot. Durable DB-backed
sessions with optimistic concurrency (`revision` checks), timezone-correct day
boundaries including DST, WAL + transactions + CHECK constraints, owner-scoped
queries everywhere, a Go→Python data migrator, legacy callback compatibility,
and ~1,600 lines of tests. No urgent bugs were found. The main opportunity is
on the product side: the bot is robust but high-friction to use every day.

## Part 1 — Code improvements (polish, not problems)

Status: **done** (2026-07-13, except 1.4 which is deferred). All 93 tests pass;
ruff check and format are clean.

### 1.1 Deduplicate prompt text — DONE
Prompt strings used to appear both in `_session_prompt` and inline at each
`_start_with_prompt` call site.

**Done:** all prompts are now named constants in `render.py`
(`TIMEZONE_ONBOARDING_PROMPT`, `FOOD_NAME_PROMPT`, …) used by both
`session_prompt()` and the handlers. `_advance_with_prompt` no longer takes a
prompt argument — it derives the prompt for the new state from
`session_prompt()`, so the food wizard's step prompts exist in exactly one
place. Minor wording unifications: the resume prompt for timezone entry now
matches the onboarding/change prompts instead of slightly shorter variants.

### 1.2 Extract the repeated handler preamble — DONE
Six handlers repeated the same opening ritual: `_require_private` →
`_identity` → `ensure_user` → fetch session → expiry check.

**Done:** `_begin(update, context, register_user=...)` returns a `Turn`
dataclass (database, user_id, chat_id, message, session, session_expired) or
`None` when the update should be ignored. `_active_session()` centralizes the
expiry check and is also used by `handle_callback`. Side benefits: `cancel`
and `update_timezone` now also clear expired sessions (previously a week-old
stale session could block `/updatetimezone`), and the shared re-prompt tail of
`unknown_command`/`handle_non_text` moved into `_reprompt()`.

### 1.3 Split `bot.py` (was 1,400 lines) — DONE
**Done:** split into three modules instead of the originally sketched four —
keeping all handlers in `bot.py` avoids import cycles between the text and
callback handlers, which share most helpers:

- `callbacks.py` (149 lines) — `CallbackAction`, `parse_callback`,
  `confirmation_expired`, paging constants. Deliberately telegram-free, which
  let `tests/test_callbacks.py` drop its entire telegram-stubbing machinery
  (it previously only passed when run after other tests had imported the real
  library).
- `render.py` (173 lines) — keyboards, prompt constants, `session_prompt`,
  `navigation_row`, and all message/button formatting.
- `bot.py` (1,137 lines) — handlers and `build_application`, with an
  `__all__` re-exporting the public names so existing imports keep working.

### 1.4 Reduce DB round-trips per message — DEFERRED
A single text message can still open several SQLite connections, each
re-running the PRAGMAs. With `concurrent_updates(False)` processing is
serialized anyway, so this is invisible at current scale.

**Next step if ever needed:** a single long-lived connection in `Database`,
or a combined "get user + session" query.

### 1.5 Plan removal of legacy callback prefixes — DONE
**Done:** the legacy blocks in `callbacks.py` now carry a comment marking them
safe to remove (together with their tests) after 2027-07, one year after the
Go bot was retired.

### 1.6 Tighten `_confirmation_expired` — DONE
**Done:** now `confirmation_expired` in `callbacks.py`, using
`not 0 <= now - issued_at <= 15 * 60`. Also slightly safer than the old
`abs()` version: a forged callback with a future timestamp is now treated as
expired instead of valid.

## Part 2 — Product / idea improvements (ranked by impact)

Status: **2.1, 2.2, 2.3, 2.4, and 2.6 done** (2026-07-13). Schema migrated to
version 3 (`users.daily_calorie_goal`, `sessions.selected_entry_id`) with
automatic v1/v2 → v3 upgrade at startup. 139 tests pass; ruff clean.

The current flow to log one food is up to seven messages (name → calories →
grams → protein → fat → carbs → favorite?). For a tool used 3–5 times a day,
friction is the main threat to retention.

### 2.1 One-message quick add ⭐ — DONE
**Done:** `parse_quick_add` in `domain.py` accepts `oatmeal 370 60`,
`буханка 250 ккал 150 г` (units optional, order-fixing), and optional macro
tokens `p8 f3 c47` / `б8 ж3 у47`. Hooked into the `handle_text` fallback (so
plain messages log food when no menu option matches) and the new `/add`
command. Replies include the day's progress line.

### 2.2 Match favorites during Add Food — DONE
**Done:** when the wizard's food name matches a saved favorite
(case-insensitive via `find_favorite_by_name`), the bot jumps straight to the
grams prompt with the favorite's nutrition; `Enter Manually` falls back to the
normal calories → macros steps with the name preserved.

### 2.3 Daily goals and remaining budget ⭐ — DONE
**Done:** `Daily Goal` menu button + `WAIT_GOAL` state store a calorie target
in `users.daily_calorie_goal` (`Remove` clears it). Every entry-adding flow
replies with `Today: 1240 / 2000 kcal (760 left)` via `_today_progress`, and
`Today Stats` appends goal progress to the calories line. Macro targets were
left out deliberately to keep the flow one prompt.

### 2.4 Recent foods — DONE
**Done:** `Recent Foods` lists the latest 10 distinct food names (casefolded
dedupe in `recent_entry_templates`). Tapping one starts `WAIT_RECENT_GRAMS`,
where `Same as last time` repeats the previous serving and a number logs a new
amount; per-100g values are recovered from stored totals
(`per_100_from_totals`).

### 2.5 Nutrition lookup instead of manual entry
Integrate Open Food Facts or USDA FoodData Central so typing a product name
(or later, scanning a barcode photo) fills per-100g values automatically.
Manually typing four numbers per food is the part users abandon. A further
step is photo-based estimation via a vision LLM, but database lookup gives
most of the value for free.

### 2.6 Edit entries and backdating — DONE
**Done:** the entry view gained `Edit Grams` (re-scales stored nutrition to
the new serving weight) and `Edit Time` (accepts `HH:MM` for today or a full
`YYYY-MM-DD HH:MM` / `DD.MM.YYYY HH:MM` local datetime; rejects future times
and dates older than a year). Both run as durable sessions like every other
workflow.

### 2.7 Default serving sizes for favorites
Store "1 egg = 55 g" so using a favorite can be one tap instead of a grams
prompt.

### 2.8 Weekly summary + optional reminder
A scheduled evening "nothing logged today?" nudge and a Sunday recap (average
kcal, best/worst day). Cheap with python-telegram-bot's job queue.

### 2.9 CSV export
`/export` of the food history — users trust a tracker more when they can get
their data out; ~30 lines given the current schema.

### 2.10 Localization
Examples in the README suggest Russian-speaking users, but all buttons and
messages are English-only. Even a simple two-language string table would widen
the audience.

## Recommended next steps

With 2.1–2.4 and 2.6 shipped, the remaining ideas in rough priority order:
nutrition lookup via Open Food Facts / USDA (2.5), default serving sizes for
favorites (2.7), weekly summary + reminder (2.8), CSV export (2.9), and
localization (2.10).
