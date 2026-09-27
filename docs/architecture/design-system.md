# Interface design system

The workspace is a clinical reading surface. Its visual job is to make one thing obvious — what the
answer is and what evidence stands behind it — and to keep everything else out of the way without
hiding it.

This document records the tokens, the rules that govern them, and the measured accessibility
results. It describes the presentation layer only. No retrieval, evidence, generation, verification
or safety behaviour is expressed here, and none of it changed when this system was introduced.

## Principles

Drawn from mature enterprise and clinical-reference practice rather than from any one product.

**Density comes from spacing and type, not from containers.** IBM Carbon's spacing scale is built
on multiples of four so that small and large intervals stay in the same system; the scale below
does the same. Where the interface previously drew a border, a radius and a fill around nearly
every block, hierarchy now comes from whitespace and weight. When every block is a card, no block
is emphasised.

**Prose is capped in characters, not pixels.** Readability research puts the comfortable band at
50–75 characters a line, around 66 being the usual target. `--measure` is `68ch` and governs the
answer text, the source excerpts and every explanatory paragraph. A source excerpt set across a
full 1040-pixel card ran past 110 characters a line.

**Status is carried by three signals.** Every outcome, every stage and every selected item states
itself with a mark, a word and a colour. Remove the colour and the interface still reads — which is
what makes it legible in greyscale, to a colour-blind reader and to a screen reader.

**Engineering vocabulary is opt-in.** Milestone markers, chunk types, authority levels, recorded
regions, model identifiers and retrieval constants are real and stay reachable. They live behind
`Source details`, `Timing` and the Advanced tools group. A clinician or a student never has to
learn what M10, RRF or MedCPT are in order to read an answer.

**Motion is restrained and never informative on its own.** The only thing that animates on its own
is the pulse on the running stage mark, and that row already says "Running now" in text. Everything
honours `prefers-reduced-motion`.

## Tokens

All tokens are defined in `frontend/src/styles/tokens.css`. The rule the stylesheet enforces is
that no literal colour appears outside that file.

### Colour

| Token | Value | Use |
| --- | --- | --- |
| `--bg-app` | `#f6f8f9` | the canvas the workspace sits on |
| `--bg-surface` | `#ffffff` | cards, panels, the composer |
| `--bg-subtle` | `#eef2f3` | quiet fills: quotes, thumbnails, code |
| `--bg-sidebar` | `#10272d` | the rail |
| `--bg-hover` / `--bg-selected` | `#eef2f3` / `#e5f0f0` | light-context interaction states |
| `--bg-rail-hover` / `--bg-rail-selected` | `#1b3d45` / `#22505a` | dark-context interaction states |
| `--text-primary` / `--text-secondary` / `--text-muted` | `#0f2126` / `#44575d` / `#5d7076` | the three levels of prose |
| `--text-inverse` | `#ffffff` | on the accent and on the rail's selected rows |
| `--text-rail` / `--text-rail-muted` | `#cfdfe0` / `#a6c0c2` | rail labels and rail metadata |
| `--border-subtle` / `--border-strong` | `#e1e7e9` / `#c3ced1` | decorative separation |
| `--border-control` | `#84979c` | the edge of an input or button — the only border held to 3:1 |
| `--accent-primary` / `--accent-hover` / `--accent-subtle` | `#0f5b60` / `#0b464a` / `#e5f0f0` | links, primary actions, the question bubble |
| `--success` / `--success-subtle` | `#1a6b41` / `#e6f3eb` | verified |
| `--warning` / `--warning-subtle` | `#8a5a0f` / `#fbf2e0` | withheld, unverified, figure notices |
| `--danger` / `--danger-subtle` | `#9b332a` / `#fbeae7` | technical failure |
| `--info` / `--info-subtle` | `#2d5c8e` / `#e9f1f8` | scope, educational-use notice |
| `--focus-ring` | `#b47719` | the one focus indicator, everywhere |

