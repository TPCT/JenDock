# Shared spacing follow-through — 2026-10-11

Scope: the monitoring React interface, preserving the current GitHub-style design
and the shared 4/8/12/16/24px spacing system.

The existing shared page, dialog, empty-state and Versions spacing already replaces
the larger gaps shown in the reported screenshot. This pass fixes remaining
consumer-level inconsistencies:

- Branch availability helper is grouped with its input, separated by 4px rather
  than an unrelated 16px form-row interval.
- Versions use a single metadata column at the same 800px breakpoint as the
  compact workspace, avoiding squeezed columns at 701–800px.
- Container Actions header shares the same inset as its row actions.
- Container metadata uses 4px label/value gaps and a 16px separation before the
  next pair on mobile, without a trailing value margin.
- Run-stage body indentation derives from the heading icon widths and gaps;
  mobile outcome alignment uses that same measurement.
- Loading/error content does not add an extra 16px padding inside already
  padded settings/resource surfaces.
- MUI table cells use shared 12px vertical/16px horizontal insets, with 8px
  vertical insets for explicitly dense tables.

Verification: frontend production build passed; 86 frontend tests passed;
`git diff --check` passed; Impeccable layout scan returned no findings.
Existing Vite warning about chunks larger than 500kB remains unrelated.

Browser review used the existing local product at port 19191, which clearly
labels its captured observations and disconnects production operations. Inspected
project containers/Versions, environment settings, Source & build dialog,
deployment detail, container detail, project directory and mobile navigation.
Desktop 1307px, tablet 760px and mobile 390px were checked. Versions reflowed into
one column at 760px; mobile Versions and directory had no page-width overflow.
The modal retained separate header/content/footer and touch controls.

Screenshots are in `.runtime/spacing-review/`. These are visual acceptance of
the local UI, not a claim of live operational or production deployment acceptance.
Database inspection tables share the new theme, but their full live workflow was
not rerun for this visual-only pass.
