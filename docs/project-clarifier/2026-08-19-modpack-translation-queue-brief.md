# Modpack translation queue

## Goal

Let a user select several local modpack folders at once, review them as a
queue, and run Moru's existing scan and translation pipeline for one modpack at
a time without further input between packs.

## User workflow

1. Open the translation queue from Moru's normal navigation.
2. Select multiple modpack folders with the native folder picker.
3. Review the validated queue, reorder or remove pending items, and start it.
4. Moru scans and translates each pack sequentially using the translation
   settings captured when the queue starts and all discovered categories.
5. A failed or cancelled pack is recorded and the queue continues with the
   next pending pack.
6. Completed translations remain in the existing History screen, where the
   user performs review and export through the established W5/W6 flow.

## Scope and constraints

- Reuse the existing W1-W6 wizard job lifecycle, session records, progress
  frames, recovery checks, and engine APIs. Do not add an engine-side scheduler
  or rewrite the translation pipeline.
- Only one scan or translation job may be active for the queue at a time.
- Queue controls include start/resume, pause after the current pack, move up,
  move down, remove pending items, and retry failed items.
- Queue items and their order are persisted locally. After an app restart the
  queue is paused; pending items remain, while an interrupted active item is
  reconciled through the existing session/job recovery behavior instead of
  silently starting paid work.
- The desktop quit-warning remains active while the current queued pack is
  translating.
- Batch queue items use ordinary translation only. A/B/C previous-translation
  migration remains available through the existing single-pack workflow.
- Existing normal and migration behavior must remain unchanged outside the
  queue.

## Inputs and outputs

- Input: two or more user-selected local modpack directories. Duplicate paths
  already present in the queue are ignored.
- Invalid or inaccessible folders are rejected individually with an actionable
  reason; valid selections are still added.
- Output: one existing translation session per attempted pack. The queue does
  not automatically export resource-pack or overrides ZIP files.

## Failure modes

- A probe, scan, provider, or translation failure marks only that queue item as
  failed and advances to the next pending item.
- Cancelling the active translation records it as cancelled and advances unless
  the queue was paused.
- Starting a manual wizard job while the queue is running is blocked by the UI;
  pausing the queue waits for the current pack to finish rather than cancelling
  it.
- If the engine loses an active job across restart, the existing session logic
  reports that job as failed. Remaining queued packs require an explicit resume.

## Verification

- Unit tests cover persisted-state migration, deduplication, ordering, removal,
  retry, pause-after-current, sequential advancement, and continue-on-failure.
- Regression tests prove a queued normal run does not enable migration and
  preserves v1.0's matching W2-scan reuse in the W4 normal path.
- Run desktop tests, TypeScript checks, production build, and packaged Windows
  installer build.

## Deferred

- Per-item A/B/C migration inputs.
- Automatic review or export.
- Parallel translation jobs.
- CurseForge downloads or remote queue synchronization.