One accent serves both the link colour and the filled button, which is what stops the interface
drifting into several unrelated teals. Semantic colours are used as accents on top of a mark and a
word, never as the signal itself.

### Spacing, radius, elevation

Spacing is `4 / 8 / 12 / 16 / 24 / 32 / 48`. A value off the scale is a mistake, not a nuance.

Radius is three steps: `6px` for controls, `10px` for cards, `14px` for the composer and the
question bubble.

Elevation is two levels, used only for things that genuinely float: the account menu and the mobile
drawer. A card that merely sits on the canvas gets a border.

### Typography

A system stack — `Inter` where the operating system already has it, then Segoe UI Variable, system
UI, and the platform fallbacks. Nothing is downloaded, because first paint in a clinical workspace
should not wait on a font request. The previous stylesheet named `Inter` without ever loading it.

| Step | Value |
| --- | --- |
| page title | `600 clamp(24px, 2.2vw, 30px)/1.2` |
| section heading | `600 17px/1.35` |
| body | `400 15px/1.65` (answer text 15.5px) |
| secondary | `400 14px/1.6` |
| metadata | `400 12.5px/1.5` |
| technical / timings | `400 12px/1.5` monospace |

Nothing is heavier than 600. A page where everything is bold has no emphasis left to give.

## Accessibility results

Measured against the running application, reading the effective computed colour of each element and
the first ancestor that actually paints a background. The harness is
`.local/a11y.mjs`; the keyboard and zoom harness is `.local/focus.mjs`.

**Contrast: 25 of 25 text pairs pass WCAG 2.1 AA.** The pairs that matter most:

| Element | Ratio | Required |
| --- | --- | --- |
| user question bubble (white on accent) | 7.82:1 | 4.5:1 |
| answer body text | 15.57:1 | 4.5:1 |
| source excerpt | 14.72:1 | 4.5:1 |
| rail navigation label | 8.89:1 | 4.5:1 |
| rail selected conversation | 8.89:1 | 4.5:1 |
| rail section heading | 8.10:1 | 4.5:1 |
| outcome status line | 6.12:1 | 4.5:1 |
| figure notice | 5.32:1 | 4.5:1 |
| evidence page line, disclosure summary | 5.19:1 | 4.5:1 |
| disabled submit label | 5.05:1 | 4.5:1 |
| muted helper text, footer, section headings | 4.87:1 | 4.5:1 |

The question bubble was the worst defect in the previous interface: a generic `article p` rule
applied a muted grey to the bubble's paragraph, so muted grey rendered on deep teal at roughly
1.5:1. It is now white on the accent at 7.82:1.

`--border-control` is held to the 3:1 of WCAG 1.4.11 because the edge of an input carries meaning.
The other two border tokens separate surfaces and are decorative.

**Keyboard: 22 of 22 stops paint a visible indicator** — a 3px `--focus-ring` outline. Two were
fixed during this pass: the composer, whose pale tinted ring measured 1.1:1 and is now the standard
amber ring on the box that wraps the field; and the scrollable source excerpt, which browsers make
focusable because it scrolls and which now has an explicit tab stop, an accessible name and the
application's ring rather than the browser's default outline.

**Zoom: no sideways scrolling at 150% or 200%.** At 200% the layout reflows to the narrow-viewport
treatment, and a separate short-viewport rule stops the composer claiming most of the screen.

## Responsive behaviour

One breakpoint at 900px and one height rule at 560px.

Above 900px the rail is in the layout, collapsible to a 72px icon rail, and the conversation is
centred at 1040px with other pages capped at 1200px. Below 900px the rail becomes a drawer over the
content with a scrim; nothing is removed from it. Below 560px tall the composer compacts without
losing a control.

## What is deliberately still visible

The Retrieval inspector, the chunk and index inspectors and the Timing waterfall still speak in
RRF constants, BM25 parameters, encoder identifiers and chunk types. That is correct: they exist
for engineers, they sit behind the Advanced tools disclosure, and the permission that lists them is
the permission that can use them.
