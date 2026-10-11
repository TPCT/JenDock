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


## Final shared-component pass

- Empty title/description spacing now uses an explicit 8px flex gap and 16px
  inset rather than collapsing typography margins.
- Shared MUI Stacks use `useFlexGap` to preserve consistent wrapping, including
  dialog controls and responsive forms.
- Sidebar insets and row spacing use the shared scale; mobile rows retain 44px
  targets. Environment save/discard actions wrap in an 8px row.
- Current browser pass inspected loaded project containers/Versions, settings,
  project directory, deployment directory and run #27, global containers,
  integrations and activity. Mobile 390x844 checked Versions and Add container
  modal; document width/scroll width both 383px. This is a captured local preview,
  not live production acceptance. Other operational dialogs inherit shared theme
  styles but were not individually exercised this pass.
- Screenshots: `/tmp/monitor-spacing-desktop-final.png` and
  `/tmp/monitor-spacing-mobile-final.png`.
